"""Dead (tracker-unregistered) torrent sweep: dry run changes nothing; apply blocklists queue items
and removes only matching torrents through remove_torrent; guards still refuse. Synthetic data, mocked
Deluge/Sonarr/Radarr. Run: python -m pytest test_unregistered_sweep.py"""
import importlib.util
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
os.environ['SONARR_URL'] = 'http://sonarr.test'
os.environ['SONARR_API_KEY'] = 'fake-key'
os.environ['RADARR_URL'] = 'http://radarr.test'
os.environ['RADARR_API_KEY'] = 'fake-key'
_spec = importlib.util.spec_from_file_location(
    'arr_webhook_unreg', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)

NOW = 2_000_000_000.0
OLD = 40 * 86400
DL = '/data/Downloads/Complete'
A, B, C, D, E, F = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40, 'e' * 40, 'f' * 40)


def tor(name, status, label='sonarr', seeded=OLD, added_days=60, **kw):
    t = {'name': name, 'label': label, 'save_path': DL, 'seeding_time': seeded, 'is_finished': True,
         'progress': 100.0, 'state': 'Seeding', 'total_uploaded': 10, 'tracker_status': status,
         'time_added': NOW - added_days * 86400}
    t.update(kw)
    return t


class Resp:
    def __init__(self, p=None):
        self._p = p if p is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class World:
    def __init__(self, torrents, sonarr_q=(), radarr_q=(), imported=()):
        self.torrents, self.sonarr_q, self.radarr_q = torrents, list(sonarr_q), list(radarr_q)
        self.imported = set(imported)
        self.deluge, self.deletes, self.sonarr_down, self.radarr_down = [], [], False, False

    def post(self, url, json=None, timeout=None, **kw):
        m, params = json['method'], json['params']
        self.deluge.append((m, params))
        if m == 'core.get_torrents_status':
            return Resp({'result': self.torrents})
        if m == 'core.remove_torrent':
            return Resp({'result': True, 'error': None})
        return Resp({'result': {}, 'error': None})

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        if url.startswith('http://sonarr.test/api/v3/queue'):
            if self.sonarr_down:
                raise ConnectionError('down')
            return Resp({'records': self.sonarr_q})
        if url.startswith('http://radarr.test/api/v3/queue'):
            if self.radarr_down:
                raise ConnectionError('down')
            return Resp({'records': self.radarr_q})
        if '/api/v3/history' in url:
            h = params['downloadId'].lower()
            return Resp({'records': [{'eventType': 'downloadFolderImported'}] if h in self.imported else []})
        raise AssertionError(url)

    def delete(self, url, headers=None, params=None, json=None, timeout=None, **kw):
        self.deletes.append((url, dict(params or {}), json))
        return Resp()

    def removed(self):
        return [(p[0], p[1]) for m, p in self.deluge if m == 'core.remove_torrent']


@pytest.fixture
def world(monkeypatch):
    def make(torrents, **kw):
        w = World(torrents, **kw)
        monkeypatch.setattr(aw, 'session', w)
        monkeypatch.setattr(aw.requests, 'get', w.get)
        monkeypatch.setattr(aw.requests, 'delete', w.delete)
        monkeypatch.setattr(aw, 'deluge_login', lambda: None)
        aw._invalidate_snapshot()
        aw._unregistered_reported.clear()
        return w
    return make


def test_phrases_one_constant_case_insensitive():
    for s in ('Unregistered torrent', 'TORRENT NOT REGISTERED WITH THIS TRACKER', 'Error: Torrent Not Found',
              'info hash not found', 'Torrent deleted'):
        assert aw.torrent_is_unregistered({'tracker_status': s}), s
    for s in ('Announce OK', 'Error: Connection timed out', 'Error: Host not found (authoritative)', 'unreachable',
              '', None):
        assert not aw.torrent_is_unregistered({'tracker_status': s}), s


def test_dry_run_changes_nothing_and_lists(world):
    w = world({A: tor('Show.S01E01', 'Error: Unregistered torrent'), B: tor('Fine', 'Announce OK')},
              sonarr_q=[{'id': 7, 'downloadId': A.upper()}])
    rows = aw.unregistered_sweep(apply=False, now=NOW)
    assert [r['hash'] for r in rows] == [A]
    r = rows[0]
    assert r['arr'] == 'Sonarr' and r['in_queue'] and r['age_days'] == 60.0 and r['outcome'] == 'dry-run'
    assert 'blocklist' in r['would']
    assert w.deletes == [] and w.removed() == []
    assert aw._unregistered_report['apply'] is False
    html = aw._digest_unregistered_html()
    assert 'Show.S01E01' in html and 'dry run' in html


def test_apply_blocklists_queue_item_then_removes_only_matches(world):
    w = world({A: tor('Show.S01E01', 'Error: unregistered torrent'),
               B: tor('Movie.2024', 'Torrent not registered', label='radarr'),
               C: tor('Healthy', 'Announce OK'),
               D: tor('Timeout', 'Error: Connection timed out'),
               E: tor('Imported.Show', 'Unregistered', label='sonarr')},
              sonarr_q=[{'id': 7, 'downloadId': A.upper()}, {'id': 8, 'downloadId': A.upper()}],
              radarr_q=[{'id': 21, 'downloadId': B.upper()}], imported={E})
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert {r['hash'] for r in rows} == {A, B, E}
    # queue: blocklist true, client untouched
    assert (('http://sonarr.test/api/v3/queue/bulk', {'removeFromClient': False, 'blocklist': True}, {'ids': [7, 8]})) in w.deletes
    assert ('http://radarr.test/api/v3/queue/21', {'removeFromClient': False, 'blocklist': True}, None) in w.deletes
    assert len(w.deletes) == 2
    # torrents removed through remove_torrent: queue items and the imported one; nothing else
    assert sorted(w.removed()) == sorted([(A, True), (B, True), (E, True)])
    assert all(r['outcome'] == 'removed' for r in rows)


def test_imported_removal_never_touches_the_arr_queue(world):
    w = world({E: tor('Imported.Show', 'Unregistered')}, imported={E})
    aw.unregistered_sweep(apply=True, now=NOW)
    assert w.deletes == [] and w.removed() == [(E, True)]


def test_not_in_queue_and_not_confirmed_imported_keeps_files(world):
    w = world({E: tor('Unknown.Show', 'Unregistered')})
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert w.deletes == [] and w.removed() == [(E, False)]
    assert rows[0]['outcome'] == 'removed (files kept)'


def test_seed_floor_still_refuses_and_is_listed(world):
    w = world({A: tor('Young', 'Unregistered', seeded=3 * 86400, added_days=4)},
              sonarr_q=[{'id': 7, 'downloadId': A.upper()}])
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert w.removed() == []                       # remove_torrent deferred; no way around it
    assert w.deletes and w.deletes[0][1]['blocklist'] is True   # the queue side still ran
    assert rows[0]['outcome'].startswith('refused by seed floor')
    assert 'refused by seed floor' in aw._digest_unregistered_html()


def test_library_save_path_keeps_files(world):
    w = world({E: tor('Lib.Show', 'Unregistered', save_path='/data/Media/TV Shows/Lib')}, imported={E})
    aw.unregistered_sweep(apply=True, now=NOW)
    assert w.removed() == [(E, False)]             # library-seed guard downgrades to keep-data


def test_arr_unreachable_leaves_its_torrents_alone(world):
    w = world({A: tor('Show.S01E01', 'Unregistered')})
    w.sonarr_down = True
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert w.deletes == [] and w.removed() == []
    assert rows[0]['outcome'] == 'dry-run' and 'unreadable' in rows[0]['would']


def test_tick_runs_at_most_once_an_hour(world, monkeypatch):
    world({})
    aw._unregistered_last[0] = 0.0
    assert aw.unregistered_sweep_tick(now=NOW) == []
    assert aw.unregistered_sweep_tick(now=NOW + 3599) is None
    assert aw.unregistered_sweep_tick(now=NOW + 3600) == []


def test_default_is_dry_run(world):
    w = world({A: tor('Show', 'Unregistered')}, sonarr_q=[{'id': 7, 'downloadId': A.upper()}])
    assert aw.UNREGISTERED_APPLY is False
    aw.unregistered_sweep(now=NOW)
    assert w.deletes == [] and w.removed() == []
