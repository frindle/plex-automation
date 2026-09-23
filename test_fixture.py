"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s1-batch-constants

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
import os
import sys
import importlib.util


def _load(env):
    """Fresh module load with UPGRADE_BATCH_* env vars set/unset per `env`."""
    saved = {}
    for k in ('UPGRADE_BATCH_SIZE', 'UPGRADE_BATCH_INTERVAL_DAYS'):
        saved[k] = os.environ.pop(k, None)
    for k, v in (env or {}).items():
        if v is not None:
            os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
        target = importlib.util.module_from_spec(spec)
        # REGISTER BEFORE EXEC. Not optional: a module loaded this way has no
        # entry in sys.modules, so sys.modules[cls.__module__] is None -- and on
        # Python 3.14 (the Studio worker) dataclasses resolves string annotations
        # through exactly that lookup. A target with `from __future__ import
        # annotations` + @dataclass then dies at IMPORT with AttributeError:
        # 'NoneType' object has no attribute '__dict__', so the fixture fails for
        # a reason that has nothing to do with the task and the dispatch reads as
        # a model failure.
        sys.modules["target"] = target
        spec.loader.exec_module(target)
        return target
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


CASES = [
    # Defaults when the env vars are absent.
    ("UPGRADE_BATCH_SIZE defaults to 12",
     lambda: _load(None).UPGRADE_BATCH_SIZE, 12),
    ("UPGRADE_BATCH_INTERVAL_DAYS defaults to 3",
     lambda: _load(None).UPGRADE_BATCH_INTERVAL_DAYS, 3),
    # Both must be real ints -- a plausible wrong fix leaves them as the raw
    # env strings ('12'/'3'), which would fail these.
    ("UPGRADE_BATCH_SIZE is an int (not str)",
     lambda: isinstance(_load(None).UPGRADE_BATCH_SIZE, int), True),
    ("UPGRADE_BATCH_INTERVAL_DAYS is an int (not str)",
     lambda: isinstance(_load(None).UPGRADE_BATCH_INTERVAL_DAYS, int), True),
    # Env override must be honoured and coerced.
    ("UPGRADE_BATCH_SIZE honours env override '7'",
     lambda: _load({'UPGRADE_BATCH_SIZE': '7'}).UPGRADE_BATCH_SIZE, 7),
    ("UPGRADE_BATCH_INTERVAL_DAYS honours env override '5'",
     lambda: _load({'UPGRADE_BATCH_INTERVAL_DAYS': '5'}).UPGRADE_BATCH_INTERVAL_DAYS, 5),
    # Regression half: the existing bulk-search constants keep their values.
    ("BULK_SEARCH_BATCH still defaults to 50",
     lambda: _load(None).BULK_SEARCH_BATCH, 50),
    ("BULK_SEARCH_DELAY still defaults to 180",
     lambda: _load(None).BULK_SEARCH_DELAY, 180),
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
