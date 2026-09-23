"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s4-year-sort

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


def _ids(items):
    return [it['id'] for it in items]


CASES = [
    # Descending by truthy 'year'; a plausible wrong fix (ascending sort, or
    # sorting by firstAired only) fails this.
    ("sorts by year descending",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 1, 'year': 2019}, {'id': 2, 'year': 2024}, {'id': 3, 'year': 2005}])),
     [2, 1, 3]),

    # Falsy/missing year falls back to first four chars of firstAired.
    ("falls back to firstAired when year is falsy or missing",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 'a', 'year': None, 'firstAired': '2018-03-01'},
          {'id': 'b', 'firstAired': '2021-11-30T00:00:00Z'},
          {'id': 'c', 'year': 0, 'firstAired': '1999-01-01'}])),
     ['b', 'a', 'c']),

    # Non-digit firstAired prefix -> effective year 0.
    ("non-digit firstAired prefix counts as zero",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 'x', 'firstAired': 'unknown'}, {'id': 'y', 'year': 2010}])),
     ['y', 'x']),

    # Zero-effective-year items sort LAST, after every real year. A wrong fix
    # that sorts zeros first (or interleaves them numerically) fails this.
    ("zero-effective-year items sort last",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 'z1'}, {'id': 'r', 'year': 2000}, {'id': 'z2', 'firstAired': 'nope'}])),
     ['r', 'z1', 'z2']),

    # Ties within the same effective year preserve original input order (stable).
    ("ties keep original input order",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 1, 'year': 2020}, {'id': 2, 'firstAired': '2020-05-05'},
          {'id': 3, 'year': 2020}])),
     [1, 2, 3]),

    # Input list must not be mutated.
    ("does not mutate the input list",
     lambda: (lambda items: (target.sort_ids_by_year_desc(items), items == [{'id': 9}, {'id': 8}]))(
         [{'id': 9}, {'id': 8}]),
     ([{'id': 9}, {'id': 8}], True)),

    # Degenerate inputs must not raise.
    ("empty list returns empty list",
     lambda: target.sort_ids_by_year_desc([]),
     []),

    # firstAired exactly 4 chars (the >= 4 boundary itself) must still parse;
    # a mutant that flips >= to > or 4 to 5 would drop this to effective year 0
    # and sort it last instead of between 2019 and 2016.
    ("firstAired of exactly 4 chars parses at the length boundary",
     lambda: _ids(target.sort_ids_by_year_desc(
         [{'id': 'p', 'year': 2019}, {'id': 'q', 'firstAired': '2018'},
          {'id': 'r', 'year': 2016}])),
     ['p', 'q', 'r']),
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
