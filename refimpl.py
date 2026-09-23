#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s2-state-load

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Adds UPGRADE_STATE_PATH + _load_upgrade_state next to _load_seed_state,
preserving everything already in arr-webhook.py.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''def _load_seed_state():'''
NEW = r'''UPGRADE_STATE_PATH = os.environ.get('UPGRADE_STATE_PATH', '/data/upgrade_batch_state.json')

def _load_upgrade_state():
    try:
        with open(UPGRADE_STATE_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def _load_seed_state():'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
