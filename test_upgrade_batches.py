"""Yearly-upgrade batch machinery: ordering, cursor walk, interval gate, and
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
    assert [i['id'] for i in ordered] == [4, 2, 1, 5, 3]
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
