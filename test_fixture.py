"""Adversarial fixture for: arr-webhook-recent-upgrade-priority-s1b-recent-year-window

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


class _Clock:
    """Injected clock: any object with `.year` is a valid `now`."""
    def __init__(self, year):
        self.year = year


NOW = 2026
clock = lambda y: _Clock(y)

CASES = [
    # --- the boundary the defect is about (window must be exactly 1) --------
    ("year two before now.year is NOT recent (landed window=3 says recent)",
     lambda: target._is_recent_year(NOW - 2, clock(NOW)), False),
    ("year one after now.year IS recent (landed upper bound says not)",
     lambda: target._is_recent_year(NOW + 1, clock(NOW)), True),
    # --- the contract boundary itself ---------------------------------------
    ("now.year is recent",
     lambda: target._is_recent_year(NOW, clock(NOW)), True),
    ("now.year - 1 is recent (the one-year lookback)",
     lambda: target._is_recent_year(NOW - 1, clock(NOW)), True),
    # --- degenerate inputs must return False WITHOUT raising -----------------
    ("None returns False without raising",
     lambda: target._is_recent_year(None, clock(NOW)), False),
    ("empty string returns False without raising",
     lambda: target._is_recent_year("", clock(NOW)), False),
    ("0 returns False without raising",
     lambda: target._is_recent_year(0, clock(NOW)), False),
    ("non-numeric garbage returns False without raising",
     lambda: target._is_recent_year("not-a-year", clock(NOW)), False),
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
