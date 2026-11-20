"""Wiring of abandoned_audit into arr-webhook: the weekly tick, GET /abandoned-audit, the /digest section and the
log lines. Every source is faked; nothing here reaches Deluge, Plex or the arrs. Run: pytest test_abandoned_audit_wiring.py"""
import inspect
import json
import logging
import os

import pytest

aw = __import__('arr-webhook')

DAY = 86400
FIXTURE_NAME = 'Secret.Fixture.Title.mkv'


def _put(path, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)
    os.utime(path, (1, 1))
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    dl, lib = tmp_path / 'dl', tmp_path / 'lib'
    _put(dl / 'Gone' / FIXTURE_NAME, 100)
    _put(lib / 'Movie' / '<b>x</b>.mkv', 50)
    state = tmp_path / 'abandoned.json'
    monkeypatch.setattr(aw, 'ABANDONED_AUDIT_STATE', str(state))
    monkeypatch.setattr(aw, 'ABANDONED_AUDIT_INTERVAL', 7 * DAY)
    monkeypatch.setattr(aw, 'ABANDONED_AUDIT_ROOTS', [str(lib)])
    monkeypatch.setattr(aw, 'DELUGE_PATH_MAP', [('/data/Downloads', str(dl))])
    monkeypatch.setattr(aw, 'DIGEST_STATE', str(tmp_path / 'digest.json'))
    monkeypatch.setattr(aw, '_abandoned_fetch_deluge', lambda: {})
    monkeypatch.setattr(aw, '_abandoned_fetch_plex', lambda: [])
    monkeypatch.setattr(aw, '_abandoned_fetch_radarr', lambda: [])
    monkeypatch.setattr(aw, '_abandoned_fetch_sonarr', lambda: [])
    return state


def _write_state(state, finished_at):
    state.write_text(json.dumps({'finished_at': finished_at, 'buckets': {}}))


def test_tick_weekly_uses_persisted_finished_at(env):
    t0 = 5_000_000_000.0
    _write_state(env, t0)
    assert aw.abandoned_audit_tick(now=t0 + 6 * DAY) is None
    r = aw.abandoned_audit_tick(now=t0 + 7 * DAY)
    assert r is not None and r['buckets']['downloads_orphan']['count'] == 1
    assert json.loads(env.read_text())['finished_at'] == r['finished_at']   # the run was saved
    assert not os.path.exists(str(env) + '.tmp')


def test_tick_runs_with_no_state(env):
    assert aw.abandoned_audit_tick(now=1.0) is not None


def test_interval_zero_disables(env, monkeypatch):
    monkeypatch.setattr(aw, 'ABANDONED_AUDIT_INTERVAL', 0)
    monkeypatch.setattr(aw, 'abandoned_audit_run', lambda dry_run=False: pytest.fail('must not run'))
    assert aw.abandoned_audit_tick(now=1e12) is None
    assert not env.exists()


def test_scheduler_calls_tick():
    assert 'abandoned_audit_tick()' in inspect.getsource(aw.maintenance_scheduler)


def test_route_dry_run_writes_nothing(env):
    _write_state(env, 123.0)
    before = env.read_text()
    with aw.app.test_client() as c:
        r = c.get('/abandoned-audit?run=1&dry_run=1')
        assert r.status_code == 200 and r.get_json()['buckets']['downloads_orphan']['count'] == 1
    assert env.read_text() == before
    assert not os.path.exists(str(env) + '.tmp')


def test_route_run_saves_and_get_returns_stored(env):
    with aw.app.test_client() as c:
        assert c.get('/abandoned-audit').status_code == 404
        run = c.get('/abandoned-audit?run=1').get_json()
        assert env.exists()
        assert c.get('/abandoned-audit').get_json()['finished_at'] == run['finished_at']
        assert set(run['sources']) == {'deluge', 'plex', 'radarr', 'sonarr'}


def test_missing_root_is_listed_not_an_error(env, monkeypatch, tmp_path):
    monkeypatch.setattr(aw, 'ABANDONED_AUDIT_ROOTS', [str(tmp_path / 'lib'), str(tmp_path / 'nope')])
    r = aw.abandoned_audit_run(dry_run=True)
    assert r['missing_root'] == [str(tmp_path / 'nope')] and r['walk_errors'] == 0


def test_failed_source_marks_incomplete(env, monkeypatch):
    monkeypatch.setattr(aw, '_abandoned_fetch_sonarr', lambda: None)
    r = aw.abandoned_audit_run(dry_run=True)
    assert r['sources']['sonarr'] == 'failed' and r['sources']['radarr'] == 'ok'
    assert r['buckets']['library_untracked']['status'] == 'incomplete'
    assert r['buckets']['downloads_orphan']['status'] == 'ok'


def test_digest_section_renders(env, monkeypatch):
    assert 'no audit yet' in aw._digest_abandoned_html()
    aw.abandoned_audit_run()
    html = aw._digest_abandoned_html()
    assert 'downloads_orphan: 1' in html and 'library_untracked: 1' in html
    assert 'href="/abandoned-audit"' in html
    assert '&lt;b&gt;x&lt;/b&gt;.mkv' in html and '<b>x</b>' not in html
    assert FIXTURE_NAME in html
    monkeypatch.setattr(aw, '_abandoned_fetch_plex', lambda: None)
    aw.abandoned_audit_run()
    assert 'incomplete' in aw._digest_abandoned_html()
    with aw.app.test_client() as c:
        assert 'Abandoned files' in c.get('/digest').get_data(as_text=True)


def test_digest_empty_state_via_route(env):
    with aw.app.test_client() as c:
        assert 'no audit yet' in c.get('/digest').get_data(as_text=True)


def test_logs_have_no_paths(env, caplog, tmp_path):
    with caplog.at_level(logging.DEBUG):
        aw.abandoned_audit_tick(now=1e12)
        aw.abandoned_audit_run()
    text = caplog.text
    assert 'Abandoned audit:' in text and 'GB reclaimable' in text
    assert FIXTURE_NAME not in text and 'Secret' not in text and str(tmp_path) not in text and 'Gone' not in text


def test_source_failure_logs_name_not_url(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError('ConnectionError: http://example.invalid:32400/library/sections?token=zzz')
    monkeypatch.setattr(aw, '_plex_get', boom)
    with caplog.at_level(logging.DEBUG):
        assert aw._abandoned_fetch_plex() is None
    assert 'plex source failed' in caplog.text and 'example.invalid' not in caplog.text and 'zzz' not in caplog.text
