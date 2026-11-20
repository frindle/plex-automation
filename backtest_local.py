#!/usr/bin/env python3
"""Back-test one decision kind through the local Darkbloom layer only, against whatever model is loaded.

usage: python backtest_local.py --kind K[,K2|all] [--limit N] [--facts FILE.jsonl]
       python backtest_local.py --kind K --layer claude --model claude-haiku-5-5 [--max-usd 2]
       python backtest_local.py --kind all --layer unraid [--model qwen3:14b] [--wait-min 50]

--layer unraid asks the Unraid Ollama (UNRAID_OLLAMA_URL) the way production does (llm_advisor._ollama_chat_fn, the
production timeouts, the same _unraid_gate before every row) and scores only the answers production would KEEP: the
ruled action is in UNRAID_DECISIVE[kind] and confidence is at least llm_decide.MIN_CONFIDENCE. The rest are passed
on (unsure, weak, invalid, error, timeout) and counted by reason. The model is --model, else LLM_UNRAID_MODEL, else
llm_advisor.UNRAID_MODEL, and must be listed by GET /api/tags (a back-test never pulls a model). The GPU is shared
with the queue: while the gate says hold or another model is resident the run polls every 30 s, one --wait-min
budget for the whole run; when it is spent the run stops INCONCLUSIVE (GPU busy) without sending a request.
Only kinds in LLM_UNRAID_KINDS run. There is no answered-% bar: passing on is by design.

--layer claude forces the Claude layer and model (key from ANTHROPIC_API_KEY; no Darkbloom needed, no GPU wait; the
production spend file is not touched). It reports per kind the agreement with the stored reference decisions
(rules_action / human), the invalid (unparseable) and error counts and the token cost, and stops before the run
would pass --max-usd (default 2 dollars).

Never a switch: the model is the first id GET {DARKBLOOM_BASE_URL}/v1/models lists (printed first). --model may only
name a listed id; anything else, an unreadable or empty list, or an LLM_LOCAL_MODEL pin that is not listed, refuses
to run (no request is sent), so the run can never make Darkbloom load a model. Approval is not checked: this run is
what earns a model its LLM_LOCAL_APPROVED_<KIND> entry.

Facts: JSONL. Default, first found in LLM_BACKTEST_DIR (/data/backtest): <kind>.jsonl, <kind>-live.jsonl (written by
the advisor's capture), then the newest <kind>-YYYYMMDD.jsonl; the run prints facts: plain|live|stamped. A row is a facts dict, or {"facts": {...}, "id": "...",
"rules_action": "...", "human": "..."}. The rules action is the row's `rules_action`, else facts['rules_action'];
`human` is the saved human decision, when there is one. The model answer goes through the same hard rules as
production. Waits while LLM_LOCAL_HOLD_URL says Plex (or anything) holds the GPU, up to --wait-min.

Output is counts and ids only (row numbers, plus a row's id when it is a short token): no facts, titles or paths.
"""
import argparse
import json
import os
import re
import sys
import time

import llm_advisor
import llm_decide

MIN_ANSWERED_PCT = 90.0     # Plex AI Tests.md: at least 90% answered while the GPU is free
MIN_AGREE_PCT = 95.0        # same bar as /llm-stats (STATS_MIN_AGREE_PCT), for rules and for saved human decisions
MIN_ROWS = 10               # below this the run says INCONCLUSIVE, not PASS
# Kinds where the rules action says something to agree with. For the rest the rules always say "ask" or "wait", so
# agreement means nothing there: each (rules -> model) pair is read by a person instead (Plex AI Tests.md).
AGREE_KINDS = frozenset({'import_identity', 'dupe_confirm', 'upgrade_gate', 'unregistered_triage'})
_ID = re.compile(r'^[A-Za-z0-9_.-]{1,40}$')


def norm(kind, action):
    """Compare actions: for unregistered_triage every remove_* is one 'remove'."""
    if kind == 'unregistered_triage' and isinstance(action, str) and action.startswith('remove'):
        return 'remove'
    return action


def pick_model(base, key, http, want=None, pin=None):
    """(model, None) or (None, reason). Reads /v1/models only; never names an unlisted model."""
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    try:
        r = http.get(base.rstrip('/') + '/v1/models', headers=headers, timeout=(2, 5))
        r.raise_for_status()
        ids = [e['id'] for e in r.json()['data'] if isinstance(e, dict) and isinstance(e.get('id'), str)
               and e['id'].strip() and e.get('loaded') is not False]
    except Exception:  # noqa: BLE001
        return None, 'models-unreadable'
    if not ids:
        return None, 'no-model-loaded'
    for name, why in ((pin, 'pin-not-loaded'), (want, 'model-not-loaded')):
        if name and name not in ids:
            return None, why
    return want or pin or ids[0], None


def load_rows(path, limit=None):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            if limit and len(rows) >= limit:
                break
    return rows


def _split(row):
    facts = row['facts'] if isinstance(row.get('facts'), dict) else row
    rules = row.get('rules_action') if isinstance(row.get('rules_action'), str) else facts.get('rules_action')
    human = row.get('human') if isinstance(row.get('human'), str) else None
    rid = row.get('id') if isinstance(row.get('id'), str) and _ID.match(row['id']) else None
    return facts, rules, human, rid


def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def percentile(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, max(0, int(-(-p * len(vals) // 100)) - 1))]


def _tokens(meta):
    """(input, output) tokens from a Meta dict (Claude: input/output_tokens; chat: prompt/completion_tokens)."""
    g = lambda *k: next((int(meta[x]) for x in k if isinstance(meta.get(x), (int, float))), 0)  # noqa: E731
    return g('input_tokens', 'prompt_tokens'), g('output_tokens', 'completion_tokens')


def _error_line(e):
    """Class, HTTP status and the API's own message (or str(e)), one line, at most 160 chars. Never the prompt."""
    body = getattr(e, 'body', None)
    msg = body.get('error', {}).get('message') if isinstance(body, dict) and isinstance(body.get('error'), dict) else None
    msg = ' '.join(str(msg or e).split())[:160]
    return '%s %s %s' % (type(e).__name__, getattr(e, 'status_code', '-'), msg)


def score(kind, rows, transport, clock=time.monotonic, price_model=None, max_usd=None, spent_usd=0.0):
    """Replay rows through transport. Returns counts, hard-rule blocks and latencies; nothing from the facts.
    With price_model set, each call's tokens are priced (llm_advisor.claude_cost_usd) into cost_usd, and the run
    stops before the next row once spent_usd + cost_usd has reached max_usd (stopped_budget=True)."""
    out = {'kind': kind, 'total': 0, 'answered': 0, 'rules_n': 0, 'rules_agree': 0, 'human_n': 0, 'human_agree': 0,
           'blocks': [], 'disagree_ids': [], 'pairs': {}, 'lat_ms': [], 'invalid': 0, 'errors': 0,
           'in_tokens': 0, 'out_tokens': 0, 'cost_usd': 0.0, 'stopped_budget': False, 'rows_available': len(rows)}
    for i, row in enumerate(rows, 1):
        if max_usd is not None and spent_usd + out['cost_usd'] >= max_usd:
            out['stopped_budget'] = True
            break
        facts, rules, human, rid = _split(row)
        ref = '#%d' % i + ('/' + rid if rid else '')
        out['total'] += 1
        t0 = clock()
        dec = None
        try:
            raw = transport(llm_decide.build_prompt(kind, facts))
            ti, to = _tokens(getattr(raw, 'meta', None) or {})
            dec = llm_decide.parse_decision(kind, raw)
            if dec is None:
                out['invalid'] += 1
        except Exception as e:  # noqa: BLE001
            out['errors'] += 1
            if 'first_error' not in out:
                out['first_error'] = _error_line(e)
            ti, to = _tokens(getattr(e, 'meta', None) or {})
        out['in_tokens'] += ti
        out['out_tokens'] += to
        if price_model:
            out['cost_usd'] += llm_advisor.claude_cost_usd(price_model, ti, to)
        out['lat_ms'].append(int((clock() - t0) * 1000))
        if dec is None:
            continue
        out['answered'] += 1
        action, rule = llm_decide.apply_hard_rules(kind, dec['action'], facts)
        if rule:
            out['blocks'].append('%s:%s->%s:%s' % (ref, dec['action'], action, rule))
        if rules:
            pair = '%s->%s' % (rules, action)
            out['pairs'][pair] = out['pairs'].get(pair, 0) + 1
            out['rules_n'] += 1
            if norm(kind, action) == norm(kind, rules):
                out['rules_agree'] += 1
            else:
                out['disagree_ids'].append(ref)
        if human:
            out['human_n'] += 1
            out['human_agree'] += norm(kind, action) == norm(kind, human)
    return out


def verdict(kind, res):
    """('PASS'|'FAIL'|'INCONCLUSIVE', [reasons]) against the pass bar. Blocks and disagreements still need a read."""
    why = []
    if res['total'] < MIN_ROWS:
        return 'INCONCLUSIVE', ['only %d rows (need %d)' % (res['total'], MIN_ROWS)]
    if res.get('stopped_budget'):
        return 'INCONCLUSIVE', ['stopped at the --max-usd guard after %d of %d rows' % (res['total'], res.get('rows_available', res['total']))]
    ans = pct(res['answered'], res['total'])
    if ans < MIN_ANSWERED_PCT:
        why.append('answered %.1f%% < %.0f%%' % (ans, MIN_ANSWERED_PCT))
    if kind in AGREE_KINDS and res['rules_n']:
        a = pct(res['rules_agree'], res['rules_n'])
        if a < MIN_AGREE_PCT:
            why.append('rules agreement %.1f%% < %.0f%%' % (a, MIN_AGREE_PCT))
    if res['human_n']:
        h = pct(res['human_agree'], res['human_n'])
        if h < MIN_AGREE_PCT:
            why.append('human agreement %.1f%% < %.0f%%' % (h, MIN_AGREE_PCT))
    return ('FAIL' if why else 'PASS'), why


def report(kind, model, res, waited_min=0.0):
    v, why = verdict(kind, res)
    lat = res['lat_ms']
    lines = ['model=%s kind=%s' % (model, kind),
             'answered=%d/%d (%s%%)' % (res['answered'], res['total'], pct(res['answered'], res['total'])),
             'rules_agree=%d/%d (%s%%)%s' % (res['rules_agree'], res['rules_n'], pct(res['rules_agree'], res['rules_n']),
                                             '' if kind in AGREE_KINDS else ' [not scored: rules always ask/wait]'),
             'human_agree=%d/%d (%s%%)' % (res['human_agree'], res['human_n'], pct(res['human_agree'], res['human_n'])),
             'hard_rule_blocks=%d' % len(res['blocks'])]
    lines += ['  block ' + b for b in res['blocks']]
    lines.append('rules_disagree_ids=' + (','.join(res['disagree_ids']) or '-'))
    lines += ['  pair %s %d' % (p, n) for p, n in sorted(res['pairs'].items())]
    lines.append('invalid=%d errors=%d' % (res.get('invalid', 0), res.get('errors', 0)))
    if res.get('first_error'):
        lines.append('first_error=' + res['first_error'])
    lines.append('tokens in=%d out=%d cost_usd=%.4f' % (res.get('in_tokens', 0), res.get('out_tokens', 0),
                                                       res.get('cost_usd', 0.0)))
    lines.append('latency_ms p50=%s p95=%s' % (percentile(lat, 50), percentile(lat, 95)))
    lines.append('gpu_wait_min=%.1f' % waited_min)
    lines.append('verdict=%s%s' % (v, (' (' + '; '.join(why) + ')') if why else ''))
    if res['blocks'] or res['disagree_ids']:
        lines.append('read every block and disagreement before approving')
    return lines


def wait_for_gpu(env, http, wait_min, sleep=time.sleep, clock=time.monotonic, poll_s=30.0):
    """(free, waited_min). Polls the hold check until it clears or wait_min passes."""
    t0 = clock()
    while True:
        held, _ = llm_advisor.hold_active(env, http)
        waited = (clock() - t0) / 60.0
        if not held:
            return True, waited
        if waited >= wait_min:
            return False, waited
        sleep(poll_s)


# ── --layer unraid ───────────────────────────────────────────────────────────

GPU_POLL_S = 30.0
PASS_REASONS = ('unsure', 'weak', 'invalid', 'error', 'timeout')


def unraid_model_listed(base, model, http):
    """(True, None) when GET {base}/api/tags lists model, else (False, reason). Never pulls."""
    try:
        r = http.get(base.rstrip('/') + '/api/tags', timeout=(2, 5))
        r.raise_for_status()
        tags = {llm_advisor._model_tag(m.get('model') or m.get('name')) for m in (r.json().get('models') or [])
                if isinstance(m, dict)}
    except Exception:  # noqa: BLE001
        return False, 'tags-unreadable'
    if llm_advisor._model_tag(model) not in tags:
        return False, 'model-not-listed'
    return True, None


def score_unraid(kind, rows, chat, gate, state, budget, sleep=time.sleep, clock=time.monotonic):
    """Replay rows through the Unraid layer the way run_chain keeps answers. Before every row gate() must return None
    (GPU free for this model); any other answer (hold, other-model-resident, backoff, probe error) waits GPU_POLL_S
    and asks again, drawing on budget['left_s'] shared across the whole run. When it is spent the run stops
    (stopped_gpu=True) with no request sent. Counts, ids and latencies only."""
    out = {'kind': kind, 'total': 0, 'kept': 0, 'passed': dict.fromkeys(PASS_REASONS, 0), 'rules_n': 0,
           'rules_agree': 0, 'human_n': 0, 'human_agree': 0, 'blocks': [], 'disagree_ids': [], 'pairs': {},
           'lat_ms': [], 'cold': 0, 'stopped_gpu': False, 'rows_available': len(rows)}
    decisive = llm_advisor.UNRAID_DECISIVE.get(kind, ())
    for i, row in enumerate(rows, 1):
        while True:
            try:
                skip = gate()
            except Exception:  # noqa: BLE001
                skip = 'error'
            if skip is None:
                break
            if budget['left_s'] < GPU_POLL_S:
                out['stopped_gpu'] = True
                return out
            budget['left_s'] -= GPU_POLL_S
            budget['waited_s'] += GPU_POLL_S
            sleep(GPU_POLL_S)
        facts, rules, human, rid = _split(row)
        ref = '#%d' % i + ('/' + rid if rid else '')
        out['total'] += 1
        out['cold'] += bool(state.get('cold'))
        t0 = clock()
        dec, reason = None, None
        try:
            dec = llm_decide.parse_decision(kind, chat(llm_decide.build_prompt(kind, facts)))
            if dec is None:
                reason = 'invalid'
        except Exception as e:  # noqa: BLE001
            reason = 'timeout' if 'timeout' in type(e).__name__.lower() else 'error'
        out['lat_ms'].append(int((clock() - t0) * 1000))
        if dec is not None:
            action, rule = llm_decide.apply_hard_rules(kind, dec['action'], facts)
            if rule:
                out['blocks'].append('%s:%s->%s:%s' % (ref, dec['action'], action, rule))
            if action not in decisive:
                reason = 'unsure'
            elif dec['confidence'] < llm_decide.MIN_CONFIDENCE:
                reason = 'weak'
        if reason:
            out['passed'][reason] += 1
            continue
        out['kept'] += 1
        if rules:
            pair = '%s->%s' % (rules, action)
            out['pairs'][pair] = out['pairs'].get(pair, 0) + 1
            out['rules_n'] += 1
            if norm(kind, action) == norm(kind, rules):
                out['rules_agree'] += 1
            else:
                out['disagree_ids'].append(ref)
        if human:
            out['human_n'] += 1
            out['human_agree'] += norm(kind, action) == norm(kind, human)
    return out


def verdict_unraid(kind, res):
    """INCONCLUSIVE when the run stopped, has too few rows or kept nothing; FAIL when kept agreement is under the bar;
    else PASS. No answered-% bar: passing on is by design."""
    if res.get('stopped_gpu'):
        return 'INCONCLUSIVE', ['GPU busy']
    if res['total'] < MIN_ROWS:
        return 'INCONCLUSIVE', ['only %d rows (need %d)' % (res['total'], MIN_ROWS)]
    if res['kept'] == 0:
        return 'INCONCLUSIVE', ['nothing kept']
    why = []
    if kind in AGREE_KINDS and res['rules_n']:
        a = pct(res['rules_agree'], res['rules_n'])
        if a < MIN_AGREE_PCT:
            why.append('rules agreement %.1f%% < %.0f%% (kept answers)' % (a, MIN_AGREE_PCT))
    if res['human_n']:
        h = pct(res['human_agree'], res['human_n'])
        if h < MIN_AGREE_PCT:
            why.append('human agreement %.1f%% < %.0f%% (kept answers)' % (h, MIN_AGREE_PCT))
    return ('FAIL' if why else 'PASS'), why


def report_unraid(kind, model, res, waited_min=0.0):
    v, why = verdict_unraid(kind, res)
    lat = res['lat_ms']
    lines = ['model=%s kind=%s' % (model, kind),
             'kept=%d/%d (%s%%)' % (res['kept'], res['total'], pct(res['kept'], res['total'])),
             'passed_on=%d (%s)' % (sum(res['passed'].values()), ' '.join('%s=%d' % (r, res['passed'][r]) for r in PASS_REASONS)),
             'rules_agree=%d/%d (%s%%)%s' % (res['rules_agree'], res['rules_n'], pct(res['rules_agree'], res['rules_n']),
                                             '' if kind in AGREE_KINDS else ' [not scored: rules always ask/wait]'),
             'human_agree=%d/%d (%s%%)' % (res['human_agree'], res['human_n'], pct(res['human_agree'], res['human_n'])),
             'hard_rule_blocks=%d' % len(res['blocks'])]
    lines += ['  block ' + b for b in res['blocks']]
    lines.append('rules_disagree_ids=' + (','.join(res['disagree_ids']) or '-'))
    lines += ['  pair %s %d' % (p, n) for p, n in sorted(res['pairs'].items())]
    lines.append('latency_ms p50=%s p95=%s' % (percentile(lat, 50), percentile(lat, 95)))
    lines.append('cold_rows=%d' % res['cold'])
    lines.append('gpu_wait_min=%.1f' % waited_min)
    lines.append('verdict=%s%s' % (v, (' (' + '; '.join(why) + ')') if why else ''))
    if res['blocks'] or res['disagree_ids']:
        lines.append('read every block and disagreement before approving')
    return lines


def _run_unraid(kinds, loaded, args, env, http, base, model, sleep):
    """Run each loaded kind through the Unraid layer; returns the exit code."""
    env2 = dict(env)
    env2['LLM_UNRAID_BACKOFF_MIN'] = '0'    # one timeout must not stall the rest of the run
    try:
        cold_s = float(env.get('LLM_UNRAID_COLD_TIMEOUT') or llm_advisor.UNRAID_COLD_TIMEOUT)
    except (TypeError, ValueError):
        cold_s = llm_advisor.UNRAID_COLD_TIMEOUT
    budget = {'left_s': args.wait_min * 60.0, 'waited_s': 0.0}
    worst = 0
    for kind in kinds:
        state = {}
        chat = llm_advisor._ollama_chat_fn(base, model, http, state=state, env=env2,
                                           cold_total=cold_s if kind in llm_advisor.UNRAID_COLD_KINDS else None)
        gate = llm_advisor._unraid_gate(base, model, env2, http, state)
        res = score_unraid(kind, loaded[kind], chat, gate, state, budget, sleep)
        print('\n'.join(report_unraid(kind, model, res, budget['waited_s'] / 60.0)))
        v = verdict_unraid(kind, res)[0]
        worst = max(worst, 0 if v == 'PASS' else 1)
        if res['stopped_gpu']:
            print('stopped: GPU busy, wait budget spent; later kinds not run')
            return 3
    return worst


def _kinds(arg):
    names = [k.strip() for k in arg.split(',') if k.strip()]
    if names == ['all']:
        return sorted(llm_decide.KINDS)
    bad = [k for k in names if k not in llm_decide.KINDS]
    return None if bad or not names else names


def default_facts(kind, env):
    """Pick the facts file for a kind: plain, else live, else newest stamped. Returns (path, label); never a listing."""
    d = env.get('LLM_BACKTEST_DIR') or '/data/backtest'
    plain = os.path.join(d, '%s.jsonl' % kind)
    if os.path.exists(plain):
        return plain, 'plain'
    live = os.path.join(d, '%s-live.jsonl' % kind)
    if os.path.exists(live):
        return live, 'live'
    try:
        stamped = sorted(n for n in os.listdir(d) if re.fullmatch(re.escape(kind) + r'-\d{8}\.jsonl', n))
    except OSError:
        stamped = []
    if stamped:
        return os.path.join(d, stamped[-1]), 'stamped'
    return plain, 'plain'


def main(argv=None, env=None, http=None, transport=None, sleep=time.sleep):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--kind', required=True, help='a kind, a comma list, or all')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--facts', default='')
    ap.add_argument('--layer', choices=('local', 'claude', 'unraid'), default='local')
    ap.add_argument('--model', default='', help='local: one of the listed ids (default the first listed); '
                                                'claude: the model id (default LLM_CLAUDE_MODEL or the layer default); '
                                                'unraid: a model /api/tags lists (default LLM_UNRAID_MODEL or the layer default)')
    ap.add_argument('--max-usd', type=float, default=2.0, help='stop before a claude run passes this many dollars')
    ap.add_argument('--wait-min', type=float, default=50.0)
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args(argv)
    kinds = _kinds(args.kind)
    if kinds is None:
        print('refused: unknown kind')
        return 2
    if args.facts and len(kinds) > 1:
        print('refused: --facts names one file, so it needs one --kind')
        return 2
    claude = args.layer == 'claude'
    unraid = args.layer == 'unraid'
    waited = 0.0
    if unraid:
        base = (env.get('UNRAID_OLLAMA_URL') or '').strip()
        if not base:
            print('refused: UNRAID_OLLAMA_URL not set')
            return 2
        model = args.model.strip() or (env.get('LLM_UNRAID_MODEL') or '').strip() or llm_advisor.UNRAID_MODEL
        if http is None:
            import requests as http
        ok, why = unraid_model_listed(base, model, http)
        if not ok:
            print('refused: %s (no request sent; a back-test never pulls a model)' % why)
            return 2
        ukinds = {k.strip() for k in (env.get('LLM_UNRAID_KINDS') or llm_advisor.UNRAID_KINDS).split(',') if k.strip()}
    elif claude:
        model = args.model.strip() or (env.get('LLM_CLAUDE_MODEL') or '').strip() or llm_advisor.CLAUDE_MODEL
        if transport is None and not (env.get('ANTHROPIC_API_KEY') or '').strip():
            print('refused: ANTHROPIC_API_KEY not set')
            return 2
        factory = llm_advisor._real_claude_client((env.get('ANTHROPIC_API_KEY') or '').strip(),
                                                  (env.get('ANTHROPIC_WORKSPACE_ID') or '').strip())
    else:
        base = (env.get('DARKBLOOM_BASE_URL') or '').strip()
        if not base:
            print('refused: DARKBLOOM_BASE_URL not set')
            return 2
        if http is None:
            import requests as http
        key = env.get('DARKBLOOM_API_KEY')
        model, why = pick_model(base, key, http, args.model.strip() or None, (env.get('LLM_LOCAL_MODEL') or '').strip() or None)
        if why:
            print('refused: %s (no request sent; a back-test never loads or switches a model)' % why)
            return 2
    print('model=%s layer=%s' % (model, args.layer))
    loaded = {}
    for kind in kinds:
        if unraid and kind not in ukinds:
            print('kind=%s skipped: not an Unraid kind' % kind)
            continue
        path = args.facts
        if not path:
            path, which = default_facts(kind, env)
            print('kind=%s facts: %s' % (kind, which))
        try:
            loaded[kind] = load_rows(path, args.limit or None)
        except OSError:
            if len(kinds) == 1:
                print('refused: facts file not readable')
                return 2
            print('kind=%s skipped: no facts file' % kind)
    kinds = [k for k in kinds if k in loaded]
    if not kinds:
        print('refused: no kind left to run')
        return 2
    if unraid:
        return _run_unraid(kinds, loaded, args, env, http, base, model, sleep)
    if not claude:
        free, waited = wait_for_gpu(env, http, args.wait_min, sleep)
        if not free:
            print('gpu_wait_min=%.1f\nverdict=INCONCLUSIVE (GPU still held)' % waited)
            return 3
    spent, worst = 0.0, 0
    for kind in kinds:
        tr = transport
        if tr is None:
            if claude:
                tr = llm_advisor._claude_fn(kind, model, factory)
            else:
                tr = llm_advisor._chat_fn(llm_advisor._chat_url(base), lambda: model, key, http, args.timeout, 2.0,
                                          {'chat_template_kwargs': {'enable_thinking': False}})
        res = score(kind, loaded[kind], tr, price_model=model if claude else None,
                    max_usd=args.max_usd if claude else None, spent_usd=spent)
        spent += res['cost_usd']
        print('\n'.join(report(kind, model, res, waited)))
        v = verdict(kind, res)[0]
        worst = max(worst, 0 if v == 'PASS' else 1)
        if res['stopped_budget']:
            print('stopped: --max-usd %.2f reached (spent %.4f); later kinds not run' % (args.max_usd, spent))
            break
    if claude and len(kinds) > 1:
        print('total_cost_usd=%.4f' % spent)
    return worst


if __name__ == '__main__':
    sys.exit(main())
