#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s4-year-sort

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

# ADDITIVE: insert the new function just before the __main__ block, preserving
# everything already in the file (prior slices landed work here).
OLD = r"""if __name__ == '__main__':
    port = int(os.environ.get('PORT', 9876))"""

NEW = r'''def sort_ids_by_year_desc(items):
    """Return items sorted by effective release year, descending.

    Effective year: the item's 'year' value when truthy; otherwise the first
    four characters of its 'firstAired' string parsed as an int when those are
    digits; otherwise 0. Zero-effective-year items sort LAST (after every real
    year). Ties within the same effective year preserve original input order
    (stable sort). The input list is not mutated."""
    def _eff(item):
        y = item.get('year') if isinstance(item, dict) else None
        if y:
            return int(y)
        fa = item.get('firstAired') if isinstance(item, dict) else None
        if isinstance(fa, str) and len(fa) >= 4 and fa[:4].isdigit():
            return int(fa[:4])
        return 0

    def _key(item):
        y = _eff(item)
        # group 1 (zero-year) sorts after every real year; within a group,
        # -y gives descending order. sorted() is stable, so ties keep input order.
        return (1 if y == 0 else 0, -y)

    return sorted(items, key=_key)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 9876))'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
