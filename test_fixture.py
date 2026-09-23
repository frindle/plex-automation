"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s8-tests

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


def _patch(**attrs):
    """Monkeypatch attributes on `target`, return (saved, restore)."""
    saved = {}
    for name, value in attrs.items():
        saved[name] = getattr(target, name)
        setattr(target, name, value)

    def restore():
        for name, value in saved.items():
            setattr(target, name, value)
    return restore


def _sort_case():
    # Newest year first; missing 'year' falls back to the first four chars of
    # 'firstAired'; no usable year at all sorts last; ties keep input order.
    items = [
        {'id': 1, 'year': 2019},
        {'id': 2, 'firstAired': '2023-05-01T00:00:00Z'},
        {'id': 3},
        {'id': 4, 'year': 2023},
        {'id': 5, 'firstAired': 'not-a-date'},
    ]
    return [i['id'] for i in target.sort_ids_by_year_desc(items)]


def _sort_stable_case():
    # Ties within a year keep input order (stable sort).
    items = [{'id': 'a', 'year': 2020}, {'id': 'b', 'firstAired': '2020-01-01'},
             {'id': 'c'}]
    return [i['id'] for i in target.sort_ids_by_year_desc(items)]


def _cursor_case():
    # Three simulated passes over a 7-item catalog with batch size 12 -> each
    # pass covers the whole remaining slice and wraps; then a stale cursor from
    # a larger (50-item) catalog must clamp into range when total shrinks to 4.
    target.UPGRADE_BATCH_SIZE = 3
    try:
        idx, cur = target.advance_upgrade_cursor(0, 7)      # pass 1
        assert list(idx) == [0, 1, 2] and cur == 3
        idx, cur = target.advance_upgrade_cursor(cur, 7)     # pass 2
        assert list(idx) == [3, 4, 5] and cur == 6
        idx, cur = target.advance_upgrade_cursor(cur, 7)     # pass 3 wraps
        assert list(idx) == [6, 0, 1] and cur == 2
        idx, cur = target.advance_upgrade_cursor(49, 4)      # stale cursor clamps
        assert list(idx) == [3, 0, 1] and cur == 2
        idx, cur = target.advance_upgrade_cursor(5, 0)       # empty catalog
        assert list(idx) == [] and cur == 0
    finally:
        del target.UPGRADE_BATCH_SIZE
    return 'ok'


def _gate_case():
    from datetime import datetime, timedelta
    now = datetime(2026, 1, 15, 12, 0, 0)
    interval = target.UPGRADE_BATCH_INTERVAL_DAYS
    # First-ever poll: empty state -> due.
    assert target.upgrade_batch_due({}, now) is True
    assert target.upgrade_batch_due(None, now) is True
    # Inside the window (interval - 1 days ago) -> NOT due.
    recent = {'last_run': (now - timedelta(days=interval - 1)).isoformat()}
    assert target.upgrade_batch_due(recent, now) is False
    # Exactly at the boundary and past it -> due.
    exact = {'last_run': (now - timedelta(days=interval)).isoformat()}
    assert target.upgrade_batch_due(exact, now) is True
    old = {'last_run': (now - timedelta(days=interval + 10)).isoformat()}
    assert target.upgrade_batch_due(old, now) is True
    # Unparseable stamp -> due, never wedged.
    assert target.upgrade_batch_due({'last_run': 'garbage'}, now) is True
    return 'ok'


def _route_case():
    """POST /run-monthly-upgrade: {'ok': True}, exactly ONE next batch for the
    requested service (radarr_bulk_search once), no full-catalog sweep and no
    sonarr pass when only radarr was asked for."""
    import time as _t
    calls = []
    target.time.sleep = lambda s: None  # don't wait out the cycle's sleeps
    restore = _patch(
        radarr_bulk_search=lambda: calls.append('radarr'),
        sonarr_bulk_search=lambda: calls.append('sonarr'),
        purge_stalled_upgrade_torrents=lambda label=None: calls.append('purge'),
        relabel_radarr_upgrades=lambda: calls.append('relabel-radarr'),
        relabel_sonarr_upgrades=lambda: calls.append('relabel-sonarr'),
        verify_and_fix_labels=lambda services=('radarr', 'sonarr'): [],
    )
    try:
        client = target.app.test_client()
        r = client.post('/run-monthly-upgrade?service=radarr&skip_waits=1')
        body = r.get_json()
        assert r.status_code == 200, (r.status_code, body)
        assert body['ok'] is True and body['service'] == 'radarr'
        # The cycle runs on a daemon thread; keep the patches alive while we
        # wait for it to record its calls.
        deadline = _t.monotonic() + 5.0
        while 'radarr' not in calls and _t.monotonic() < deadline:
            _t.sleep(0.02)
    finally:
        restore()
    assert calls.count('radarr') == 1, calls          # exactly one batch pass
    assert 'sonarr' not in calls, calls               # no other service swept
    return 'ok'


def _route_bad_service_case():
    """Invalid ?service= must be a clean 400 with an error body, not a 500."""
    client = target.app.test_client()
    r = client.post('/run-monthly-upgrade?service=plex')
    body = r.get_json() or {}
    return (r.status_code, body.get('ok'), 'error' in body)


CASES = [
    ("sort_ids_by_year_desc: year desc, firstAired fallback, no-year last, stable ties",
     _sort_case, [2, 4, 1, 3, 5]),
    ("sort_ids_by_year_desc: stable ties within a year keep input order",
     _sort_stable_case, ['a', 'b', 'c']),
    ("advance_upgrade_cursor: multi-pass wraparound + stale-cursor clamp + empty total",
     _cursor_case, 'ok'),
    ("upgrade_batch_due: not due inside window, due at/after interval, first-ever poll due",
     _gate_case, 'ok'),
    ("POST /run-monthly-upgrade?service=radarr -> {'ok': True}, exactly one radarr batch, no sonarr sweep",
     _route_case, 'ok'),
    ("POST /run-monthly-upgrade with invalid service -> 400 + error body (not 500)",
     _route_bad_service_case, (400, False, True)),
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
