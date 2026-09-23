"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s5-cursor-advance

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

# Pin the batch size BEFORE the target reads it from the environment so every
# expectation below is deterministic regardless of the ambient env.
os.environ['UPGRADE_BATCH_SIZE'] = '4'

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


def _adv(cursor, total):
    return tuple(target.advance_upgrade_cursor(cursor, total))


CASES = [
    # Happy path: cursor in range, batch smaller than catalog -- no wrap.
    ("cursor 0 of 10 takes the first 4", lambda: _adv(0, 10), ([0, 1, 2, 3], 4)),

    # Wrap-around: a plausible wrong impl that stops at the end (or slices
    # without wrapping) fails this one.
    ("cursor 8 of 10 wraps past the end back to 0", lambda: _adv(8, 10), ([8, 9, 0, 1], 2)),

    # Stale cursor from a larger catalog must be clamped into range -- an impl
    # that indexes with the raw cursor raises or returns out-of-range indices.
    ("stale cursor 50 of 10 clamps to index 9", lambda: _adv(50, 10), ([9, 0, 1, 2], 3)),

    # Negative cursor must clamp down to 0, not raise or wrap weirdly.
    ("negative cursor -5 of 10 clamps to index 0", lambda: _adv(-5, 10), ([0, 1, 2, 3], 4)),

    # Catalog smaller than the batch: one full lap is exhausted before
    # UPGRADE_BATCH_SIZE indices are collected -- no duplicate indices. A wrong
    # impl that always takes exactly UPGRADE_BATCH_SIZE emits [1, 2, 0, 1].
    ("total 3 < batch 4 stops at one full lap", lambda: _adv(1, 3), ([1, 2, 0], 1)),

    # Degenerate: empty catalog returns an empty pass with cursor reset to 0.
    ("total 0 returns no indices and next cursor 0", lambda: _adv(7, 0), ([], 0)),

    # Single-item catalog: clamped cursor 0, one index, cursor stays at 0.
    ("total 1 with stale cursor takes [0] and stays at 0", lambda: _adv(5, 1), ([0], 0)),
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
