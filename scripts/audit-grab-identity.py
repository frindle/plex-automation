#!/usr/bin/env python3
"""Offline, read-only back-test of the grab-time identity rules.

Usage: audit-grab-identity.py identity-history.json > audit.json

Input: {"rows": [{date, item_title, item_year, release_title, service, source}, ...]}.
Each row becomes the same facts the live grab check builds (arr-webhook._grab_identity_facts), then
llm_decide.apply_hard_rules('import_identity', 'match', facts). No network, no model.
"""
import importlib
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def main(path):
    aw = importlib.import_module('arr-webhook')
    import llm_decide
    with open(path, encoding='utf-8') as fh:
        rows = json.load(fh).get('rows') or []
    counts = defaultdict(lambda: defaultdict(int))
    flagged, total = [], defaultdict(int)
    for r in rows:
        service = r.get('service') or ''
        total[service] += 1
        payload = {'release': {'releaseTitle': r.get('release_title')}}
        item = {'title': r.get('item_title'), 'year': r.get('item_year')}
        payload['series' if service == 'Sonarr' else 'movie'] = item
        facts, _ = aw._grab_identity_facts(payload, service)
        if not facts:
            counts[service]['unusable_row'] += 1
            continue
        action, rule = llm_decide.apply_hard_rules('import_identity', 'match', facts)
        if action != 'match':
            counts[service][rule] += 1
            flagged.append({'service': service, 'item_title': r.get('item_title'),
                            'release_title': r.get('release_title'), 'rule': rule, 'date': r.get('date')})
    json.dump({'rows': len(rows), 'rows_by_service': dict(total),
               'rule_counts_by_service': {k: dict(v) for k, v in counts.items()},
               'flagged_count': len(flagged), 'flagged': flagged}, sys.stdout, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
