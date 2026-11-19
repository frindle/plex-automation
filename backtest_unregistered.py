#!/usr/bin/env python3
"""Backtest unregistered_triage: JSONL fact rows -> model answers -> agree/disagree with the rules action.

Usage: python backtest_unregistered.py FACTS.jsonl --url http://HOST:11434/api/chat [--model NAME]
Each row is a facts dict (it must carry rules_action 'remove' or 'keep'). Prints counts only; no names are logged.
"""
import argparse
import json
import sys
import urllib.request

import llm_decide


def ollama_transport(url, model, timeout=60):
    def call(prompt):
        body = json.dumps({'model': model, 'stream': False, 'think': False,
                           'messages': [{'role': 'user', 'content': prompt}]}).encode()
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310
            return (json.load(r).get('message') or {}).get('content') or ''
    return call


def backtest(rows, transport):
    """Counts of agree / disagree / ask / none. Agree: the (hard-ruled) model action matches the rules
    (keep for a keep, any remove_* for a remove). Disagree: it differs. ask and no answer are counted apart."""
    out = {'agree': 0, 'disagree': 0, 'ask': 0, 'none': 0, 'rows': 0}
    for facts in rows:
        out['rows'] += 1
        dec = llm_decide.decide('unregistered_triage', facts, transport)
        if dec is None:
            out['none'] += 1
            continue
        action, _ = llm_decide.apply_hard_rules('unregistered_triage', dec['action'], facts)
        if action == 'ask':
            out['ask'] += 1
        elif (action == 'keep') == (facts.get('rules_action') == 'keep'):
            out['agree'] += 1
        else:
            out['disagree'] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('facts')
    ap.add_argument('--url', required=True)
    ap.add_argument('--model', default='qwen3:14b')
    a = ap.parse_args(argv)
    with open(a.facts, encoding='utf-8') as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    res = backtest(rows, ollama_transport(a.url, a.model))
    print(' '.join(f'{k}={v}' for k, v in res.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
