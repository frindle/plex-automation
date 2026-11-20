"""Stuck downloads in arr-webhook.py: the gate, the actions, the arr API calls, the preview.
Everything is faked; nothing reaches Deluge, Sonarr, Radarr or Pushover. Titles are made up."""
import importlib.util
import json

import pytest

spec = importlib.util.spec_from_file_location("aw_stuck", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

MIN = 60
HOUR = 3600
T0 = 5_000_000.0
_REAL_REMOVE = aw.remove_torrent
H1, H2, H3 = '1' * 40, '2' * 40, '3' * 40


def tor(**kw):
    base = dict(name='Example Movie (2020)', label='radarr', state='Downloading', progress=40.0, is_finished=False,
                download_payload_rate=0, time_since_download=30 * MIN, active_time=2 * HOUR, max_connections=-1)
    base.update(kw)
    return base


class _Resp:
    def __init__(self, payload=None):
        self._p = payload if payload is not None else {}
        self.content = b'{}'

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeRequests:
    """requests: the arr queue is served from `queue`; every call is recorded."""

    def __init__(self, queue=None, fail_delete=False):
        self.calls = []
        self.queue = queue or {}
        self.fail_delete = fail_delete

    def get(self, url, **kw):
        self.calls.append(('GET', url, kw))
        who = 'Sonarr' if 'sonarr' in url else 'Radarr'
        return _Resp({'records': [r for r in self.queue.get(who, [])]})

    def delete(self, url, **kw):
        self.calls.append(('DELETE', url, kw))
        if self.fail_delete:
            raise RuntimeError('boom')
        return _Resp()

    def post(self, url, **kw):
        self.calls.append(('POST', url, kw))
        return _Resp({'id': 1})

    def writes(self):
        return [c for c in self.calls if c[0] != 'GET']


@pytest.fixture
def env(monkeypatch, tmp_path):
    rec = {'activity': [], 'rpc': [], 'pause': [], 'resume': [], 'push': [], 'removed': []}
    monkeypatch.setattr(aw, 'SONARR_URL', 'http://sonarr.example.com')
    monkeypatch.setattr(aw, 'RADARR_URL', 'http://radarr.example.com')
    monkeypatch.setattr(aw, 'SONARR_API_KEY', 'k')
    monkeypatch.setattr(aw, 'RADARR_API_KEY', 'k')
    monkeypatch.setattr(aw, 'STUCK_STATE_PATH', str(tmp_path / 'stuck_state.json'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'record_activity', lambda cat, text: rec['activity'].append((cat, text)))
    monkeypatch.setattr(aw, '_deluge_rpc', lambda m, p: rec['rpc'].append((m, p)) or True)
    monkeypatch.setattr(aw, 'deluge_pause_torrent', lambda h: rec['pause'].append(h))
    monkeypatch.setattr(aw, 'deluge_resume_torrent', lambda h: rec['resume'].append(h))
    monkeypatch.setattr(aw, 'send_pushover', lambda t, m, url=None: rec['push'].append((t, m)))
    monkeypatch.setattr(aw, 'requests', FakeRequests())
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: rec['removed'].append((h, remove_data)) or True)
    monkeypatch.setattr(aw, '_stuck_state', {})
    monkeypatch.setattr(aw, '_stuck_state_loaded', [False])
    monkeypatch.setattr(aw, '_stuck_logged', set())
    for k in ('STUCK_AUTOMATION', 'STUCK_AFTER_MIN', 'STUCK_GIVE_UP_HOURS', 'UPGRADE_MAX_CONNECTIONS'):
        monkeypatch.delenv(k, raising=False)
    rec['set_torrents'] = lambda torrents: monkeypatch.setattr(aw, 'get_all_torrents', lambda fields=None, force=False: torrents)
    rec['set_requests'] = lambda fake: monkeypatch.setattr(aw, 'requests', fake)
    return rec


def changes(rec):
    return rec['rpc'] + [('pause', h) for h in rec['pause']] + [('resume', h) for h in rec['resume']]


# ── the gate ────────────────────────────────────────────────────────────────
def test_default_and_unknown_values_are_dry(monkeypatch):
    monkeypatch.delenv('STUCK_AUTOMATION', raising=False)
    assert aw.stuck_mode() == 'dry'
    for v in ('', 'banana', 'true', '1', 'ON ', ' Dry'):
        monkeypatch.setenv('STUCK_AUTOMATION', v)
        assert aw.stuck_mode() == ('on' if v.strip().lower() == 'on' else 'dry'), v
    monkeypatch.setenv('STUCK_AUTOMATION', 'off')
    assert aw.stuck_mode() == 'off'


def test_off_does_nothing(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'off')
    env['set_torrents']({H1: tor(), H2: tor(label='radarr-upgrade')})
    assert aw.stuck_pass(now=T0) is None
    assert changes(env) == [] and aw.requests.calls == [] and env['activity'] == []


def test_dry_changes_nothing_anywhere_and_records_lines(env):
    env['set_torrents']({H1: tor(), H2: tor(label='radarr-upgrade'), H3: tor(label='sonarr', progress=97)})
    out = aw.stuck_pass(now=T0)                                   # default mode: dry
    assert out['mode'] == 'dry'
    assert changes(env) == [] and env['push'] == [] and aw.requests.calls == []
    text = ' | '.join(t for _, t in env['activity'])
    assert '[dry] would have reannounced' in text and '[dry] would have queued to top' in text
    assert '[dry] would cap upgrade-lane torrent "Example Movie (2020)" at 10 connections' in text
    assert 'pass:' not in text, 'the per-pass summary is log-only'
    # a later dry pass walks the ladder in memory only, still changing nothing
    aw.stuck_pass(now=T0 + 25 * MIN)
    assert changes(env) == [] and aw.requests.calls == []
    assert 'would have paused' in ' | '.join(t for _, t in env['activity'])
    import os
    assert not os.path.exists(aw.STUCK_STATE_PATH), 'dry mode saves no state file'


def test_dry_give_up_and_notify_are_only_recorded(env):
    env['set_torrents']({H1: tor(), H3: tor(label='sonarr', progress=97)})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 7 * HOUR)
    assert changes(env) == [] and env['push'] == [] and aw.requests.writes() == []
    text = ' | '.join(t for _, t in env['activity'])
    assert '[dry] would have given up on stuck "Example Movie (2020)"' in text
    assert 'would have notified about' in text


def test_every_pass_logs_a_summary_but_adds_no_activity_line(env, caplog):
    env['set_torrents']({H1: tor(download_payload_rate=500_000, time_since_download=0)})
    with caplog.at_level('INFO'):
        aw.stuck_pass(now=T0)
    assert any('[stuck] dry pass:' in r.getMessage() for r in caplog.records)
    assert env['activity'] == []


def test_dry_repeated_passes_do_not_add_repeated_digest_lines(env):
    env['set_torrents']({H1: tor(), H2: tor(label='radarr-upgrade')})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 7 * HOUR)
    first = list(env['activity'])
    assert sum('would have given up' in t for _, t in first) == 1
    aw.stuck_pass(now=T0 + 7 * HOUR + 10 * MIN)       # same state again: the give-up is re-planned
    aw.stuck_pass(now=T0 + 7 * HOUR + 20 * MIN)
    assert env['activity'] == first, 'two more passes in the same state add no digest line'
    assert sum('would cap' in t for _, t in first) == 1


# ── on: the ladder ──────────────────────────────────────────────────────────
def test_on_step_1_then_2(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    env['set_torrents']({H1: tor()})
    aw.stuck_pass(now=T0)
    assert env['rpc'] == [('core.force_reannounce', [[H1]]), ('core.queue_top', [[H1]])]
    assert env['pause'] == [] and env['resume'] == []
    aw.stuck_pass(now=T0 + 20 * MIN)
    assert env['pause'] == [H1] and env['resume'] == [H1]
    assert aw.requests.calls == []


def test_on_saves_state_and_a_restart_keeps_the_ladder(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    env['set_torrents']({H1: tor()})
    aw.stuck_pass(now=T0)
    saved = json.load(open(aw.STUCK_STATE_PATH))
    assert saved[H1]['step'] == 1
    # "restart": empty memory, same file
    monkeypatch.setattr(aw, '_stuck_state', {})
    monkeypatch.setattr(aw, '_stuck_state_loaded', [False])
    del env['rpc'][:]
    aw.stuck_pass(now=T0 + 20 * MIN)
    assert env['pause'] == [H1], 'step 2 follows from the saved step 1'
    assert env['rpc'] == [], 'step 1 is not repeated after a restart'


def test_on_pause_failure_resumes_the_torrent(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    monkeypatch.setattr(aw, 'deluge_pause_torrent', lambda h: (_ for _ in ()).throw(RuntimeError('boom')))
    env['set_torrents']({H1: tor()})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 20 * MIN)
    assert H1 in env['resume'], 'a torrent is never left paused by us'


def test_on_give_up_radarr_uses_the_arr_api_with_blocklist_and_searches(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    fake = FakeRequests(queue={'Radarr': [{'id': 77, 'downloadId': H1.upper(), 'movieId': 12}]})
    env['set_requests'](fake)
    env['set_torrents']({H1: tor(progress=50)})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    del env['rpc'][:]
    aw.stuck_pass(now=T0 + 6 * HOUR + MIN)
    deletes = [c for c in fake.calls if c[0] == 'DELETE']
    assert len(deletes) == 1 and deletes[0][1] == 'http://radarr.example.com/api/v3/queue/77'
    assert deletes[0][2]['params'] == {'removeFromClient': False, 'blocklist': True}, 'the arr never deletes from the client'
    assert env['removed'] == [(H1, True)], 'the removal goes through remove_torrent()'
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert posts[0][1] == 'http://radarr.example.com/api/v3/command'
    assert posts[0][2]['json'] == {'name': 'MoviesSearch', 'movieIds': [12]}
    assert env['rpc'] == [], 'no direct Deluge call'
    assert any('Gave up on stuck' in t for _, t in env['activity'])


def test_on_give_up_sonarr_bulk_delete_and_episode_search(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    fake = FakeRequests(queue={'Sonarr': [{'id': 5, 'downloadId': H1, 'episodeId': 31},
                                          {'id': 6, 'downloadId': H1, 'episodeId': 32}]})
    env['set_requests'](fake)
    env['set_torrents']({H1: tor(label='sonarr', name='Example Show S01')})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 6 * HOUR + MIN)
    delete = [c for c in fake.calls if c[0] == 'DELETE'][0]
    assert delete[1] == 'http://sonarr.example.com/api/v3/queue/bulk'
    assert delete[2]['json'] == {'ids': [5, 6]}
    assert delete[2]['params'] == {'removeFromClient': False, 'blocklist': True}
    post = [c for c in fake.calls if c[0] == 'POST'][0]
    assert post[2]['json'] == {'name': 'EpisodeSearch', 'episodeIds': [31, 32]}


def test_on_give_up_not_in_any_queue_changes_nothing(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    fake = FakeRequests(queue={})
    env['set_requests'](fake)
    env['set_torrents']({H1: tor()})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    del env['rpc'][:]
    aw.stuck_pass(now=T0 + 7 * HOUR)
    assert fake.writes() == [] and env['rpc'] == []
    assert any('not in a Radarr queue' in t for _, t in env['activity'])


def test_on_failed_removal_does_not_search_and_is_retried(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    fake = FakeRequests(queue={'Radarr': [{'id': 77, 'downloadId': H1, 'movieId': 12}]}, fail_delete=True)
    env['set_requests'](fake)
    env['set_torrents']({H1: tor()})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 7 * HOUR)
    assert [c for c in fake.calls if c[0] == 'POST'] == []
    aw.stuck_pass(now=T0 + 7 * HOUR + 10 * MIN)
    assert len([c for c in fake.calls if c[0] == 'DELETE']) == 2, 'tried again next pass'


def test_on_at_95_percent_notifies_once_and_never_calls_the_arr(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    fake = FakeRequests(queue={'Radarr': [{'id': 77, 'downloadId': H1, 'movieId': 12}]})
    env['set_requests'](fake)
    env['set_torrents']({H1: tor(progress=96.5)})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 7 * HOUR)
    aw.stuck_pass(now=T0 + 14 * HOUR)
    assert len(env['push']) == 1 and '96.5%' in env['push'][0][1]
    assert fake.writes() == []


def test_on_leaves_out_of_scope_torrents_alone(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    env['set_torrents']({'a': tor(label=''), 'b': tor(label='library-seed'),
                         'c': tor(state='Seeding', is_finished=True, progress=100.0),
                         'd': tor(label='adult')})
    for dt in (0, 25 * MIN, 7 * HOUR):
        aw.stuck_pass(now=T0 + dt)
    assert changes(env) == [] and aw.requests.calls == [] and env['push'] == []


# ── on: the connection cap ──────────────────────────────────────────────────
def test_on_caps_upgrade_lane_connections(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    monkeypatch.setenv('UPGRADE_MAX_CONNECTIONS', '6')
    env['set_torrents']({H1: tor(label='radarr-upgrade'), H2: tor(label='sonarr-upgrade-recent', max_connections=6),
                         H3: tor(label='radarr')})
    aw.stuck_pass(now=T0)
    caps = [(m, p) for m, p in env['rpc'] if m == 'core.set_torrent_options']
    assert caps == [('core.set_torrent_options', [[H1], {'max_connections': 6}])], 'only the field max_connections'


def test_relabel_release_resets_the_connection_cap_too(env, monkeypatch):
    posts = []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            posts.append(json)
            return _Resp({'result': True})

    monkeypatch.setattr(aw, 'session', S())
    aw.release_inflight_superseded(H1, 'Downloading', 40.0)
    opts = [p['params'] for p in posts if p['method'] == 'core.set_torrent_options']
    assert opts == [[[H1], {'max_download_speed': -1, 'max_connections': -1}]]


def test_relabel_to_base_lifts_the_connection_cap(env, monkeypatch):
    status = {'label': 'radarr-upgrade', 'name': 'Example Movie (2020)', 'max_connections': 10}

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            return _Resp({'result': dict(status)})

    labels = []
    monkeypatch.setattr(aw, 'session', S())
    monkeypatch.setattr(aw, 'set_torrent_label', lambda h, lab: labels.append((h, lab)))
    assert aw.relabel_download_to_base(H1, 'Radarr')[0] == 'flipped'
    assert labels == [(H1, 'radarr')]
    assert env['rpc'] == [('core.set_torrent_options', [[H1], {'max_connections': -1}])]
    # a cap this feature did not set (any other value) is left alone
    del env['rpc'][:]
    status['max_connections'] = 40
    aw.relabel_download_to_base(H1, 'Radarr')
    assert env['rpc'] == []


def test_give_up_never_deletes_a_stalled_partial_that_uploaded_under_the_seed_floor(env, monkeypatch):
    """The real remove_torrent() runs: a stalled partial with uploaded bytes is kept, and the arr is never
    asked to delete from the client."""
    monkeypatch.setattr(aw, 'remove_torrent', _REAL_REMOVE)
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')
    deluge = []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            deluge.append(json['method'])
            if json['method'] == 'core.get_torrent_status':
                return _Resp({'result': {'name': 'Example Movie (2020)', 'seeding_time': 0, 'total_uploaded': 5_000_000,
                                         'is_finished': False, 'progress': 50.0, 'save_path': '/downloads/x',
                                         'tracker_status': 'ok'}})
            return _Resp({'result': True})

    monkeypatch.setattr(aw, 'session', S())
    fake = FakeRequests(queue={'Radarr': [{'id': 77, 'downloadId': H1, 'movieId': 12}]})
    env['set_requests'](fake)
    env['set_torrents']({H1: tor(progress=50)})
    aw.stuck_pass(now=T0)
    aw.stuck_pass(now=T0 + 25 * MIN)
    aw.stuck_pass(now=T0 + 6 * HOUR + MIN)
    assert 'core.remove_torrent' not in deluge, 'seed floor: the torrent is not deleted'
    assert all(c[2].get('params', {}).get('removeFromClient') is False for c in fake.calls if c[0] == 'DELETE')
    assert any('seed floor refused' in t for _, t in env['activity'])


# ── settings ────────────────────────────────────────────────────────────────
def test_settings_default_and_bad_values(monkeypatch):
    for k in ('STUCK_AFTER_MIN', 'STUCK_GIVE_UP_HOURS', 'UPGRADE_MAX_CONNECTIONS'):
        monkeypatch.delenv(k, raising=False)
    c = aw.stuck_config()
    assert (c.stuck_after_min, c.give_up_hours, c.upgrade_max_connections) == (20, 6, 10)
    monkeypatch.setenv('STUCK_AFTER_MIN', 'soon')
    monkeypatch.setenv('STUCK_GIVE_UP_HOURS', '12')
    monkeypatch.setenv('UPGRADE_MAX_CONNECTIONS', '0')
    c = aw.stuck_config()
    assert (c.stuck_after_min, c.give_up_hours, c.upgrade_max_connections) == (20, 12, 2)
    assert 'radarr-upgrade' in c.upgrade_labels and 'library-seed' not in c.normal_labels


# ── the preview ─────────────────────────────────────────────────────────────
def test_preview_lists_steps_and_changes_nothing(env, monkeypatch):
    monkeypatch.setenv('STUCK_AUTOMATION', 'on')              # even when on, the preview is read-only
    env['set_torrents']({H1: tor(), H2: tor(label='radarr-upgrade'), H3: tor(label='')})
    c = aw.app.test_client()
    body = c.get('/stuck-downloads').get_json()
    assert body['ok'] and body['mode'] == 'on' and body['count'] == 2
    by = {r['hash']: r for r in body['rows']}
    assert by[H1]['step'] == 1 and 'reannounce' in by[H1]['next']
    assert 'cap connections at 10' in by[H2]['next']
    assert H3 not in by
    assert changes(env) == [] and aw.requests.calls == [] and aw._stuck_state == {}, 'nothing changed, state untouched'
    # the next real pass then really does step 1 (the preview did not advance the ladder)
    aw.stuck_pass(now=T0)
    assert ('core.force_reannounce', [[H1]]) in env['rpc']


def test_preview_when_deluge_cannot_be_read(env, monkeypatch):
    def boom(fields=None, force=False):
        raise RuntimeError('down')
    monkeypatch.setattr(aw, 'get_all_torrents', boom)
    r = aw.app.test_client().get('/stuck-downloads')
    assert r.status_code == 502 and r.get_json()['ok'] is False
