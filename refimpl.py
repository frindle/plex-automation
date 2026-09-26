#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority-s1b-recent-year-window

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

OLD = r'''RECENT_YEAR_WINDOW = 3


def _is_recent_year(year, now=None):
    if now is None:
        now = datetime.now()
    try:
        y = int(str(year).strip())
    except (TypeError, ValueError):
        return False
    return now.year - RECENT_YEAR_WINDOW <= y <= now.year'''

NEW = r'''RECENT_YEAR_WINDOW = 1


def _is_recent_year(year, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        y = int(str(year).strip())
    except (TypeError, ValueError):
        return False
    return y >= now.year - RECENT_YEAR_WINDOW'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
