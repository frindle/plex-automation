"""llm_advisor.py -- glue between arr-webhook seams and llm_decide: mode, layered transport, hard rules, logging.

Decision chain (each layer is skipped when its env is unset):
  1. local Darkbloom (LAN)   DARKBLOOM_BASE_URL + DARKBLOOM_API_KEY  (alias: LLM_DECIDE_URL/_MODEL/_KEY)
  2. paid Darkbloom          DARKBLOOM_CLOUD_API_KEY
  3. Claude                  ANTHROPIC_API_KEY (official SDK, structured output)
  4. rules                   the caller keeps rules_action
While the local layer is unavailable the decision is PENDING for LLM_LOCAL_WAIT_MIN minutes (default 15)
before a paid layer is used. A valid but low-confidence open-model answer escalates to Claude at once.
Shadow mode only asks the local layer: it changes nothing, so it never spends paid calls.
import_identity answers are cached by pending key for LLM_ANSWER_TTL_H hours (default 24), so a stuck import
isn't re-asked every pass. Hard rules still run on the current facts.
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
CLAUDE_MODEL = 'claude-sonnet-5-5'
MODELS_TTL = 600.0
PENDING_MAX_AGE = 24 * 3600.0
DEFAULT_WAIT_MIN = 15.0
DEFAULT_ANSWER_TTL_H = 24.0
ANSWER_CACHE_MAX = 2000
CACHE_KINDS = frozenset({'import_identity'})

# What a seam does while a decision is pending in enforce mode (shadow always keeps the rules action).
PENDING_ACTION = {'import_identity': 'unsure'}


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
    """One transport: fn(prompt) -> raw text. model() names the model that answers (for the log)."""

    def __init__(self, name, fn, model=None):
        self.name = name
        self.fn = fn
        self._model = model

    def model(self):
        try:
            return self._model() if callable(self._model) else self._model
        except Exception:
            return None


_MODELS_CACHE = {}


def pick_local_model(base, key, http, now=time.time, timeout=(2, 5)):
    """LLM_LOCAL_MODEL is handled by the caller; otherwise the first id from GET {base}/v1/models, cached 10 min."""
    hit = _MODELS_CACHE.get(base)
    if hit and now() - hit[0] < MODELS_TTL:
        return hit[1]
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    r = http.get(base.rstrip('/') + '/v1/models', headers=headers, timeout=timeout)
    r.raise_for_status()
    model = r.json()['data'][0]['id']
    _MODELS_CACHE[base] = (now(), model)
    return model


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
    total = float(env.get('LLM_DECIDE_TIMEOUT') or 20)
    base = (env.get('DARKBLOOM_BASE_URL') or '').strip()
    legacy = (env.get('LLM_DECIDE_URL') or '').strip()
    if base or legacy:
        key = env.get('DARKBLOOM_API_KEY') if base else env.get('LLM_DECIDE_KEY')
        if base:
            fixed = (env.get('LLM_LOCAL_MODEL') or '').strip()
            model_fn = (lambda: fixed) if fixed else (lambda: pick_local_model(base, key, http, now))
            url = _chat_url(base)
        else:
            model_fn = (lambda: env.get('LLM_DECIDE_MODEL') or DEFAULT_MODEL)
            url = legacy
        layers['local'] = Layer('local', _chat_fn(url, model_fn, key, http, total, 2.0,
                                                  {'chat_template_kwargs': {'enable_thinking': False}}), model_fn)
    paid_key = (env.get('DARKBLOOM_CLOUD_API_KEY') or '').strip()
    if paid_key:
        pm = (env.get('DARKBLOOM_CLOUD_MODEL') or '').strip() or PAID_MODEL
        layers['paid'] = Layer('paid', _chat_fn(_chat_url(PAID_BASE), lambda: pm, paid_key, http, 20.0, 5.0), pm)
    ckey = (env.get('ANTHROPIC_API_KEY') or '').strip()
    if ckey or claude_client is not None:
        cm = (env.get('LLM_CLAUDE_MODEL') or '').strip() or CLAUDE_MODEL
        factory = (lambda: claude_client) if claude_client is not None else _real_claude_client(ckey)
        layers['claude'] = Layer('claude', _claude_fn(kind, cm, factory), cm)
    return layers


def hold_active(env, http=None):
    """(held, cause). Held only on a 200 whose JSON has hold exactly true; anything else fails open."""
    url = (env.get('LLM_LOCAL_HOLD_URL') or '').strip()
    if not url:
        return False, None
    try:
        if http is None:
            import requests as http
        tok = env.get('LLM_LOCAL_HOLD_TOKEN')
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
VOLATILE_FACTS = frozenset({'seeded_days', 'seeders'})


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
    """Return a dict: decision, layer, model, latency_ms, pending, pending_min, escalations, hold_cause, error."""
    prompt = llm_decide.build_prompt(kind, facts)
    key = pending_key(kind, facts)
    res = {'decision': None, 'layer': None, 'model': None, 'latency_ms': None, 'pending': False,
           'pending_min': None, 'escalations': [], 'calls': [], 'hold_cause': None, 'error': None, 'first_pending': False}

    def answered(layer, dec, lat):
        store.remove(key)
        res.update(decision=dec, layer=layer.name, model=layer.model(), latency_ms=lat)

    def weak(dec):
        return dec['confidence'] < llm_decide.MIN_CONFIDENCE

    local = layers.get('local')
    if local is not None:
        held, cause = hold_active(env, hold_http)
        if held:
            dec, err, lat = None, 'hold', 0
            res['hold_cause'] = cause
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
            mode = (env.get('LLM_DECISIONS', 'off') or '').strip().lower()
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
        if mode == 'shadow':
            # Shadow changes nothing, so it never pays for an answer.
            layers = {k: v for k, v in layers.items() if k not in ('paid', 'claude')}
        else:
            capped = paid_cap_reached(env, path, time.strftime('%Y-%m', time.gmtime(now())))
            if capped:
                layers = {k: v for k, v in layers.items() if k not in ('paid', 'claude')}

        answers, akey, ttl_s = None, None, 0.0
        if kind in CACHE_KINDS:
            try:
                ttl_s = float(env.get('LLM_ANSWER_TTL_H', DEFAULT_ANSWER_TTL_H)) * 3600.0
            except (TypeError, ValueError):
                ttl_s = DEFAULT_ANSWER_TTL_H * 3600.0
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
                info.update(pending=False, layer='cache', pending_min=None)
            return llm_decide.resolve_action(kind, rules_action, decision, mode)

        res = run_chain(kind, facts, layers, store, now, wait_min, env, hold_http)
        decision = res['decision']
        if decision is not None and answers is not None:
            answers.put(akey, decision, res['layer'], res['model'])

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
            info.update(pending=res['pending'], layer=res['layer'], pending_min=res['pending_min'])

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
