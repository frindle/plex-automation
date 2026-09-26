"""Adversarial fixture for: arr-webhook-recent-upgrade-priority-s1-is-recent-year

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

Each case: (description, callable_returning_actual, expected)
"""
import sys
from datetime import datetime
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)

NOW = datetime(2026, 6, 15)
W = getattr(target, 'RECENT_YEAR_WINDOW', None) or 3


def _years_ago(n):
    """Year n whole years before the pinned clock."""
    return NOW.year - n



CASES = [
    # current year is recent
    ("current year is recent", lambda: target._is_recent_year(NOW.year, NOW), True),
    # one year back is recent (window is >= 1 by spec)
    ("one year back is recent", lambda: target._is_recent_year(_years_ago(1), NOW), True),
    # exact window boundary (W years ago) IS still recent -- a `<` instead of
    # `<=` fails this
    ("exact window boundary is recent", lambda: target._is_recent_year(_years_ago(W), NOW), True),
    # one year past the boundary is NOT recent -- an off-by-one in the other
    # direction (or no upper bound) fails this
    ("one year past the boundary is not recent", lambda: target._is_recent_year(_years_ago(W + 1), NOW), False),
    # far-past year is not recent
    ("far-past year is not recent", lambda: target._is_recent_year(1998, NOW), False),
    # future years are not "recent" -- an implementation that only checks
    # `year >= now.year - window` (no upper bound) fails this
    ("future year is not recent", lambda: target._is_recent_year(2030, NOW), False),
    # degenerate inputs must return False, never raise
    ("None year returns False without raising", lambda: target._is_recent_year(None, NOW), False),
    ("non-numeric string year returns False without raising", lambda: target._is_recent_year("not-a-year", NOW), False),
    ("empty string year returns False without raising", lambda: target._is_recent_year("", NOW), False),
    # now=None must default to the real clock and still work
    ("now defaults to the real clock (current year)", lambda: target._is_recent_year(datetime.now().year), True),
    ("now defaults to the real clock (old year)", lambda: target._is_recent_year(1985), False),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
