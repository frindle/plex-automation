"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s10-shared-weekly-quota

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
from datetime import datetime, timedelta, timezone
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

# Real naive-UTC clock at fixture start: weekly_quota_state tests pass this as
# an explicit `now` (deterministic), and record_upgrades_found's default clock
# is asserted loosely against it (it ticks a few ms past NOW).
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _fresh(now=NOW):
    return {'count': 0, 'week_start': now.isoformat()}


def test_quota_constant():
    expected = int(os.environ.get('WEEKLY_UPGRADE_QUOTA', '10'))
    assert isinstance(target.WEEKLY_UPGRADE_QUOTA, int)
    assert target.WEEKLY_UPGRADE_QUOTA == expected


def test_absent_key_defaults_fresh_and_does_not_mutate():
    state = {'radarr': {'last_run': NOW.isoformat()}}
    got = target.weekly_quota_state(state, NOW)
    assert got == _fresh(NOW)
    # must not have written a 'quota' key into the passed-in state
    assert 'quota' not in state


def test_valid_entry_within_window_returned_unchanged():
    entry = {'count': 4, 'week_start': (NOW - timedelta(days=6)).isoformat()}
    state = {'quota': dict(entry)}
    got = target.weekly_quota_state(state, NOW)
    assert got == entry


def test_boundary_exactly_seven_days_is_expired():
    entry = {'count': 9, 'week_start': (NOW - timedelta(days=7)).isoformat()}
    state = {'quota': dict(entry)}
    got = target.weekly_quota_state(state, NOW)
    assert got == _fresh(NOW)          # fresh window at the boundary
    assert state['quota'] == entry     # ...and the old entry was NOT mutated


def test_just_under_seven_days_still_valid():
    entry = {'count': 9, 'week_start': (NOW - timedelta(days=6, hours=23)).isoformat()}
    got = target.weekly_quota_state({'quota': dict(entry)}, NOW)
    assert got == entry


def test_unparseable_week_start_is_expired_not_raised():
    state = {'quota': {'count': 5, 'week_start': 'not-a-date'}}
    got = target.weekly_quota_state(state, NOW)
    assert got == _fresh(NOW)
    assert state['quota'] == {'count': 5, 'week_start': 'not-a-date'}


def test_record_adds_n_and_persists():
    saves = []
    orig_save = target._save_upgrade_state
    target._save_upgrade_state = lambda s: saves.append(dict(s))
    try:
        state = {'quota': {'count': 3, 'week_start': (NOW - timedelta(days=1)).isoformat()}}
        target.record_upgrades_found(state, 5)
    finally:
        target._save_upgrade_state = orig_save
    assert state['quota']['count'] == 8
    assert len(saves) == 1 and saves[0]['quota']['count'] == 8


def test_record_does_not_enforce_cap():
    # a plausible wrong fix clamps to WEEKLY_UPGRADE_QUOTA; the spec says it
    # does NOT -- callers check remaining capacity themselves.
    state = {'quota': {'count': target.WEEKLY_UPGRADE_QUOTA - 1,
                       'week_start': (NOW - timedelta(days=1)).isoformat()}}
    target.record_upgrades_found(state, 3)
    assert state['quota']['count'] == target.WEEKLY_UPGRADE_QUOTA + 2


def test_record_with_expired_window_starts_fresh():
    state = {'quota': {'count': 99, 'week_start': (NOW - timedelta(days=8)).isoformat()}}
    target.record_upgrades_found(state, 2)
    assert state['quota']['count'] == 2
    # fresh window stamped with the real clock: within a second of NOW
    delta = abs((datetime.fromisoformat(state['quota']['week_start']) - NOW).total_seconds())
    assert delta < 5


CASES = [
    ("WEEKLY_UPGRADE_QUOTA is an int read from env with default 10", test_quota_constant, None),
    ("absent 'quota' key -> fresh {'count': 0} entry, state not mutated", test_absent_key_defaults_fresh_and_does_not_mutate, None),
    ("valid entry within the 7-day window returned unchanged", test_valid_entry_within_window_returned_unchanged, None),
    ("boundary: exactly 7 days elapsed -> fresh window, old entry untouched", test_boundary_exactly_seven_days_is_expired, None),
    ("just under 7 days elapsed -> existing entry still valid", test_just_under_seven_days_still_valid, None),
    ("unparseable week_start -> expired (fresh), no exception raised", test_unparseable_week_start_is_expired_not_raised, None),
    ("record_upgrades_found adds n to count and persists via _save_upgrade_state", test_record_adds_n_and_persists, None),
    ("record_upgrades_found does NOT clamp at the cap (callers check capacity)", test_record_does_not_enforce_cap, None),
    ("record on an expired window starts a fresh count", test_record_with_expired_window_starts_fresh, None),
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
