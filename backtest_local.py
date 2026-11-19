#!/usr/bin/env python3
"""Back-test one decision kind through the local Darkbloom layer only, against whatever model is loaded.

usage: python backtest_local.py --kind K [--limit N] [--facts FILE.jsonl]

Never a switch: the model is the first id GET {DARKBLOOM_BASE_URL}/v1/models lists (printed first). --model may only
name a listed id; anything else, an unreadable or empty list, or an LLM_LOCAL_MODEL pin that is not listed, refuses
to run (no request is sent), so the run can never make Darkbloom load a model. Approval is not checked: this run is
what earns a model its LLM_LOCAL_APPROVED_<KIND> entry.

Facts: JSONL, default /data/backtest/<kind>.jsonl. A row is a facts dict, or {"facts": {...}, "id": "...",
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


def score(kind, rows, transport, clock=time.monotonic):
    """Replay rows through transport. Returns counts, hard-rule blocks and latencies; nothing from the facts."""
    out = {'kind': kind, 'total': 0, 'answered': 0, 'rules_n': 0, 'rules_agree': 0, 'human_n': 0, 'human_agree': 0,
           'blocks': [], 'disagree_ids': [], 'pairs': {}, 'lat_ms': []}
    for i, row in enumerate(rows, 1):
        facts, rules, human, rid = _split(row)
        ref = '#%d' % i + ('/' + rid if rid else '')
        out['total'] += 1
        t0 = clock()
        try:
            dec = llm_decide.parse_decision(kind, transport(llm_decide.build_prompt(kind, facts)))
        except Exception:  # noqa: BLE001
            dec = None
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


def main(argv=None, env=None, http=None, transport=None, sleep=time.sleep):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--kind', required=True, choices=sorted(llm_decide.KINDS))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--facts', default='')
    ap.add_argument('--model', default='', help='must be one of the listed ids; default the first listed')
    ap.add_argument('--wait-min', type=float, default=50.0)
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args(argv)
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
    print('model=%s' % model)
    path = args.facts or '/data/backtest/%s.jsonl' % args.kind
    try:
        rows = load_rows(path, args.limit or None)
    except OSError:
        print('refused: facts file not readable')
        return 2
    free, waited = wait_for_gpu(env, http, args.wait_min, sleep)
    if not free:
        print('gpu_wait_min=%.1f\nverdict=INCONCLUSIVE (GPU still held)' % waited)
        return 3
    if transport is None:
        transport = llm_advisor._chat_fn(llm_advisor._chat_url(base), lambda: model, key, http, args.timeout, 2.0,
                                         {'chat_template_kwargs': {'enable_thinking': False}})
    res = score(args.kind, rows, transport)
    print('\n'.join(report(args.kind, model, res, waited)))
    return 0 if verdict(args.kind, res)[0] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
