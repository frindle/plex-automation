"""Tests for restore_moved_files (hardlink moved files back into live torrents) and importMode 'copy'."""

import inspect
import json
import os
import tempfile

import requests

ACT = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ['ACTIVITY_LOG'] = ACT
aw = __import__('arr-webhook')


class Resp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


SAVE = '/data/Downloads/Complete/sonarr'
H = 'a' * 40
DROPPED = f'{SAVE}/Show.S01/ep1.mkv'
IMPORTED = '/data/Media/TV Shows/Show/ep1.mkv'


class World:
    def __init__(self, monkeypatch, tmp_path, events=None, state='Seeding', save_path=SAVE,
                 files=None, create_src=True, src_size=10, dst_exists=False):
        self.calls = []
        self.root = tmp_path
        monkeypatch.setattr(aw, 'RELINK_DATA_ROOT', str(tmp_path))
        monkeypatch.setattr(aw, 'RESTORE_MOVED_FILES', True)
        # other test modules patch these module globals at import; pin our own
        monkeypatch.setattr(aw, 'ACTIVITY_LOG', ACT)
        monkeypatch.setattr(aw, 'session', requests.Session())
        monkeypatch.setattr(aw, 'SONARR_URL', 'http://sonarr')
        monkeypatch.setattr(aw, 'DELUGE_URL', 'http://deluge')
        monkeypatch.setattr(aw, 'SONARR_API_KEY', 'k')
        monkeypatch.setattr(aw, 'RADARR_API_KEY', '')
        monkeypatch.setattr(aw, '_restore_present', set())
        monkeypatch.setattr(aw, '_repair_rechecked', set())
        monkeypatch.setattr(aw, '_restore_warned', False)
        open(ACT, 'w').close()
        self.src = tmp_path / 'Media' / 'TV Shows' / 'Show' / 'ep1.mkv'
        self.dst = tmp_path / 'Downloads' / 'Complete' / 'sonarr' / 'Show.S01' / 'ep1.mkv'
        if create_src:
            self.src.parent.mkdir(parents=True)
            self.src.write_bytes(b'x' * src_size)
        if dst_exists:
            self.dst.parent.mkdir(parents=True)
            self.dst.write_bytes(b'x' * src_size)
        if events is None:
            events = [{'date': '2099-01-01T00:00:00Z', 'downloadId': '',
                       'data': {'droppedPath': DROPPED, 'importedPath': IMPORTED}}]
        self.events = events
        self.torrents = {H: {'name': 'Show.S01', 'save_path': save_path, 'state': state, 'label': 'sonarr'}}
        self.files = files if files is not None else [{'path': 'Show.S01/ep1.mkv', 'size': 10}]
        monkeypatch.setattr(requests, 'get', self._get)
        monkeypatch.setattr(requests.Session, 'post', self._post)

    def _get(self, url, headers=None, params=None, timeout=None, **kw):
        assert url.endswith('/api/v3/history') and params['eventType'] == 3
        recs = self.events if params['page'] == 1 else []
        return Resp({'records': recs, 'totalRecords': len(recs)})

    def _post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        self.calls.append((m, p))
        if m == 'auth.login':
            res = True
        elif m == 'core.get_torrents_status':
            res = self.torrents
        elif m == 'core.get_torrent_status':
            res = {'files': self.files}
        else:
            res = None
        return Resp({'result': res})

    def writes(self):
        return [c for c in self.calls if c[0] in ('core.pause_torrent', 'core.force_recheck')]


def acts():
    with open(ACT) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_restores_untracked_move_by_hardlink(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    r = aw.restore_moved_files()
    assert w.dst.exists() and os.path.samefile(w.src, w.dst)
    assert w.src.stat().st_ino == w.dst.stat().st_ino
    assert [e['dropped'] for e in r['restored']] == [DROPPED]
    assert [a['category'] for a in acts()] == ['deluge-restore']
    assert 'Restored 1 moved file(s) into "Show.S01"' in acts()[0]['summary']
    assert w.writes() == []
    assert aw._repair_rechecked == set()


def test_destination_exists_is_noop(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, dst_exists=True)
    r = aw.restore_moved_files()
    assert r['restored'] == [] and r['would_restore'] == []
    assert w.dst.stat().st_ino != w.src.stat().st_ino  # untouched, not replaced by a link
    assert acts() == []
    assert DROPPED in aw._restore_present


def test_size_mismatch_no_link(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, src_size=7)
    r = aw.restore_moved_files()
    assert not w.dst.exists()
    assert len(r['size_mismatch']) == 1 and r['size_mismatch'][0]['have'] == 7
    assert r['restored'] == [] and acts() == []


def test_imported_missing_reported(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, create_src=False)
    r = aw.restore_moved_files()
    assert not w.dst.exists()
    assert [e['imported'] for e in r['source_missing']] == [IMPORTED]
    assert r['restored'] == []


def test_library_save_path_torrent_skipped(monkeypatch, tmp_path):
    lib = '/data/Media/TV Shows'
    ev = [{'date': '2099-01-01T00:00:00Z', 'downloadId': '',
           'data': {'droppedPath': '/data/Downloads/Complete/sonarr/Show.S01/ep1.mkv', 'importedPath': IMPORTED}}]
    w = World(monkeypatch, tmp_path, events=ev, save_path=lib)
    r = aw.restore_moved_files()
    assert not w.dst.exists()
    assert r['restored'] == [] and r['would_restore'] == [] and r['source_missing'] == []
    assert not any(c[0] == 'core.get_torrent_status' for c in w.calls)


def test_dry_run_reports_without_linking(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, state='Error')
    r = aw.restore_moved_files(dry_run=True)
    assert not w.dst.exists()
    assert [e['dropped'] for e in r['would_restore']] == [DROPPED]
    assert r['restored'] == [] and w.writes() == [] and acts() == []


def test_missing_data_root_disables(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    monkeypatch.setattr(aw, 'RELINK_DATA_ROOT', str(tmp_path / 'nope'))
    r = aw.restore_moved_files()
    assert r['ok'] is True and 'disabled' in r
    assert aw.restore_moved_files()['ok'] is True  # second call still no exception
    assert not w.dst.exists() and w.calls == []


def test_error_torrent_paused_and_rechecked(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, state='Error')
    r = aw.restore_moved_files(lookback_days=90, only_hashes={H}, untracked_only=False)
    assert len(r['restored']) == 1 and w.dst.exists()
    methods = [c[0] for c in w.writes()]
    assert methods == ['core.pause_torrent', 'core.force_recheck']
    assert H in aw._repair_rechecked


def test_written_off_queued_torrent_rechecked_not_paused(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, state='Queued')
    w.torrents[H]['progress'] = 0.0
    r = aw.restore_moved_files()
    assert len(r['restored']) == 1 and w.dst.exists()
    assert [c[0] for c in w.writes()] == ['core.force_recheck']
    assert aw._repair_rechecked == set()


def test_complete_seeding_torrent_not_rechecked(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, state='Seeding')
    w.torrents[H]['progress'] = 100.0
    r = aw.restore_moved_files()
    assert len(r['restored']) == 1
    assert w.writes() == []


def test_error_repair_does_not_double_pause(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, state='Error')
    r = aw.deluge_error_repair()
    assert len(r['restore']['restored']) == 1
    assert [c[0] for c in w.writes()] == ['core.pause_torrent', 'core.force_recheck']
    assert r['rechecked'] == []


def test_unowned_dropped_path_never_stat(monkeypatch, tmp_path):
    other = '/data/Downloads/Other/Thing/f.mkv'
    ev = [{'date': '2099-01-01T00:00:00Z', 'downloadId': '',
           'data': {'droppedPath': other, 'importedPath': IMPORTED}}]
    w = World(monkeypatch, tmp_path, events=ev)
    touched = []
    real_exists, real_stat = os.path.exists, os.stat
    monkeypatch.setattr(os.path, 'exists', lambda p, *a, **k: (touched.append(str(p)), real_exists(p))[1])
    monkeypatch.setattr(os, 'stat', lambda p, *a, **k: (touched.append(str(p)), real_stat(p, *a, **k))[1])
    r = aw.restore_moved_files()
    assert r['restored'] == [] and r['source_missing'] == [] and r['errors'] == []
    assert not any('Other' in t or 'Thing' in t for t in touched)
    assert not any('TV Shows' in t for t in touched)
    assert not any(c[0] == 'core.get_torrent_status' for c in w.calls)


def test_manual_import_uses_copy_mode():
    for fn in (aw._try_radarr_manual_import, aw._try_sonarr_manual_import):
        src = inspect.getsource(fn)
        assert "'importMode': 'copy'" in src and "'importMode': 'auto'" not in src


def test_revive_rescan_uses_copy_mode():
    src = inspect.getsource(aw)
    line = [ln for ln in src.splitlines() if "'path': save_path" in ln and 'scan_cmd' in ln]
    assert len(line) == 1 and "'importMode': 'copy'" in line[0]
    assert "'importMode': 'auto'" not in src
