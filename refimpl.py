#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority-s1-is-recent-year

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Adds `_is_recent_year` to arr-webhook.py immediately before the existing
`sort_ids_by_year_desc` helper (both live in the yearly-upgrade area), and
preserves everything else already in the file.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

ANCHOR = "def sort_ids_by_year_desc(items):"
assert ANCHOR in t, "refimpl anchor not found -- did the target change?"
if "_is_recent_year" in t:
    print("refimpl already applied; nothing to do")
    sys.exit(0)

NEW = r'''# How far back (whole years) a release year still counts as "recent" for
# upgrade-priority ordering. A movie/series from this many years ago or newer
# is recent; anything older is not.
RECENT_YEAR_WINDOW = 3

def _is_recent_year(year, now=None):
    """True when `year` falls within RECENT_YEAR_WINDOW whole years of `now`.

    `now` defaults to datetime.now() but may be injected (any object with a
    `.year`) so callers and tests can pin the clock. Non-integer or missing
    years are not recent -- this helper must never raise on bad input, since
    it runs over untrusted *arr catalog data."""
    if now is None:
        now = datetime.now()
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False
    return 0 <= now.year - y <= RECENT_YEAR_WINDOW


'''

t = t.replace(ANCHOR, NEW + ANCHOR, 1)
p.write_text(t)
print("refimpl applied")
