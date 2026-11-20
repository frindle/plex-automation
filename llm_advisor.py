"""llm_advisor.py -- glue between arr-webhook seams and llm_decide: mode, layered transport, hard rules, logging.

Decision chain (each layer is skipped when its env is unset):
  0. Unraid Ollama (LAN)     UNRAID_OLLAMA_URL, model LLM_UNRAID_MODEL (default qwen3:14b), only for
                             LLM_UNRAID_KINDS (default import_identity,dupe_confirm,upgrade_gate,torrent_error,unregistered_triage,stuck_import,stuck_upgrade_tag,duplicate_folder,audit_findings). Asked only when the GPU is free
                             (nothing loaded, or its own model loaded) and LLM_UNRAID_HOLD_URL isn't holding.
                             Its answer is kept only when, after hard rules, it is decisive (not unsure) at
                             confidence >= MIN_CONFIDENCE; anything else passes on to local Darkbloom.
  1. local Darkbloom (LAN)   DARKBLOOM_BASE_URL + DARKBLOOM_API_KEY  (alias: LLM_DECIDE_URL/_MODEL/_KEY)
  2. paid Darkbloom          DARKBLOOM_CLOUD_API_KEY
  3. Claude                  ANTHROPIC_API_KEY (official SDK, structured output)
  4. rules                   the caller keeps rules_action
Paid layers are only a backup for the free ones: while the free layers (local, or Unraid when it is the only one)
can't answer, the decision is PENDING for LLM_LOCAL_WAIT_MIN minutes (default 15) before a paid layer is used, and
with no free layer configured nothing paid is asked (the rules stand). A valid but low-confidence open-model
answer escalates to Claude at once.
Shadow mode only asks the free layers (Unraid, local): it changes nothing, so it never spends paid calls.
dupe_confirm and torrent_error (FREE_ONLY_KINDS) only ask the free layers in every mode: duplicate cleanup is not time-sensitive.
import_identity answers are cached by pending key for LLM_ANSWER_TTL_H hours (default 24), so a stuck import
isn't re-asked every pass; dupe_confirm answers are kept 720 hours (LLM_ANSWER_TTL_H_DUPE_CONFIRM); torrent_error answers 24 hours, so the hourly repair run does not re-ask. Hard rules still run on the current facts.
llm_decide stays the source of truth for the prompt, parsing, MIN_CONFIDENCE and hard rules.
"""

import hashlib
import json
import os
import threading
import time
import urllib.request

import llm_decide

DEFAULT_MODEL = 'qwen3.6-35b-a3b-vl-mtp-mxfp8'
DEFAULT_LOG = '/data/llm_decisions.jsonl'
DEFAULT_PENDING = '/data/llm_pending.json'
PAID_BASE = 'https://api.darkbloom.dev/v1'
PAID_MODEL = 'qwen3.5-35b-a3b'
PAID_EXTRA_BODY = {'chat_template_kwargs': {'enable_thinking': False}, 'max_tokens': 2048}
CLAUDE_MODEL = 'claude-sonnet-5-5'
MODELS_TTL = 15.0
# Auto-picked local models a kind may use: only models whose back-test passed for that kind. Whatever Darkbloom
# has loaded is used as is; one that isn't listed skips the local layer (never a model swap). Override per kind
# with LLM_LOCAL_APPROVED_<KIND> (comma-separated, '*' = any). An LLM_LOCAL_MODEL pin bypasses this.
LOCAL_APPROVED = {'import_identity': ('qwen3.5-35b-a3b',), 'upgrade_gate': ('qwen3.5-35b-a3b',)}
UNRAID_TIMEOUT = 30.0
# Cold asks that answered took up to 1 min 30 s (11-17..19), and 7 hit 2 min and aborted their load. The cold kinds
# are background pollers, so waiting longer costs nothing, while each abort throws a model load away.
UNRAID_COLD_TIMEOUT = 240.0
# A listed model counts as warm only if it answered this process within this long (Ollama's default keep-alive):
# /api/ps also lists a model whose load is still running or was just aborted, and a 30 s ask then aborts it again.
UNRAID_WARM_S = 300.0
# After an Unraid timeout the layer is skipped for this long, doubling per timeout in a row up to 4x (an answer
# resets it). Each timed-out ask had started a model load that the hang-up aborted (11-16/17: about 40 in a loop).
UNRAID_BACKOFF_MIN = 15.0
UNRAID_BACKOFF_MAX_X = 4
# Per (base, model): last answer time, timeouts in a row, skip-until. Per process; a restart starts clean.
_UNRAID_HEALTH = {}
# Kinds asked from a background poller, where waiting out a cold Unraid load is fine. Grab-time kinds keep 30 s.
UNRAID_COLD_KINDS = frozenset({'import_identity', 'dupe_confirm', 'torrent_error', 'unregistered_triage', 'stuck_import', 'stuck_upgrade_tag', 'duplicate_folder', 'audit_findings'})
PENDING_MAX_AGE = 24 * 3600.0
DEFAULT_WAIT_MIN = 15.0
DEFAULT_ANSWER_TTL_H = 24.0
ANSWER_CACHE_MAX = 2000
CACHE_KINDS = frozenset({'import_identity', 'dupe_confirm', 'torrent_error', 'unregistered_triage', 'stuck_import', 'stuck_upgrade_tag', 'duplicate_folder', 'audit_findings'})
# Per-kind answer lifetime in hours (LLM_ANSWER_TTL_H_<KIND> overrides). Two files' names don't change, so a
# dupe answer holds for a month; the rest use LLM_ANSWER_TTL_H.
ANSWER_TTL_H = {'dupe_confirm': 720.0, 'torrent_error': 24.0, 'unregistered_triage': 24.0, 'stuck_import': 24.0, 'stuck_upgrade_tag': 24.0, 'duplicate_folder': 24.0, 'audit_findings': 24.0}
# Kinds that never use a paid layer, in any mode (Penn 11-15: paid services only for time-sensitive work such as
# grab-time decisions and upgrades). They wait for the free layers, then fall back to the caller's rules action.
FREE_ONLY_KINDS = frozenset({'dupe_confirm', 'torrent_error', 'unregistered_triage', 'stuck_import', 'stuck_upgrade_tag', 'duplicate_folder', 'audit_findings'})
UNRAID_MODEL = 'qwen3:14b'
UNRAID_KINDS = 'import_identity,dupe_confirm,upgrade_gate,torrent_error,unregistered_triage,stuck_import,stuck_upgrade_tag,duplicate_folder,audit_findings'
# Actions an Unraid answer may settle on its own; unsure (or a hard rule turning an answer into unsure)
# always passes on to local Darkbloom.
UNRAID_DECISIVE = {'import_identity': frozenset({'match', 'mismatch'}),
                   'dupe_confirm': frozenset({'same', 'different'}),
                   'upgrade_gate': frozenset({'allow', 'throttle', 'reject'}),
                   'torrent_error': frozenset({'recheck', 'resume', 'relink', 'wait', 'remove_and_research'}),
                   'unregistered_triage': frozenset({'keep', 'remove_keep_library', 'remove_and_research'}),
                   'stuck_import': frozenset({'wait', 'retry_import'}),
                   'stuck_upgrade_tag': frozenset({'wait', 'run_sweep'}),
                   'duplicate_folder': frozenset({'leave', 'merge'}),
                   'audit_findings': frozenset({'fix', 'ignore'})}

# What a seam does while a decision is pending in enforce mode (shadow always keeps the rules action).
PENDING_ACTION = {'import_identity': 'unsure', 'dupe_confirm': 'unsure', 'torrent_error': 'wait',
                  'unregistered_triage': 'keep', 'stuck_import': 'ask', 'stuck_upgrade_tag': 'ask', 'duplicate_folder': 'ask', 'audit_findings': 'ask'}


def http_transport(url, model, timeout=20):
    def transport(prompt):
        data = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.2,
            'max_tokens': 1024,
            'chat_template_kwargs': {'enable_thinking': False},
        }
        body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        key = os.environ.get('LLM_DECIDE_KEY')
        if key:
            req.add_header('Authorization', 'Bearer ' + key)
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read())
        return result['choices'][0]['message']['content']
    return transport


# ── layers ───────────────────────────────────────────────────────────────────

class Meta(str):
    """Response text that also carries usage/request-id (still a plain str to the parser)."""

    def __new__(cls, text, meta=None):
        o = str.__new__(cls, text)
        o.meta = meta or {}
        return o


class Layer:
    """One transport: fn(prompt) -> raw text. model() names the model that answers (for the log).
    gate(), when set, returns None to go ahead or a short reason to skip the layer this time."""

    def __init__(self, name, fn, model=None, gate=None):
        self.name = name
        self.fn = fn
        self._model = model
        self.gate = gate

    def model(self):
        try:
            return self._model() if callable(self._model) else self._model
        except Exception:
            return None


_MODELS_CACHE = {}


_NOT_LOADED = frozenset({'unloaded', 'not_loaded', 'not-loaded', 'available', 'downloaded', 'offline', 'stopped'})


def _entry_loaded(e):
    """Darkbloom's /v1/models entries carry no loaded/status field today (the list is what is loaded), so every
    entry counts. Should one appear, an entry marked not loaded is dropped: listing is never a reason to load."""
    if e.get('loaded') is False:
        return False
    st = e.get('status', e.get('state'))
    return not (isinstance(st, str) and st.strip().lower() in _NOT_LOADED)


def local_models(base, key, http, now=time.time, timeout=(2, 5)):
    """Every loaded model id from GET {base}/v1/models, in listed order, cached MODELS_TTL seconds (short, so a
    model Darkbloom swaps in is seen on the next decision). Raises when the list can't be read."""
    hit = _MODELS_CACHE.get(base)
    if hit and now() - hit[0] < MODELS_TTL:
        return hit[1]
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    r = http.get(base.rstrip('/') + '/v1/models', headers=headers, timeout=timeout)
    r.raise_for_status()
    models = [e['id'] for e in r.json()['data']
              if isinstance(e, dict) and isinstance(e.get('id'), str) and e['id'].strip() and _entry_loaded(e)]
    _MODELS_CACHE[base] = (now(), models)
    return models


def pick_local_model(base, key, http, now=time.time, timeout=(2, 5)):
    """First loaded id from /v1/models (no approval check)."""
    return local_models(base, key, http, now, timeout)[0]


def _chat_url(base):
    base = base.rstrip('/')
    if base.endswith('/chat/completions'):
        return base
    if base.endswith('/v1'):
        return base + '/chat/completions'
    return base + '/v1/chat/completions'


def _chat_fn(url, model_fn, key, http, total, connect, extra_body=None):
    def fn(prompt):
        body = {
            'model': model_fn(),
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.2,
            'max_tokens': 1024,
        }
        if extra_body:
            body.update(extra_body)
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        r = http.post(url, json=body, headers=headers, timeout=(connect, total))
        r.raise_for_status()
        data = r.json()
        meta = {}
        usage = data.get('usage') if isinstance(data, dict) else None
        if isinstance(usage, dict):
            meta['prompt_tokens'] = usage.get('prompt_tokens')
            meta['completion_tokens'] = usage.get('completion_tokens')
        rid = None
        try:
            rid = r.headers.get('x-request-id') or r.headers.get('request-id')
        except Exception:
            pass
        rid = rid or (data.get('id') if isinstance(data, dict) else None)
        if rid:
            meta['request_id'] = str(rid)
        return Meta(data['choices'][0]['message']['content'], meta)
    return fn


def _rejected_request(e):
    """True for a 400/422: the server refused the request shape (an unknown field), not the model failing."""
    code = getattr(getattr(e, 'response', None), 'status_code', None)
    return code in (400, 422) or str(e).startswith(('http 400', 'http 422'))


def _first_accepted(fn, plain):
    """Ask with fn; if the server rejects that request shape, ask once more with plain."""
    def go(prompt):
        try:
            return fn(prompt)
        except Exception as e:  # noqa: BLE001
            if not _rejected_request(e):
                raise
            return plain(prompt)
    return go


def _model_tag(name):
    """Ollama's name for a model: 'qwen3' and 'qwen3:latest' are the same model."""
    n = str(name or '').strip()
    return n if ':' in n.rsplit('/', 1)[-1] else n + ':latest'


def _unraid_health(base, model):
    return _UNRAID_HEALTH.setdefault((base.rstrip('/'), _model_tag(model)), {'ok_at': None, 'fails': 0, 'until': 0.0})


def _unraid_backoff_s(env, fails):
    try:
        base = float(env.get('LLM_UNRAID_BACKOFF_MIN') or UNRAID_BACKOFF_MIN)
    except (TypeError, ValueError):
        base = UNRAID_BACKOFF_MIN
    return max(0.0, base) * 60.0 * min(2 ** max(0, fails - 1), UNRAID_BACKOFF_MAX_X)


def _ollama_chat_fn(base, model, http, total=UNRAID_TIMEOUT, connect=2.0, state=None, cold_total=None, env=None,
                    now=time.time):
    """Native Ollama /api/chat, the shape the back-test ran: no stream, no thinking, temperature 0.2.
    When the gate judged the model cold (state['cold']), the read timeout is cold_total so the model load fits.
    A timeout starts the layer's back-off; an answer marks the model warm and clears it."""
    url = base.rstrip('/') + '/api/chat'
    health = _unraid_health(base, model)

    def fn(prompt):
        body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': False, 'think': False,
                'options': {'temperature': 0.2, 'num_predict': 1024}}
        t = cold_total if cold_total and state is not None and state.get('cold') else total
        try:
            r = http.post(url, json=body, headers={'Content-Type': 'application/json'}, timeout=(connect, t))
        except Exception as e:  # noqa: BLE001
            if 'timeout' in type(e).__name__.lower():
                health['fails'] += 1
                health['until'] = now() + _unraid_backoff_s(env or {}, health['fails'])
            raise
        r.raise_for_status()
        data = r.json()
        health.update(ok_at=now(), fails=0, until=0.0)
        meta = {}
        if isinstance(data, dict):
            meta['prompt_tokens'] = data.get('prompt_eval_count')
            meta['completion_tokens'] = data.get('eval_count')
        return Meta(data['message']['content'], meta)
    return fn


def _unraid_gate(base, model, env, http, state=None, now=time.time):
    """Skip reason, or None when the Unraid GPU is free for this model. The queue shares that GPU: asking
    while it has another model loaded would evict it, so a different resident model means skip. A hold returns
    ('hold', cause) when the hold names one (the queue's GPU hold says 'plex' or 'gpu-exclusive'). 'backoff' while
    a recent timeout's back-off runs. Sets state['cold'] unless this model is listed and answered within
    UNRAID_WARM_S (the ask may include a model load)."""
    health = _unraid_health(base, model)

    def gate():
        if state is not None:
            state['cold'] = False
        if now() < health['until']:
            return 'backoff'
        held, cause = hold_active(env, http, 'LLM_UNRAID_HOLD')
        if held:
            return ('hold', cause) if cause else 'hold'
        try:
            r = http.get(base.rstrip('/') + '/api/ps', timeout=1)
            r.raise_for_status()
            loaded = {_model_tag(m.get('model') or m.get('name')) for m in (r.json().get('models') or [])
                      if isinstance(m, dict)}
        except Exception as e:  # noqa: BLE001
            return 'timeout' if 'timeout' in type(e).__name__.lower() else 'error'
        if loaded and _model_tag(model) not in loaded:
            return 'other-model-resident'
        if state is not None:
            ok_at = health['ok_at']
            state['cold'] = not loaded or ok_at is None or now() - ok_at > UNRAID_WARM_S
        return None
    return gate


def _model_key(name):
    """'mlx-community/Qwen3.5-35B-A3B' and 'qwen3.5-35b-a3b' compare equal."""
    return str(name or '').strip().rsplit('/', 1)[-1].lower()


def local_approved(kind, env):
    """Approved model keys for kind, or None when any model is allowed."""
    raw = env.get('LLM_LOCAL_APPROVED_' + kind.upper())
    names = [n.strip() for n in raw.split(',')] if raw is not None and raw.strip() else list(LOCAL_APPROVED.get(kind, ()))
    if '*' in names:
        return None
    return {_model_key(n) for n in names if n}


def pick_approved(models, approved):
    """First listed id approved for the kind (approved None = any), else None."""
    for m in models:
        if approved is None or _model_key(m) in approved:
            return m
    return None


def local_model_state(kind, env, models, pin=None):
    """(model, skip) the local layer would use now for kind, given the loaded ids (None = list unreadable).
    skip is None, 'model-unknown', 'model-not-loaded' (pin not listed) or 'model-not-approved'. Never loads or
    switches anything: a pin must already be listed, and an unapproved list means the layer sits out."""
    if not models:
        return None, 'model-unknown'
    if pin:
        listed = next((m for m in models if _model_key(m) == _model_key(pin)), None)
        return (listed, None) if listed else (None, 'model-not-loaded')
    pick = pick_approved(models, local_approved(kind, env))
    return (pick, None) if pick else (None, 'model-not-approved')


def _local_gate(kind, env, models_fn, pin=None):
    """Skip reason for the local layer, or None to go ahead (see local_model_state)."""
    def gate():
        return local_model_state(kind, env, models_fn(), pin)[1]
    return gate


def claude_schema(kind):
    return {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': list(llm_decide.KINDS[kind])},
            'confidence': {'type': 'number'},
            'reason': {'type': 'string'},
        },
        'required': ['action', 'confidence', 'reason'],
        'additionalProperties': False,
    }


class ClaudeFailure(RuntimeError):
    def __init__(self, msg, meta):
        super().__init__(msg)
        self.meta = meta


def _claude_fn(kind, model, client_factory):
    def fn(prompt):
        client = client_factory()
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{'role': 'user', 'content': prompt}],
            output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': claude_schema(kind)}},
        )
        meta = {}
        usage = getattr(resp, 'usage', None)
        if usage is not None:
            meta['input_tokens'] = getattr(usage, 'input_tokens', None)
            meta['output_tokens'] = getattr(usage, 'output_tokens', None)
        rid = getattr(resp, '_request_id', None) or getattr(resp, 'id', None)
        if isinstance(rid, str):
            meta['request_id'] = rid
        if getattr(resp, 'stop_reason', None) == 'refusal':
            raise ClaudeFailure('claude refused', meta)
        for block in resp.content:
            if getattr(block, 'type', None) == 'text':
                return Meta(block.text, meta)
        raise ClaudeFailure('claude returned no text', meta)
    return fn


def _real_claude_client(key):
    def make():
        import anthropic
        return anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
    return make


def build_layers(kind, env, transport=None, http=None, claude_client=None, now=time.time):
    """Layers configured by env, in order. An explicit transport replaces the whole chain (it is the local layer)."""
    if transport is not None:
        return {'local': Layer('local', transport)}
    if http is None:
        import requests as http
    layers = {}
    ubase = (env.get('UNRAID_OLLAMA_URL') or '').strip()
    ukinds = {k.strip() for k in (env.get('LLM_UNRAID_KINDS') or UNRAID_KINDS).split(',') if k.strip()}
    if ubase and kind in ukinds:
        um = (env.get('LLM_UNRAID_MODEL') or '').strip() or UNRAID_MODEL
        state = {}
        cold = float(env.get('LLM_UNRAID_COLD_TIMEOUT') or UNRAID_COLD_TIMEOUT) if kind in UNRAID_COLD_KINDS else None
        layers['unraid'] = Layer('unraid', _ollama_chat_fn(ubase, um, http, state=state, cold_total=cold, env=env,
                                                           now=now), um,
                                 _unraid_gate(ubase, um, env, http, state, now))
    total = float(env.get('LLM_DECIDE_TIMEOUT') or 20)
    base = (env.get('DARKBLOOM_BASE_URL') or '').strip()
    legacy = (env.get('LLM_DECIDE_URL') or '').strip()
    if base or legacy:
        key = env.get('DARKBLOOM_API_KEY') if base else env.get('LLM_DECIDE_KEY')
        gate = None
        if base:
            fixed = (env.get('LLM_LOCAL_MODEL') or '').strip()

            def models_fn():
                return local_models(base, key, http, now)

            def model_fn():
                models = models_fn()
                return local_model_state(kind, env, models, fixed)[0] or (fixed or (models[0] if models else None))
            gate = _local_gate(kind, env, models_fn, fixed)
            url = _chat_url(base)
        else:

            def model_fn():
                return env.get('LLM_DECIDE_MODEL') or DEFAULT_MODEL
            url = legacy
        layers['local'] = Layer('local', _chat_fn(url, model_fn, key, http, total, 2.0,
                                                  {'chat_template_kwargs': {'enable_thinking': False}}), model_fn, gate)
    paid_key = (env.get('DARKBLOOM_CLOUD_API_KEY') or '').strip()
    if paid_key:
        pm = (env.get('DARKBLOOM_CLOUD_MODEL') or '').strip() or PAID_MODEL
        # Same no-thinking request as local: with thinking on, the model spent the whole token budget
        # reasoning and never wrote the JSON (every paid call 11-15..11-16 hit 1024 tokens, unparseable).
        purl = _chat_url(PAID_BASE)
        layers['paid'] = Layer('paid', _first_accepted(
            _chat_fn(purl, lambda: pm, paid_key, http, 20.0, 5.0, PAID_EXTRA_BODY),
            _chat_fn(purl, lambda: pm, paid_key, http, 20.0, 5.0, {'max_tokens': PAID_EXTRA_BODY['max_tokens']})), pm)
    ckey = (env.get('ANTHROPIC_API_KEY') or '').strip()
    if ckey or claude_client is not None:
        cm = (env.get('LLM_CLAUDE_MODEL') or '').strip() or CLAUDE_MODEL
        factory = (lambda: claude_client) if claude_client is not None else _real_claude_client(ckey)
        layers['claude'] = Layer('claude', _claude_fn(kind, cm, factory), cm)
    return layers


MODES = ('off', 'shadow', 'enforce')
# Kinds added after the first three: off unless LLM_MODE_<KIND> is set, so a deploy never starts them before
# their back-test (Penn 11-19: back-test first).
OPT_IN_KINDS = frozenset({'torrent_error', 'unregistered_triage', 'stuck_import', 'stuck_upgrade_tag', 'duplicate_folder', 'audit_findings'})


def kind_mode(kind, env=None):
    """Effective mode for one kind. LLM_DECISIONS is the default for every kind and the kill switch (off there is
    off everywhere); LLM_MODE_<KIND> (off|shadow|enforce) sets one kind on its own, so one kind can enforce while
    the rest stay in shadow. A missing or unknown per-kind value follows LLM_DECISIONS, except for OPT_IN_KINDS,
    which stay off until their own LLM_MODE_<KIND> is set (a new kind is back-tested before it asks anything)."""
    env = os.environ if env is None else env
    top = (env.get('LLM_DECISIONS', 'off') or '').strip().lower()
    top = top if top in ('shadow', 'enforce') else 'off'
    if top == 'off' or not isinstance(kind, str) or not kind:
        return top
    own = (env.get('LLM_MODE_' + kind.upper()) or '').strip().lower()
    if own in MODES:
        return own
    return 'off' if kind in OPT_IN_KINDS else top


def layer_status(env):
    """Which layers each kind would use, from env alone (no calls, no keys or URLs): for /llm-layers."""
    mode = kind_mode(None, env)
    ukinds = {k.strip() for k in (env.get('LLM_UNRAID_KINDS') or UNRAID_KINDS).split(',') if k.strip()}
    unraid = bool((env.get('UNRAID_OLLAMA_URL') or '').strip())
    local = bool((env.get('DARKBLOOM_BASE_URL') or '').strip() or (env.get('LLM_DECIDE_URL') or '').strip())
    paid = bool((env.get('DARKBLOOM_CLOUD_API_KEY') or '').strip())
    claude = bool((env.get('ANTHROPIC_API_KEY') or '').strip())
    kinds, modes = {}, {}
    for kind in sorted(llm_decide.KINDS):
        kmode = modes[kind] = kind_mode(kind, env)
        names = []
        if unraid and kind in ukinds:
            names.append('unraid')
        if local:
            names.append('local')
        free = bool(names)
        if free and kmode == 'enforce' and kind not in FREE_ONLY_KINDS:
            names += [n for n, on in (('paid', paid), ('claude', claude)) if on]
        kinds[kind] = names
    return {'mode': mode, 'modes': modes, 'kinds': kinds,
            'unraid_model': ((env.get('LLM_UNRAID_MODEL') or '').strip() or UNRAID_MODEL) if unraid else None,
            'local_model': ((env.get('LLM_LOCAL_MODEL') or '').strip() or 'auto') if local else None,
            'paid_model': ((env.get('DARKBLOOM_CLOUD_MODEL') or '').strip() or PAID_MODEL) if paid else None}


def local_now(env, http=None, now=time.time):
    """What the local layer would do for each kind right now, for /llm-layers: the loaded ids and, per kind, the
    model it would use or why it sits out. One cached GET of /v1/models; None when local isn't configured."""
    base = (env.get('DARKBLOOM_BASE_URL') or '').strip()
    if not base:
        return None
    if http is None:
        import requests as http
    pin = (env.get('LLM_LOCAL_MODEL') or '').strip()
    try:
        models = local_models(base, env.get('DARKBLOOM_API_KEY'), http, now)
    except Exception:  # noqa: BLE001
        models = None
    kinds = {}
    for kind in sorted(llm_decide.KINDS):
        model, skip = local_model_state(kind, env, models, pin)
        kinds[kind] = {'model': model, 'skip': skip}
    return {'loaded': models, 'pin': pin or None, 'kinds': kinds}


def hold_active(env, http=None, prefix='LLM_LOCAL_HOLD'):
    """(held, cause) from {prefix}_URL / {prefix}_TOKEN. Held only on a 200 whose JSON has hold exactly true;
    anything else fails open."""
    url = (env.get(prefix + '_URL') or '').strip()
    if not url:
        return False, None
    try:
        if http is None:
            import requests as http
        tok = env.get(prefix + '_TOKEN')
        headers = {'Authorization': 'Bearer ' + tok} if tok else {}
        r = http.get(url, headers=headers, timeout=1)
        if getattr(r, 'status_code', None) != 200:
            return False, None
        data = r.json()
        if isinstance(data, dict) and data.get('hold') is True:
            cause = data.get('cause')
            return True, cause if isinstance(cause, str) else None
    except Exception:
        pass
    return False, None


# ── pending store ────────────────────────────────────────────────────────────

# Facts that drift while a decision waits (swarm size, seeding age). They stay in the prompt but are left out of
# the pending and answer-cache keys, so the wait-for-local clock isn't restarted each time one ticks over.
VOLATILE_FACTS = frozenset({'seeded_days', 'seeders', 'age_days', 'age_hours'})


def _stable(v):
    if isinstance(v, dict):
        return {k: _stable(x) for k, x in v.items() if k not in VOLATILE_FACTS}
    if isinstance(v, (list, tuple)):
        return [_stable(x) for x in v]
    return v


def pending_key(kind, facts):
    blob = json.dumps(_stable(facts), sort_keys=True, default=str)
    return '%s:%s' % (kind, hashlib.sha256(blob.encode('utf-8')).hexdigest())


class PendingStore:
    """Decisions waiting on the local layer. first_seen survives restarts (atomic tmp+rename under a lock)."""

    def __init__(self, path, now=time.time):
        self.path = path
        self.now = now
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, encoding='utf-8') as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items() if isinstance(v, dict) and 'first_seen' in v}
        except (OSError, ValueError):
            pass

    def _expire(self):
        cut = self.now() - PENDING_MAX_AGE
        for k in [k for k, v in self.data.items() if not isinstance(v.get('first_seen'), (int, float)) or v['first_seen'] < cut]:
            del self.data[k]

    def _save(self):
        try:
            tmp = '%s.%d.tmp' % (self.path, os.getpid())
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # memory copy still carries the clock

    def touch(self, key, kind, error):
        with self.lock:
            self._expire()
            ent = self.data.get(key)
            if ent is None:
                ent = {'kind': kind, 'first_seen': self.now(), 'attempts': 0, 'last_error': None}
                self.data[key] = ent
            ent['attempts'] += 1
            ent['last_error'] = error
            self._save()
            return dict(ent)

    def remove(self, key):
        with self.lock:
            if self.data.pop(key, None) is not None:
                self._save()

    def drop_kind(self, kind):
        with self.lock:
            gone = [k for k, v in self.data.items() if v.get('kind') == kind]
            for k in gone:
                del self.data[k]
            if gone:
                self._save()
            return len(gone)


_STORES = {}


def get_store(path, now=time.time):
    st = _STORES.get(path)
    if st is None:
        st = _STORES[path] = PendingStore(path, now)
    st.now = now
    return st


class AnswerCache:
    """Final chain answers by pending key, kept LLM_ANSWER_TTL_H hours (atomic tmp+rename under a lock)."""

    def __init__(self, path, now=time.time):
        self.path = path
        self.now = now
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, encoding='utf-8') as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items()
                             if isinstance(v, dict) and isinstance(v.get('at'), (int, float)) and isinstance(v.get('decision'), dict)}
        except (OSError, ValueError):
            pass

    def get(self, key, ttl_s):
        with self.lock:
            ent = self.data.get(key)
            if ent is None:
                return None
            if self.now() - ent['at'] > ttl_s:
                del self.data[key]
                self._save()
                return None
            return dict(ent)

    def put(self, key, decision, layer, model):
        with self.lock:
            self.data[key] = {'at': self.now(), 'decision': dict(decision), 'layer': layer, 'model': model}
            if len(self.data) > ANSWER_CACHE_MAX:
                for k in sorted(self.data, key=lambda k: self.data[k]['at'])[:len(self.data) - ANSWER_CACHE_MAX]:
                    del self.data[k]
            self._save()

    def _save(self):
        try:
            tmp = '%s.%d.tmp' % (self.path, os.getpid())
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass


def get_answers(path, now=time.time):
    st = _STORES.get(('answers', path))
    if st is None:
        st = _STORES[('answers', path)] = AnswerCache(path, now)
    st.now = now
    return st


def answer_ttl_s(kind, env):
    """Seconds an answer of this kind is reused: LLM_ANSWER_TTL_H_<KIND>, else ANSWER_TTL_H, else LLM_ANSWER_TTL_H."""
    raw = env.get('LLM_ANSWER_TTL_H_' + str(kind).upper())
    if raw is None or not str(raw).strip():
        raw = ANSWER_TTL_H.get(kind)
    if raw is None:
        raw = env.get('LLM_ANSWER_TTL_H', DEFAULT_ANSWER_TTL_H)
    try:
        return float(raw) * 3600.0
    except (TypeError, ValueError):
        return DEFAULT_ANSWER_TTL_H * 3600.0


def _answers_path(env):
    p = (env.get('LLM_ANSWERS_PATH') or '').strip()
    if p:
        return p
    return os.path.join(os.path.dirname(env.get('LLM_PENDING_PATH', DEFAULT_PENDING)) or '.', 'llm_answers.json')


def drop_pending(kind, env=None, path=None):
    """Forget every pending decision of a kind (arr-webhook does this at startup for upgrade_gate)."""
    env = os.environ if env is None else env
    p = path or env.get('LLM_PENDING_PATH', DEFAULT_PENDING)
    try:
        return PendingStore(p).drop_kind(kind)
    except Exception:
        return 0


# ── the chain ────────────────────────────────────────────────────────────────

def _ask(kind, prompt, layer, calls=None):
    t0 = time.monotonic()
    err = None
    dec = None
    meta = {}
    try:
        raw = layer.fn(prompt)
        meta = dict(getattr(raw, 'meta', None) or {})
        dec = llm_decide.parse_decision(kind, raw)
        if dec is None:
            err = 'unparseable or invalid decision'
    except Exception as e:  # noqa: BLE001
        meta = dict(getattr(e, 'meta', None) or {})
        err = '%s: %s' % (type(e).__name__, str(e)[:120])
    lat = int((time.monotonic() - t0) * 1000)
    if calls is not None and layer.name in ('paid', 'claude'):
        calls.append(dict(meta, layer=layer.name, model=layer.model(), latency_ms=lat, ok=dec is not None))
    return dec, err, lat



def run_chain(kind, facts, layers, store, now, wait_min, env, hold_http=None):
    """Return a dict: decision, layer, model, latency_ms, pending, pending_min, escalations, hold_cause, error,
    unraid (what the Unraid layer did, when it is configured)."""
    prompt = llm_decide.build_prompt(kind, facts)
    key = pending_key(kind, facts)
    res = {'decision': None, 'layer': None, 'model': None, 'latency_ms': None, 'pending': False,
           'pending_min': None, 'escalations': [], 'calls': [], 'hold_cause': None, 'error': None, 'first_pending': False,
           'unraid': None, 'local_skip': None}

    def answered(layer, dec, lat):
        store.remove(key)
        res.update(decision=dec, layer=layer.name, model=layer.model(), latency_ms=lat)

    def weak(dec):
        return dec['confidence'] < llm_decide.MIN_CONFIDENCE

    unraid = layers.get('unraid')
    if unraid is not None:
        # Free first try. Its pass-ons don't touch the pending clock, which stays keyed to local Darkbloom,
        # and a weak answer goes to local, never straight to Claude.
        step = {'model': unraid.model(), 'latency_ms': None, 'pass': None}
        res['unraid'] = step
        try:
            skip = unraid.gate() if unraid.gate is not None else None
        except Exception:  # noqa: BLE001
            skip = 'error'
        if isinstance(skip, tuple):
            skip, step['hold_cause'] = skip
        if skip:
            step['pass'] = skip
        else:
            dec, err, lat = _ask(kind, prompt, unraid)
            step['latency_ms'] = lat
            if dec is None:
                kind_of = (err or '').split(':', 1)[0].lower()
                step['pass'] = ('timeout' if 'timeout' in kind_of else
                                'unparseable' if kind_of.startswith('unparseable') else 'error')
                step['error'] = err
            else:
                ruled, _ = llm_decide.apply_hard_rules(kind, dec['action'], facts)
                if ruled not in UNRAID_DECISIVE.get(kind, ()):
                    step['pass'] = 'unsure'
                elif weak(dec):
                    step['pass'] = 'weak'
                else:
                    answered(unraid, dec, lat)

    local = layers.get('local')
    if local is not None and res['decision'] is None:
        held, cause = hold_active(env, hold_http)
        skip = None
        if not held and local.gate is not None:
            try:
                skip = local.gate()
            except Exception:  # noqa: BLE001
                skip = 'model-unknown'
        if held:
            dec, err, lat = None, 'hold', 0
            res['hold_cause'] = cause
        elif skip:
            dec, err, lat = None, skip, 0
            res['local_skip'] = skip
        else:
            dec, err, lat = _ask(kind, prompt, local)
        if dec is not None:
            answered(local, dec, lat)
        else:
            res['error'] = err
            ent = store.touch(key, kind, err)
            mins = (now() - ent['first_seen']) / 60.0
            res['pending_min'] = round(mins, 1)
            if mins < wait_min:
                res.update(pending=True, first_pending=ent['attempts'] == 1)
                return res
    if local is None and unraid is not None and res['decision'] is None:
        # Unraid is the only free layer: it gets the same wait before anything paid is asked.
        why = 'unraid ' + str(res['unraid'].get('pass') or 'failed')
        res['error'] = why
        ent = store.touch(key, kind, why)
        mins = (now() - ent['first_seen']) / 60.0
        res['pending_min'] = round(mins, 1)
        # Plex has the GPU: playback ending frees it, so a Plex hold keeps waiting past the paid clock;
        # the next run asks Unraid again.
        plex = res['unraid'].get('pass') == 'hold' and res['unraid'].get('hold_cause') == 'plex'
        if mins < wait_min or plex:
            res.update(pending=True, first_pending=ent['attempts'] == 1)
            return res
    if local is None and unraid is None:
        # Paid layers are only a backup for the free ones (Penn, 2026-11-16). With no free layer configured,
        # nothing is asked and the rules stand.
        res['error'] = 'no free layer configured'
        return res
    if res['decision'] is None:
        paid = layers.get('paid')
        if paid is not None:
            res['escalations'].append(('paid', 'local unavailable' if local is not None else 'no local layer'))
            dec, err, lat = _ask(kind, prompt, paid, res['calls'])
            if dec is not None:
                answered(paid, dec, lat)
            else:
                res['error'] = err
    claude = layers.get('claude')
    if claude is not None and (res['decision'] is None or weak(res['decision'])):
        res['escalations'].append(('claude', 'low confidence' if res['decision'] is not None else 'open models failed'))
        dec, err, lat = _ask(kind, prompt, claude, res['calls'])
        if dec is not None:
            answered(claude, dec, lat)
        else:
            res['error'] = err
    return res


def advise(kind, facts, rules_action, mode=None, transport=None, log_path=None, record=None, env=None,
           info=None, now=None, layers=None, store=None, hold_http=None, http=None, claude_client=None):
    """Return the action the caller should take. `info` (a dict) receives pending/layer/pending_min for callers
    that need to retry later (the upgrade gate)."""
    try:
        if env is None:
            env = os.environ

        if mode is None:
            mode = kind_mode(kind, env)
        if mode not in ('shadow', 'enforce'):
            mode = 'off'
        if mode == 'off':
            return rules_action

        now = now or time.time
        if layers is None:
            layers = build_layers(kind, env, transport, http, claude_client, now)
        if store is None:
            store = get_store(env.get('LLM_PENDING_PATH', DEFAULT_PENDING), now)
        try:
            wait_min = float(env.get('LLM_LOCAL_WAIT_MIN', DEFAULT_WAIT_MIN))
        except (TypeError, ValueError):
            wait_min = DEFAULT_WAIT_MIN
        path = log_path or env.get('LLM_DECISIONS_LOG', DEFAULT_LOG)
        capped = False
        if mode == 'shadow' or kind in FREE_ONLY_KINDS:
            # Shadow changes nothing, so it never pays for an answer (Unraid and local are free); a free-only kind
            # never pays at all.
            layers = {k: v for k, v in layers.items() if k not in ('paid', 'claude')}
        else:
            capped = paid_cap_reached(env, path, time.strftime('%Y-%m', time.gmtime(now())))
            if capped:
                layers = {k: v for k, v in layers.items() if k not in ('paid', 'claude')}

        answers, akey, ttl_s = None, None, 0.0
        if kind in CACHE_KINDS:
            ttl_s = answer_ttl_s(kind, env)
            if ttl_s > 0:
                answers = get_answers(_answers_path(env), now)
                akey = pending_key(kind, facts)
        hit = answers.get(akey, ttl_s) if answers is not None else None
        if hit is not None:
            # Same question answered recently: no new call, no new log row; hard rules use today's facts.
            store.remove(akey)
            decision = hit['decision']
            action, rule = llm_decide.apply_hard_rules(kind, decision.get('action'), facts)
            if rule is not None:
                decision = dict(decision, action=action)
            if info is not None:
                info.update(pending=False, layer='cache', pending_min=None, llm_action=hit['decision'].get('action'))
            return llm_decide.resolve_action(kind, rules_action, decision, mode)

        res = run_chain(kind, facts, layers, store, now, wait_min, env, hold_http)
        decision = res['decision']
        if decision is not None and answers is not None:
            answers.put(akey, decision, res['layer'], res['model'])

        raw_action = decision['action'] if decision is not None else None
        if decision is not None:
            action, rule = llm_decide.apply_hard_rules(kind, decision['action'], facts)
            if rule is not None:
                decision = dict(decision, action=action,
                                reason='[hard rule %s] %s' % (rule, decision['reason']))

        extra ={'layer': res['layer'] or ('pending' if res['pending'] else None), 'model': res['model'],
                 'latency_ms': res['latency_ms'], 'pending_min': res['pending_min']}
        if capped:
            extra['paid_cap'] = True
        if res['hold_cause'] is not None:
            extra['hold_cause'] = res['hold_cause']
        if res['calls']:
            extra['paid_calls'] = res['calls']
        if res['error'] and decision is None:
            extra['error'] = res['error']
        if res['unraid'] is not None:
            extra['unraid'] = res['unraid']
        if res.get('local_skip'):
            extra['local_skip'] = res['local_skip']
        if not res['pending'] or res['first_pending']:
            llm_decide.log_decision(path, kind, json.dumps(facts, default=str, sort_keys=True)[:500],
                                    rules_action, decision, mode, extra)

        if record is not None:
            try:
                for layer, why in res['escalations']:
                    record('llm-escalation', {'kind': kind, 'layer': layer, 'reason': why, 'mode': mode})
                if not res['pending']:
                    record('llm-decision', {
                        'kind': kind, 'mode': mode, 'rules_action': rules_action,
                        'llm_action': decision['action'] if decision else None,
                    })
            except Exception:
                pass

        if info is not None:
            info.update(pending=res['pending'], layer=res['layer'], pending_min=res['pending_min'],
                        llm_action=raw_action)

        if res['pending']:
            return PENDING_ACTION.get(kind, rules_action) if mode == 'enforce' else rules_action
        return llm_decide.resolve_action(kind, rules_action, decision, mode)

    except Exception:
        return rules_action


def spend_summary(path, month):
    """Calls and tokens per paid layer for a YYYY-MM month, summed from the decision log. Tokens only, no prices."""
    out = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict) or not str(rec.get('ts', '')).startswith(month):
                    continue
                for c in rec.get('paid_calls') or []:
                    if not isinstance(c, dict) or not c.get('layer'):
                        continue
                    row = out.setdefault(c['layer'], {'calls': 0, 'input_tokens': 0, 'output_tokens': 0})
                    row['calls'] += 1
                    row['input_tokens'] += int(c.get('prompt_tokens') or c.get('input_tokens') or 0)
                    row['output_tokens'] += int(c.get('completion_tokens') or c.get('output_tokens') or 0)
    except OSError:
        pass
    return out


def paid_cap_reached(env, path, month):
    try:
        if env is None:
            return False
        raw = env.get('LLM_PAID_MONTHLY_MAX_CALLS')
        try:
            cap = int(str(raw).strip())
            if cap < 0:
                cap = 300
        except (ValueError, TypeError):
            cap = 300
        summary = spend_summary(path, month)
        count = sum(v['calls'] for v in summary.values())
        return count >= cap
    except Exception:
        return False


# ── failure-rate report (read-only; never touches decisions) ─────────────────
STATS_MIN_ANSWERED_PCT = 90.0
STATS_MAX_CAUSE_PCT = 5.0
STATS_MIN_AGREE_PCT = 95.0
STATS_CAUSES = ('cold_load_timeout', 'warm_timeout', 'other_model_loaded', 'plex_holding_gpu', 'backoff',
                'layer_down', 'no_layer', 'unparseable', 'other')


def read_decision_rows(path):
    """Rows of the decisions JSONL (dicts only; unreadable lines and a missing file are skipped)."""
    rows = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        pass
    return rows


def _row_epoch(row):
    import datetime
    try:
        d = datetime.datetime.fromisoformat(str(row.get('ts')).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def _timeout_cause(lat):
    # An ask past the warm timeout was given the cold budget (model load); no latency = the /api/ps gate timed out.
    if not isinstance(lat, (int, float)):
        return 'layer_down'
    # A warm timeout logs just over UNRAID_TIMEOUT (e.g. 30012 ms), so the cut sits halfway to the cold budget's floor.
    return 'cold_load_timeout' if lat > UNRAID_TIMEOUT * 1500 else 'warm_timeout'


def _unanswered_cause(row):
    """Normalised name for why a question got no answer, from the logged error / unraid step / local_skip."""
    err = str(row.get('error') or '')
    skip = row.get('local_skip')
    un = row.get('unraid') if isinstance(row.get('unraid'), dict) else None
    if err == 'no free layer configured':
        return 'no_layer'
    if un is not None and un.get('pass') and (not err or err.startswith('unraid ')):
        p = un['pass']
        if p == 'timeout':
            return _timeout_cause(un.get('latency_ms'))
        if p == 'backoff':
            return 'backoff'
        if p == 'other-model-resident':
            return 'other_model_loaded'
        if p == 'hold':
            return 'plex_holding_gpu' if un.get('hold_cause') == 'plex' else 'other'
        if p == 'unparseable':
            return 'unparseable'
        if p == 'error':
            return 'layer_down'
        return 'other'
    if skip == 'model-not-approved' or err == 'model-not-approved':
        return 'other_model_loaded'
    if skip == 'model-unknown' or err == 'model-unknown':
        return 'layer_down'
    if err == 'hold':
        return 'plex_holding_gpu' if row.get('hold_cause') == 'plex' else 'other'
    if err.startswith('unparseable'):
        return 'unparseable'
    head = err.split(':', 1)[0]
    if 'timeout' in head.lower():
        return 'warm_timeout'
    if head.endswith(('Error', 'Exception')):
        return 'layer_down'
    return 'other'


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def llm_stats(rows, days=7, now=None):
    """Per decision kind over the last `days`: asked / answered, layers, unanswered causes, hard-rule vetoes,
    agreement, median latency, below_target flags. Late answers: a question logged pending is answered later as
    another row for the same kind and context_summary; those rows are counted once (answered if any answered).
    Rows without a pending sibling count one question each. Output holds counts only, never context_summary."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400.0
    by_kind = {k: [] for k in llm_decide.KINDS}
    for r in rows or []:
        if not isinstance(r, dict) or r.get('kind') not in by_kind:
            continue
        t = _row_epoch(r)
        if t is None or t < cutoff:
            continue
        by_kind[r['kind']].append((t, r))
    out = {}
    for kind, items in by_kind.items():
        items.sort(key=lambda x: x[0])
        pend_keys = {r.get('context_summary') for _, r in items if r.get('layer') == 'pending'}
        pend_keys.discard(None)
        questions, groups = [], {}
        for _, r in items:
            ck = r.get('context_summary')
            if ck in pend_keys:
                groups.setdefault(ck, []).append(r)
            else:
                questions.append([r])
        questions.extend(groups.values())
        by_layer, causes, lats = {}, {}, []
        answered = veto = agree = disagree = 0
        for grp in questions:
            ans = [r for r in grp if r.get('llm_action') is not None]
            if not ans:
                c = _unanswered_cause(grp[-1])
                causes[c] = causes.get(c, 0) + 1
                continue
            r = ans[-1]
            answered += 1
            lay = str(r.get('layer') or 'unknown')
            by_layer[lay] = by_layer.get(lay, 0) + 1
            if str(r.get('reason') or '').startswith('[hard rule'):
                veto += 1
            if r.get('agree') is True:
                agree += 1
            elif r.get('agree') is False:
                disagree += 1
            lat = r.get('latency_ms')
            if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                lats.append(lat)
        asked = len(questions)
        ans_pct = _pct(answered, asked)
        agree_pct = _pct(agree, agree + disagree)
        lats.sort()
        mid = len(lats) // 2
        med = None if not lats else (lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2)
        below = []
        if asked:
            if ans_pct < STATS_MIN_ANSWERED_PCT:
                below.append('answered %.0f%% < %.0f%%' % (ans_pct, STATS_MIN_ANSWERED_PCT))
            for c, n in sorted(causes.items(), key=lambda x: -x[1]):
                if 100.0 * n / asked > STATS_MAX_CAUSE_PCT:
                    below.append('%s %d of %d (> %.0f%%)' % (c, n, asked, STATS_MAX_CAUSE_PCT))
            if agree_pct is not None and agree_pct < STATS_MIN_AGREE_PCT:
                below.append('agree %.0f%% < %.0f%%' % (agree_pct, STATS_MIN_AGREE_PCT))
            if veto:
                below.append('%d hard-rule veto' % veto)
        out[kind] = {'asked': asked, 'answered': answered, 'answered_pct': ans_pct, 'by_layer': by_layer,
                     'causes': causes, 'hard_rule_veto': veto, 'agree': agree, 'disagree': disagree,
                     'agree_pct': agree_pct, 'median_latency_ms': med, 'below_target': below}
    return out


def llm_stats_lines(stats):
    """One short line per kind that was asked anything. No titles."""
    lines = []
    for kind, s in (stats or {}).items():
        if not s.get('asked'):
            continue
        line = '%s: %d/%d answered (%.0f%%)' % (kind, s['answered'], s['asked'], s['answered_pct'])
        if s['causes']:
            c, n = max(s['causes'].items(), key=lambda x: x[1])
            line += ', top cause %s %d' % (c, n)
        if s['agree_pct'] is not None:
            line += ', agree %.0f%%' % s['agree_pct']
        if s['hard_rule_veto']:
            line += ', veto %d' % s['hard_rule_veto']
        lines.append(line)
    return lines
