"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s11-scheduler-shared-quota

Drives the real app (Flask TestClient + the real scheduler function with its
external calls stubbed) and asserts the shared-quota gate behaviour:

  * exhausted quota -> poll skipped entirely, no service runs
  * available quota -> exactly one service runs per poll, alternating via the
    persisted 'next_service' key (defaulting to 'radarr')
  * monthly_upgrade_cycle routes relabel()'s integer count through
    record_upgrades_found -- so the manual /run-monthly-upgrade endpoint also
    advances the shared quota only on CONFIRMED queued upgrades

Each case: (description, callable_returning_actual, expected)
"""
import json
import os
import sys
import tempfile
import threading
import time as _time
from unittest import mock

_tmpdir = tempfile.mkdtemp(prefix='arr-webhook-fixture-')
os.environ['UPGRADE_STATE_PATH'] = os.path.join(_tmpdir, 'upgrade_batch_state.json')
os.environ.setdefault('WEEKLY_UPGRADE_QUOTA', '10')

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

STATE_PATH = os.environ['UPGRADE_STATE_PATH']


def _write_state(state):
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f)


def _read_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


class _StopPoll(Exception):
    """Sentinel raised by the mocked time.sleep to end exactly one loop iteration."""


def _run_one_poll(cycle_calls, record_calls):
    """Run exactly ONE iteration of monthly_search_scheduler's while-True body.

    The scheduler loops forever with a 3600s sleep between polls; mocking that
    sleep as a no-op would spin the loop infinitely, so the mock raises _StopPoll
    on its first call -- which lands at the end of the iteration (after the
    service ran and state was saved), giving us exactly one poll."""
    def _sleep(*a, **k):
        raise _StopPoll()

    with mock.patch.object(target.time, 'sleep', side_effect=_sleep), \
         mock.patch.object(target, 'monthly_upgrade_cycle', side_effect=lambda svc: cycle_calls.append(svc)), \
         mock.patch.object(target, 'record_upgrades_found', side_effect=lambda st, n: record_calls.append(n)):
        try:
            target.monthly_search_scheduler()
        except _StopPoll:
            pass


def _case_exhausted_quota_skips_poll():
    """Quota fully used -> poll skipped entirely; no service runs."""
    cycle_calls, record_calls = [], []
    _write_state({'quota': {'count': 10, 'week_start': target.datetime.now(target.timezone.utc).replace(tzinfo=None).isoformat()}})
    try:
        _run_one_poll(cycle_calls, record_calls)
    except Exception as e:
        return f'raised {type(e).__name__}: {e}'
    if cycle_calls != [] or record_calls != []:
        return f'services ran despite exhausted quota: cycles={cycle_calls} records={record_calls}'
    return 'ok'


def _case_exhausted_sleeps_exactly_one_hour():
    """Exhausted quota -> the poll still sleeps exactly 3600s before retrying.

    A longer/shorter cadence (e.g. a typo'd constant) would let the gate
    re-poll on the wrong schedule, so assert the exact duration."""
    _write_state({'quota': {'count': 10, 'week_start': target.datetime.now(target.timezone.utc).replace(tzinfo=None).isoformat()}})
    sleeps = []
    def _sleep(*a, **k):
        sleeps.append(a[0] if a else k.get('seconds'))
        raise _StopPoll()
    with mock.patch.object(target.time, 'sleep', side_effect=_sleep), \
         mock.patch.object(target, 'monthly_upgrade_cycle') as cycle:
        try:
            target.monthly_search_scheduler()
        except _StopPoll:
            pass
    if sleeps != [3600]:
        return f'exhausted poll must sleep exactly 3600s once, got {sleeps}'
    if cycle.call_count != 0:
        return f'cycle ran despite exhausted quota ({cycle.call_count} calls)'
    return 'ok'


def _case_exhausted_poll_runs_nothing():
    """Exhausted quota -> the poll must NOT fall through to service selection.

    No monthly_upgrade_cycle call, no next_service flip/persist: the iteration
    ends at the sleep."""
    cycle_calls = []
    _write_state({'next_service': 'sonarr',
                  'quota': {'count': 10, 'week_start': target.datetime.now(target.timezone.utc).replace(tzinfo=None).isoformat()}})
    # The FIRST sleep must return normally (not raise): if the exhausted branch
    # falls through to service selection instead of `continue`-ing back to the
    # top of the loop, that fall-through only becomes visible once control gets
    # past the first sleep. The second sleep then ends the run.
    sleeps = {'n': 0}
    def _sleep(*a, **k):
        sleeps['n'] += 1
        if sleeps['n'] >= 2:
            raise _StopPoll()
    with mock.patch.object(target.time, 'sleep', side_effect=_sleep), \
         mock.patch.object(target, 'monthly_upgrade_cycle', side_effect=lambda svc: cycle_calls.append(svc)):
        try:
            target.monthly_search_scheduler()
        except _StopPoll:
            pass
    if cycle_calls != []:
        return f'services ran despite exhausted quota: {cycle_calls}'
    st = _read_state()
    if st.get('next_service') != 'sonarr':
        return f'exhausted poll must not touch next_service (was sonarr, now {st.get("next_service")!r})'
    return 'ok'


def _case_alternation_and_default():
    """Empty state -> radarr first (default), then sonarr, alternating; one service per poll."""
    cycle_calls = []
    record_calls = []
    _write_state({})
    try:
        for _ in range(4):
            _run_one_poll(cycle_calls, record_calls)
    except Exception as e:
        return f'raised {type(e).__name__}: {e}'
    if cycle_calls != ['radarr', 'sonarr', 'radarr', 'sonarr']:
        return f'wrong alternation order: {cycle_calls}'
    st = _read_state()
    if st.get('next_service') not in ('radarr', 'sonarr'):
        return f"persisted next_service missing/invalid: {st.get('next_service')!r}"
    # odd number of polls from empty state must end on the OTHER service than it started
    _write_state({})
    cycle_calls.clear()
    for _ in range(3):
        _run_one_poll(cycle_calls, record_calls)
    if cycle_calls != ['radarr', 'sonarr', 'radarr']:
        return f'wrong 3-poll order: {cycle_calls}'
    if _read_state().get('next_service') != 'sonarr':
        return f"after 3 polls next_service should be sonarr, got {_read_state().get('next_service')!r}"
    return 'ok'


def _case_invalid_next_service_falls_back_to_radarr():
    """A corrupted/stale 'next_service' value must fall back to 'radarr', not be
    passed through to monthly_upgrade_cycle (which would run the radarr path
    silently for an unknown service name)."""
    cycle_calls = []
    _write_state({'next_service': 'radarr_X',
                  'quota': {'count': 0, 'week_start': target.datetime.now(target.timezone.utc).replace(tzinfo=None).isoformat()}})
    with mock.patch.object(target.time, 'sleep', side_effect=lambda *a, **k: (_ for _ in ()).throw(_StopPoll())), \
         mock.patch.object(target, 'monthly_upgrade_cycle', side_effect=lambda svc: cycle_calls.append(svc)):
        try:
            target.monthly_search_scheduler()
        except _StopPoll:
            pass
    if cycle_calls != ['radarr']:
        return f"invalid next_service must fall back to radarr, got {cycle_calls}"
    st = _read_state()
    if st.get('next_service') != 'sonarr':
        return f"after running the fallback service, next_service should flip to sonarr, got {st.get('next_service')!r}"
    return 'ok'


def _case_cycle_records_relabel_count():
    """monthly_upgrade_cycle must capture relabel()'s int and call record_upgrades_found with it."""
    calls = []
    with mock.patch.object(target.time, 'sleep'), \
         mock.patch.object(target, 'purge_stalled_upgrade_torrents'), \
         mock.patch.object(target, 'radarr_bulk_search'), \
         mock.patch.object(target, 'relabel_radarr_upgrades', return_value=3), \
         mock.patch.object(target, 'record_upgrades_found', side_effect=lambda st, n: calls.append(n)):
        target.monthly_upgrade_cycle('radarr', wait_before_search=0, wait_before_relabel=0)
    if calls != [3]:
        return f'record_upgrades_found not called with relabel count 3: {calls}'
    # zero-count path must still record (quota bookkeeping is explicit, not conditional)
    calls.clear()
    with mock.patch.object(target.time, 'sleep'), \
         mock.patch.object(target, 'purge_stalled_upgrade_torrents'), \
         mock.patch.object(target, 'radarr_bulk_search'), \
         mock.patch.object(target, 'relabel_radarr_upgrades', return_value=0), \
         mock.patch.object(target, 'record_upgrades_found', side_effect=lambda st, n: calls.append(n)):
        target.monthly_upgrade_cycle('radarr', wait_before_search=0, wait_before_relabel=0)
    if calls != [0]:
        return f'zero relabel count not recorded: {calls}'
    # sonarr path records its own relabel count too
    calls.clear()
    with mock.patch.object(target.time, 'sleep'), \
         mock.patch.object(target, 'purge_stalled_upgrade_torrents'), \
         mock.patch.object(target, 'sonarr_bulk_search'), \
         mock.patch.object(target, 'relabel_sonarr_upgrades', return_value=2), \
         mock.patch.object(target, 'record_upgrades_found', side_effect=lambda st, n: calls.append(n)):
        target.monthly_upgrade_cycle('sonarr', wait_before_search=0, wait_before_relabel=0)
    if calls != [2]:
        return f'sonarr relabel count not recorded: {calls}'
    return 'ok'


def _case_manual_endpoint_records_quota():
    """Integration: POST /run-monthly-upgrade (skip_waits) must still work AND route the
    relabel count through record_upgrades_found via monthly_upgrade_cycle."""
    calls = []
    # The endpoint spawns a daemon thread, so the mocks must stay active until
    # that thread has run -- wait INSIDE the patch context.
    with mock.patch.object(target.time, 'sleep'), \
         mock.patch.object(target, 'purge_stalled_upgrade_torrents'), \
         mock.patch.object(target, 'radarr_bulk_search'), \
         mock.patch.object(target, 'relabel_radarr_upgrades', return_value=4), \
         mock.patch.object(target, 'record_upgrades_found', side_effect=lambda st, n: calls.append(n)), \
         mock.patch.object(target, 'verify_and_fix_labels', return_value=[]):
        client = target.app.test_client()
        r = client.post('/run-monthly-upgrade?skip_waits=1&service=radarr')
        body = r.get_json() or {}
        deadline = _time.time() + 5
        while not calls and _time.time() < deadline:
            _time.sleep(0.1)
    if r.status_code != 200 or body.get('ok') is not True or body.get('service') != 'radarr':
        return f'endpoint response wrong: {r.status_code} {body}'
    if calls != [4]:
        return f'manual endpoint did not route relabel count through record_upgrades_found: {calls}'
    # invalid service must be a 400, not a 500
    r2 = client.post('/run-monthly-upgrade?service=bogus')
    b2 = r2.get_json() or {}
    if r2.status_code != 400 or b2.get('ok') is not False:
        return f'invalid service should be 400 ok=False, got {r2.status_code} {b2}'
    return 'ok'



CASES = [
    ("exhausted weekly quota skips the poll entirely (no service runs)", _case_exhausted_quota_skips_poll, "ok"),
    ("exhausted quota poll still sleeps exactly 3600s before retrying", _case_exhausted_sleeps_exactly_one_hour, "ok"),
    ("exhausted quota poll runs nothing and does not flip next_service", _case_exhausted_poll_runs_nothing, "ok"),
    ("one service per poll, alternating radarr/sonarr via persisted next_service (default radarr)", _case_alternation_and_default, "ok"),
    ("invalid/corrupted next_service value falls back to radarr and still flips the key", _case_invalid_next_service_falls_back_to_radarr, "ok"),
    ("monthly_upgrade_cycle records relabel()'s integer count via record_upgrades_found", _case_cycle_records_relabel_count, "ok"),
    ("manual /run-monthly-upgrade endpoint works and routes its relabel count through the shared quota", _case_manual_endpoint_records_quota, "ok"),
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
