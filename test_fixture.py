"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s2-state-load

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
import tempfile
import json as _test_json

# The constant is read from the environment at import time; make sure the
# default under test really is the default.
os.environ.pop('UPGRADE_STATE_PATH', None)

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


def _load_at(path):
    """Point UPGRADE_STATE_PATH at `path` and call _load_upgrade_state."""
    saved = target.UPGRADE_STATE_PATH
    try:
        target.UPGRADE_STATE_PATH = path
        return target._load_upgrade_state()
    finally:
        target.UPGRADE_STATE_PATH = saved


def _valid_file():
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        _test_json.dump({'batches': [{'id': 7}], 'cursor': 3}, f)
    return path


def _corrupt_file():
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        f.write('{not valid json,,')
    return path


CASES = [
    ("UPGRADE_STATE_PATH defaults to /data/upgrade_batch_state.json",
     lambda: target.UPGRADE_STATE_PATH, '/data/upgrade_batch_state.json'),

    ("valid JSON file is parsed and returned as the object",
     lambda: _load_at(_valid_file()), {'batches': [{'id': 7}], 'cursor': 3}),

    ("missing state file returns {} instead of raising FileNotFoundError",
     lambda: _load_at('/nonexistent/upgrade_batch_state.json'), {}),

    ("corrupt JSON (ValueError) returns {} instead of raising",
     lambda: _load_at(_corrupt_file()), {}),

    ("regression: _load_seed_state still loads valid JSON via the same idiom",
     lambda: (lambda p, old: (setattr(target, 'SEED_STATE_PATH', p),
                             target._load_seed_state(),
                             setattr(target, 'SEED_STATE_PATH', old))[1])(_valid_file(), '/data/seed_tracking.json'),
     {'batches': [{'id': 7}], 'cursor': 3}),
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
