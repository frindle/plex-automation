"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s3-state-save

Proves _save_upgrade_state exists and behaves like the codebase's other
state savers (_save_seed_state): serializes a state dict to JSON at the
module-level UPGRADE_STATE_PATH, swallows write failures with log.warning,
and never propagates an exception. A plausible-but-wrong implementation --
one that raises on failure, writes to SEED_STATE_PATH instead of
UPGRADE_STATE_PATH, or forgets the try/except -- fails at least one case.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util
import json as _json
import os
import tempfile
from unittest import mock

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


def _tmp_path():
    fd, path = tempfile.mkstemp(prefix='upgrade_state_')
    os.close(fd)
    return path


CASES = [
    ("saves the state dict as JSON at UPGRADE_STATE_PATH",
     lambda: _roundtrip({'batch': 3, 'done': ['a', 'b']}),
     True),

    ("empty state dict round-trips to {}",
     lambda: _roundtrip({}),
     True),

    ("write failure does NOT propagate -- returns None",
     lambda: _no_raise_on_bad_path(),
     True),

    ("write failure is reported via log.warning (not silent, not raised)",
     lambda: _warning_emitted_on_bad_path(),
     True),

    ("saves to UPGRADE_STATE_PATH, NOT SEED_STATE_PATH",
     lambda: _right_path_used(),
     True),

    ("regression: _save_seed_state still persists at SEED_STATE_PATH",
     lambda: _seed_roundtrip({'h1': {'first_seen_rar_at': 0}}),
     True),
]


def _roundtrip(state):
    path = _tmp_path()
    try:
        with mock.patch.object(target, 'UPGRADE_STATE_PATH', path):
            target._save_upgrade_state(state)
        return _json.load(open(path)) == state
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _seed_roundtrip(state):
    path = _tmp_path()
    try:
        with mock.patch.object(target, 'SEED_STATE_PATH', path):
            target._save_seed_state(state)
        return _json.load(open(path)) == state
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _no_raise_on_bad_path():
    # A directory that does not exist: open(..., 'w') must raise inside the
    # function; a correct impl swallows it and returns None.
    bad = os.path.join(tempfile.gettempdir(), 'no-such-dir-xyz', 'state.json')
    with mock.patch.object(target, 'UPGRADE_STATE_PATH', bad):
        ret = target._save_upgrade_state({'x': 1})
    return ret is None


def _warning_emitted_on_bad_path():
    bad = os.path.join(tempfile.gettempdir(), 'no-such-dir-xyz', 'state.json')
    with mock.patch.object(target, 'UPGRADE_STATE_PATH', bad), \
         mock.patch.object(target.log, 'warning', wraps=target.log.warning) as w:
        target._save_upgrade_state({'x': 1})
    return w.call_count == 1


def _right_path_used():
    # Both paths must start NON-existent so existence after the call is
    # meaningful evidence of which one was written.
    up = tempfile.mktemp(prefix='upgrade_state_')
    seed = tempfile.mktemp(prefix='seed_state_')
    try:
        with mock.patch.object(target, 'UPGRADE_STATE_PATH', up), \
             mock.patch.object(target, 'SEED_STATE_PATH', seed):
            target._save_upgrade_state({'k': 'v'})
        return os.path.exists(up) and not os.path.exists(seed)
    finally:
        for p in (up, seed):
            if os.path.exists(p):
                os.unlink(p)


def _seed_save_to(path, state):
    with mock.patch.object(target, 'SEED_STATE_PATH', path):
        target._save_seed_state(state)
    return path


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is a verify. Author the cases in "
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
