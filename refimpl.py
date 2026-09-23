#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s5-cursor-advance

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''UPGRADE_BATCH_SIZE = int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))'''
NEW = OLD + r'''


def advance_upgrade_cursor(cursor, total):
    """Advance the yearly-upgrade batch cursor by one pass.

    Returns (indices, next_cursor): up to UPGRADE_BATCH_SIZE consecutive
    indices starting at the clamped cursor, wrapping past the end back to 0,
    and where to resume on the next pass. A stale cursor from a larger catalog
    is clamped into range so it never indexes out of bounds; when total is
    zero there is nothing to search this pass.
    """
    if total <= 0:
        return [], 0
    clamped = max(0, min(cursor, total - 1))
    count = min(UPGRADE_BATCH_SIZE, total)
    indices = [(clamped + i) % total for i in range(count)]
    next_cursor = (clamped + count) % total
    return indices, next_cursor
'''

assert OLD in t and 'def advance_upgrade_cursor' not in t, \
    "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
