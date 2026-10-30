"""Deluge JSON-RPC load reduction: stall poll interval + state filter, the shared
get_all_torrents snapshot cache (TTL, field superset, force bypass), bulk-pull
timeout, and episode retention fetching "files" only for real candidates.
Hermetic: the Deluge session is a fake. Run: python -m pytest test_deluge_load.py
"""
import os
import tempfile

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ.pop('TRACKER_STALL_INTERVAL', None)
aw = __import__('arr-webhook')

DAY = 86400


class Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class Fake:
    def __init__(self, torrents):
        self.torrents, self.calls = torrents, []

    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        self.calls.append((m, p, timeout))
        if m == 'core.get_torrents_status':
            flt, fields = p[0], p[1]
            states = flt.get('state')
            ids = flt.get('id')
            res = {h: {f: t[f] for f in fields if f in t} for h, t in self.torrents.items()
                   if (states is None or t.get('state') in states) and (ids is None or h in ids)}
        elif m == 'core.get_torrent_status':
            t = self.torrents.get(p[0], {})
            res = {f: t[f] for f in p[1] if f in t}
        else:
            res = None
        return Resp({'result': res})

    def status_calls(self):
        return [c for c in self.calls if c[0] in ('core.get_torrents_status', 'core.get_torrent_status')]


def install(torrents):
    fake = Fake(torrents)
    aw.session = fake
    aw.deluge_login = lambda *a, **k: None
    aw._invalidate_snapshot()
    return fake


# ── 1. tracker stalls ────────────────────────────────────────────────────────

def test_stall_interval_default_is_300():
    assert aw.TRACKER_STALL_INTERVAL == 300


def test_stall_poll_filters_to_active_states_and_never_fetches_seeding():
    now = __import__('time').time()
    fake = install({
        'err': {'name': 'e', 'state': 'Error', 'time_added': now - 10, 'tracker_status': '', 'num_peers': 0},
        'dl': {'name': 'd', 'state': 'Downloading', 'time_added': now - 10, 'tracker_status': 'Error: x', 'num_peers': 0},
        'seed': {'name': 's', 'state': 'Seeding', 'time_added': now - 10, 'tracker_status': 'Error: x', 'num_peers': 0},
    })
    aw._last_reannounce.clear()
    aw.check_tracker_stalls()
    (m, params, _), = fake.status_calls()
    assert params[0] == {'state': ['Error', 'Downloading', 'Queued', 'Checking']}
    reannounced = [p[0] for m, p, _ in fake.calls if m == 'core.force_reannounce']
    assert sorted(reannounced[0]) == ['dl', 'err']


def test_stall_window_still_applies():
    now = __import__('time').time()
    fake = install({'old': {'name': 'o', 'state': 'Error', 'time_added': now - 5000,
                            'tracker_status': '', 'num_peers': 0}})
    aw._last_reannounce.clear()
    aw.check_tracker_stalls()
    assert not [c for c in fake.calls if c[0] == 'core.force_reannounce']


# ── 2. snapshot cache ────────────────────────────────────────────────────────

def test_snapshot_served_from_cache_within_ttl():
    fake = install({'a': {'name': 'a', 'state': 'Seeding'}})
    aw.get_all_torrents()
    aw.get_all_torrents()
    assert len(fake.status_calls()) == 1


def test_snapshot_expires_after_ttl():
    fake = install({'a': {'name': 'a', 'state': 'Seeding'}})
    clock = [1000.0]
    aw._snapshot_clock = lambda: clock[0]
    try:
        aw.get_all_torrents()
        clock[0] += aw.SNAPSHOT_TTL + 1
        aw.get_all_torrents()
    finally:
        aw._snapshot_clock = __import__('time').time
    assert len(fake.status_calls()) == 2


def test_force_bypasses_cache_and_refreshes_it():
    fake = install({'a': {'name': 'a', 'state': 'Seeding'}})
    aw.get_all_torrents()
    fake.torrents['a']['state'] = 'Paused'
    assert aw.get_all_torrents()['a']['state'] == 'Seeding'
    assert aw.get_all_torrents(force=True)['a']['state'] == 'Paused'
    assert aw.get_all_torrents()['a']['state'] == 'Paused'


def test_superset_snapshot_serves_subset_but_not_vice_versa():
    fake = install({'a': {'name': 'a', 'state': 'Seeding', 'save_path': '/x'}})
    aw.get_all_torrents(['name', 'state', 'save_path'])
    got = aw.get_all_torrents(['name'])
    assert len(fake.status_calls()) == 1
    assert got['a']['name'] == 'a'
    aw.get_all_torrents(['name', 'num_peers'])  # not covered -> new pull
    assert len(fake.status_calls()) == 2


def test_cached_result_is_isolated_from_caller_mutation():
    install({'a': {'name': 'a', 'state': 'Seeding'}})
    aw.get_all_torrents()['a']['state'] = 'Mutated'
    assert aw.get_all_torrents()['a']['state'] == 'Seeding'


def test_bulk_pull_uses_45s_timeout():
    fake = install({'a': {'name': 'a'}})
    aw.get_all_torrents()
    assert fake.status_calls()[0][2] == 45
    fake = install({})
    aw.check_tracker_stalls()
    assert fake.status_calls()[0][2] == 45


def test_move_storage_invalidates_snapshot_and_verifies_fresh():
    fake = install({'a': {'name': 'a', 'save_path': '/old'}})
    aw.get_all_torrents()
    assert aw.move_torrent_storage('a', '/data/Downloads/Just4Seeding') is True
    aw.get_all_torrents()
    assert len(fake.status_calls()) >= 3  # initial, verification, post-move re-pull


def test_import_and_queue_callers_force_fresh_reads():
    import inspect
    for fn in (aw.handle_upgrade_import, aw.relabel_radarr_upgrades, aw.relabel_sonarr_upgrades,
               aw.verify_and_fix_labels, aw.prioritize_normal_torrents, aw.purge_unstarted_superseded):
        assert 'get_all_torrents(force=True)' in inspect.getsource(fn), fn.__name__


# ── 3. episode retention: files only for candidates ──────────────────────────

def test_retention_fetches_files_only_for_aged_single_episode_candidates():
    old, young = 30 * DAY, 3 * DAY
    fake = install({
        'ep_old': {'name': 'Show.S01E05.1080p', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
                   'seeding_time': old, 'save_path': '/data/Downloads/Complete',
                   'files': [{'path': 'Show.S01E05.1080p/Show.S01E05.1080p.mkv'}]},
        'ep_young': {'name': 'Show.S01E06.1080p', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
                     'seeding_time': young, 'save_path': '/x', 'files': []},
        'movie': {'name': 'Film.2020', 'label': 'radarr', 'state': 'Seeding', 'progress': 100.0,
                  'seeding_time': old, 'save_path': '/x', 'files': []},
        'pack': {'name': 'Show.S01.1080p', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
                 'seeding_time': old, 'save_path': '/x', 'files': []},
    })
    orig_has_file = aw._library_has_file
    aw._library_has_file = lambda t: True
    try:
        r = aw.episode_retention_run(env={})
    finally:
        aw._library_has_file = orig_has_file
    bulk = [c for c in fake.calls if c[0] == 'core.get_torrents_status']
    assert all('files' not in c[1][1] for c in bulk)
    per_hash = [c[1][0] for c in fake.calls if c[0] == 'core.get_torrent_status']
    assert per_hash == ['ep_old']
    assert [e['hash'] for e in r['planned']] == ['ep_old']
    assert r['kept']['too-young'] == 1 and r['kept']['movie'] == 1
