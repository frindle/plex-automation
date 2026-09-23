#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s3-state-save

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Adds _save_upgrade_state next to the existing state-persistence helpers in
arr-webhook.py, mirroring the exact save-with-exception-handling idiom of
_save_seed_state. Everything else in the file is preserved untouched.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''def _save_seed_state(state):'''
NEW = r'''def _save_upgrade_state(state):
    try:
        with open(UPGRADE_STATE_PATH, 'w') as f:
            _json.dump(state, f)
    except Exception as e:
        log.warning(f'[upgrade-batches] failed to persist state: {e}')

def _save_seed_state(state):'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
assert 'def _save_upgrade_state' not in t, "_save_upgrade_state already present"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
