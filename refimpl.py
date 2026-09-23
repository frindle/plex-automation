#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s8-tests

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

The target already contains sort_ids_by_year_desc, advance_upgrade_cursor,
radarr_bulk_search / sonarr_bulk_search, and POST /run-monthly-upgrade. The one
thing missing for testability is an extractable interval-gate predicate: the
due-check currently lives inline inside monthly_search_scheduler's loop body.
This refimpl extracts it into upgrade_batch_due() (same semantics) so a test
can drive "not due before UPGRADE_BATCH_INTERVAL_DAYS, due after, first-ever
poll with empty state" directly -- and pins that extraction in the scheduler.

Everything else is preserved verbatim; no existing import/function/class is
touched.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''def monthly_search_scheduler():
    """
    On the 1st of each month, Radarr first and then the identical Sonarr
    cycle (Sonarr was silently missing a lot of episodes because nothing
    ever bulk-searched it):
    1. Purge stalled <service>-upgrade torrents
    2. Wait 30 minutes
    3. Trigger bulk search
    4. Wait 5 minutes
    5. Relabel new upgrade torrents to the throttled lane, queue them last
    """
    import datetime
    while True:
        # last_run stamps are persisted in UTC (see radarr_bulk_search /
        # sonarr_bulk_search), so compare against a naive-UTC clock.
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        state = _load_upgrade_state()
        for service in ('radarr', 'sonarr'):
            entry = state.get(service) or {}
            last_run = entry.get('last_run')
            if not last_run:
                due = True  # first run: no persisted timestamp yet
            else:
                try:
                    elapsed_days = (now - datetime.datetime.fromisoformat(last_run)).total_seconds() / 86400.0
                except ValueError:
                    elapsed_days = float('inf')  # unparseable stamp -> treat as due
                due = elapsed_days >= UPGRADE_BATCH_INTERVAL_DAYS
            if due:'''

NEW = r'''def upgrade_batch_due(entry, now=None):
    """Interval gate for the yearly-upgrade batch passes.

    A service is due when it has never run (no last_run stamp -- first-ever
    poll with empty state), or when at least UPGRADE_BATCH_INTERVAL_DAYS have
    elapsed since its last_run. An unparseable stamp is treated as due rather
    than wedging the scheduler forever. `now` defaults to a naive-UTC clock;
    stamps are persisted in UTC (see radarr_bulk_search / sonarr_bulk_search).
    """
    import datetime
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_run = (entry or {}).get('last_run')
    if not last_run:
        return True  # first run: no persisted timestamp yet
    try:
        elapsed_days = (now - datetime.datetime.fromisoformat(last_run)).total_seconds() / 86400.0
    except ValueError:
        return True  # unparseable stamp -> treat as due
    return elapsed_days >= UPGRADE_BATCH_INTERVAL_DAYS

def monthly_search_scheduler():
    """
    On the 1st of each month, Radarr first and then the identical Sonarr
    cycle (Sonarr was silently missing a lot of episodes because nothing
    ever bulk-searched it):
    1. Purge stalled <service>-upgrade torrents
    2. Wait 30 minutes
    3. Trigger bulk search
    4. Wait 5 minutes
    5. Relabel new upgrade torrents to the throttled lane, queue them last
    """
    import datetime
    while True:
        # last_run stamps are persisted in UTC (see radarr_bulk_search /
        # sonarr_bulk_search), so compare against a naive-UTC clock.
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        state = _load_upgrade_state()
        for service in ('radarr', 'sonarr'):
            entry = state.get(service) or {}
            due = upgrade_batch_due(entry, now)
            if due:'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))

# The solution also lands the new test file (the INTENT's deliverable), using
# the same importlib + monkeypatch idioms as test_radarr_upgrade_label.py.
TEST_FILE = r'''"""Yearly-upgrade batch machinery: ordering, cursor walk, interval gate, and
the /run-monthly-upgrade route -- bounded one-batch-per-service passes."""
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
sys_modules_patch = __import__('sys').modules.setdefault("target", target)
spec.loader.exec_module(target)


def test_sort_ids_by_year_desc_ordering():
    items = [
        {'id': 1, 'year': 2019},
        {'id': 2, 'firstAired': '2023-05-01T00:00:00Z'},
        {'id': 3},
        {'id': 4, 'year': 2023},
        {'id': 5, 'firstAired': 'not-a-date'},
    ]
    ordered = target.sort_ids_by_year_desc(items)
    assert [i['id'] for i in ordered] == [2, 4, 1, 3, 5]
    # input list not mutated
    assert [i['id'] for i in items] == [1, 2, 3, 4, 5]


def test_sort_ids_by_year_desc_stable_ties():
    items = [{'id': 'a', 'year': 2020}, {'id': 'b', 'firstAired': '2020-01-01'},
             {'id': 'c'}]
    ordered = target.sort_ids_by_year_desc(items)
    assert [i['id'] for i in ordered] == ['a', 'b', 'c']


def test_advance_upgrade_cursor_wraparound(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 3)
    idx, cur = target.advance_upgrade_cursor(0, 7)
    assert (list(idx), cur) == ([0, 1, 2], 3)
    idx, cur = target.advance_upgrade_cursor(cur, 7)
    assert (list(idx), cur) == ([3, 4, 5], 6)
    idx, cur = target.advance_upgrade_cursor(cur, 7)
    assert (list(idx), cur) == ([6, 0, 1], 2)


def test_advance_upgrade_cursor_stale_clamp_and_empty(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 3)
    idx, cur = target.advance_upgrade_cursor(49, 4)
    assert (list(idx), cur) == ([3, 0, 1], 2)
    assert target.advance_upgrade_cursor(5, 0) == ([], 0)


def test_interval_gate(monkeypatch):
    from datetime import datetime, timedelta
    now = datetime(2026, 1, 15, 12, 0, 0)
    interval = target.UPGRADE_BATCH_INTERVAL_DAYS
    assert target.upgrade_batch_due({}, now) is True          # first-ever poll
    recent = {'last_run': (now - timedelta(days=interval - 1)).isoformat()}
    assert target.upgrade_batch_due(recent, now) is False     # inside window
    exact = {'last_run': (now - timedelta(days=interval)).isoformat()}
    assert target.upgrade_batch_due(exact, now) is True       # boundary
    old = {'last_run': (now - timedelta(days=interval + 10)).isoformat()}
    assert target.upgrade_batch_due(old, now) is True         # past window
    assert target.upgrade_batch_due({'last_run': 'garbage'}, now) is True


def test_run_monthly_upgrade_one_batch_per_service(monkeypatch):
    calls = []
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)
    monkeypatch.setattr(target, 'radarr_bulk_search', lambda: calls.append('radarr'))
    monkeypatch.setattr(target, 'sonarr_bulk_search', lambda: calls.append('sonarr'))
    monkeypatch.setattr(target, 'purge_stalled_upgrade_torrents', lambda label=None: None)
    monkeypatch.setattr(target, 'relabel_radarr_upgrades', lambda: None)
    monkeypatch.setattr(target, 'verify_and_fix_labels', lambda services=('radarr', 'sonarr'): [])
    client = target.app.test_client()
    r = client.post('/run-monthly-upgrade?service=radarr&skip_waits=1')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and body['service'] == 'radarr'
    import time as _t
    for _ in range(50):
        if calls:
            break
        _t.sleep(0.02)
    assert calls.count('radarr') == 1      # exactly one bounded batch pass
    assert 'sonarr' not in calls           # no full-catalog / other-service sweep


def test_run_monthly_upgrade_invalid_service(monkeypatch):
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)
    client = target.app.test_client()
    r = client.post('/run-monthly-upgrade?service=plex')
    assert r.status_code == 400
    body = r.get_json()
    assert body['ok'] is False and 'error' in body
'''
(wt / 'test_upgrade_batches.py').write_text(TEST_FILE)
print("refimpl applied")
