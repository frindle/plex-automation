"""Replay duplicate_folder fact rows against a model endpoint and count agree/disagree with the recorded action.

usage: python backtest_duplicate_folder.py rows.jsonl --url http://HOST/v1/chat/completions --model NAME

Each JSONL row is a facts object (see arr-webhook._duplicate_folder_facts) or {"facts": {...}, "expected": "ask"}.
The comparison action is `expected`, else the facts' `rules_action`. The model answer goes through the same
hard rules as production. Prints counts only (no facts are echoed).
"""
import argparse
import collections
import json
import sys

import llm_advisor
import llm_decide


def backtest(rows, transport):
    """Return a Counter: agree, disagree, no_answer, plus 'pair:<expected>-><model>' counts."""
    out = collections.Counter()
    for row in rows:
        facts = row.get('facts') if isinstance(row.get('facts'), dict) else row
        expected = row.get('expected') if isinstance(row.get('expected'), str) else facts.get('rules_action')
        try:
            dec = llm_decide.parse_decision('duplicate_folder', transport(llm_decide.build_prompt('duplicate_folder', facts)))
        except Exception:  # noqa: BLE001
            dec = None
        if dec is None:
            out['no_answer'] += 1
            continue
        action, _ = llm_decide.apply_hard_rules('duplicate_folder', dec['action'], facts)
        out['agree' if action == expected else 'disagree'] += 1
        out['pair:%s->%s' % (expected, action)] += 1
    return out


def load_rows(path):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def main(argv=None, transport=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('rows')
    ap.add_argument('--url', default='')
    ap.add_argument('--model', default='')
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args(argv)
    if transport is None:
        if not args.url:
            ap.error('--url is required')
        transport = llm_advisor.http_transport(args.url, args.model, args.timeout)
    res = backtest(load_rows(args.rows), transport)
    print('agree=%d disagree=%d no_answer=%d' % (res['agree'], res['disagree'], res['no_answer']))
    for k in sorted(k for k in res if k.startswith('pair:')):
        print('%s %d' % (k[5:], res[k]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
