"""Stream guard: nothing deletes or unlinks a library file while a viewer is on it. Plex, Deluge and the arrs are
faked; names and paths are made up."""
import importlib.util

import pytest

import watch_hold as wh

spec = importlib.util.spec_from_file_location("aw_stream_guard", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

NOW = 1_800_000_000.0
FILM = '/data/Media/Movies/Some Film (2001)/Some Film (2001).mkv'
OTHER = '/data/Media/Movies/Other Film (2002)/Other Film (2002).mkv'


def sess(*files, key='7'):
    return {'key': key, 'files': list(files), 'state': 'playing'}


# ── watch_hold.busy ───────────────────────────────────────────────────────────

def test_busy_matches_a_live_session_by_file_name():
    assert wh.busy([FILM], [sess('/media/movies/Some Film (2001)/Some Film (2001).mkv')], {}, NOW, 900)
    assert not wh.busy([OTHER], [sess(FILM)], {}, NOW, 900)


def test_busy_holds_through_a_pause_then_lets_go():
    seen = {'f:' + wh.base(FILM): NOW - 600}
    assert wh.busy([FILM], [], seen, NOW, 900)
    assert not wh.busy([FILM], [], seen, NOW + 400, 900)


def test_busy_unreadable_plex_counts_as_busy_but_no_paths_never_is():
    assert wh.busy([FILM], None, {}, NOW, 900)
    assert not wh.busy([], None, {}, NOW, 900)
    assert not wh.busy(['', None], [sess(FILM)], {}, NOW, 900)


# ── stream_guard / stream_busy ────────────────────────────────────────────────

class FakeWatchIO:
    def __init__(self, sessions=None, error=False):
        self._sessions, self.error, self.reads = sessions or [], error, 0

    def sessions(self):
        self.reads += 1
        if self.error:
            raise RuntimeError('plex down')
        return self._sessions


@pytest.fixture
def plex(monkeypatch):
    io = FakeWatchIO()
    monkeypatch.setattr(aw, '_watch_io', lambda: io)
    monkeypatch.setattr(aw, 'STREAM_GUARD', True)
    monkeypatch.setitem(aw._watch_doc, 'seen', {})
    return io


def test_guard_reads_plex_once_for_many_paths(plex):
    plex._sessions = [sess(FILM)]
    playing = aw.stream_guard()
    assert playing(FILM) and not playing(OTHER) and not playing('')
    assert plex.reads == 1


def test_guard_uses_recently_seen_sessions(plex):
    aw._watch_doc['seen'] = {'f:' + wh.base(FILM): aw.time.time() - 60}
    assert aw.stream_busy([OTHER, FILM]) and not aw.stream_busy([OTHER])


def test_guard_fails_closed_unless_asked_not_to(plex):
    plex.error = True
    assert aw.stream_busy([FILM]) is True
    assert aw.stream_busy([FILM], fail_closed=False) is False


def test_guard_off_reads_nothing(plex, monkeypatch):
    monkeypatch.setattr(aw, 'STREAM_GUARD', False)
    plex._sessions = [sess(FILM)]
    assert aw.stream_busy([FILM]) is False and plex.reads == 0


# ── glacier release keeps a stopgap link someone is playing ───────────────────

def test_glacier_keeps_a_link_being_played(plex, monkeypatch):
    monkeypatch.setattr(aw, 'watch_gate', lambda h: None)
    link = '/data/Media/Movies/Some Film (2001)/Some Film (2001) - glacier-tmp.mkv'
    plex._sessions = [sess(link)]
    assert aw._glacier_keep('a' * 40, {'link': link}) is True
    plex._sessions = []
    assert aw._glacier_keep('a' * 40, {'link': link}) is False
    assert aw._glacier_keep('a' * 40, {}) is False


def test_glacier_release_goes_ahead_when_plex_is_unreadable(plex, monkeypatch):
    monkeypatch.setattr(aw, 'watch_gate', lambda h: None)
    plex.error = True
    assert aw._glacier_keep('a' * 40, {'link': FILM}) is False


def test_glacier_still_keeps_for_watch_hold(plex, monkeypatch):
    monkeypatch.setattr(aw, 'watch_gate', lambda h: 'playing')
    assert aw._glacier_keep('a' * 40, {}) is True and plex.reads == 0


# ── manual deletes ────────────────────────────────────────────────────────────

@pytest.fixture
def lib(tmp_path, monkeypatch, plex):
    monkeypatch.setattr(aw, 'MOVIES_LIBRARY', str(tmp_path))
    a, b = tmp_path / 'Some Film (2001).mkv', tmp_path / 'Other Film (2002).mkv'
    a.write_bytes(b'x')
    b.write_bytes(b'y')
    return a, b


def test_delete_paths_skips_a_file_being_played(lib, plex):
    a, b = lib
    plex._sessions = [sess(FILM)]
    r = aw.app.test_client().post('/delete-paths?apply=1', json={'paths': [str(a), str(b)]}).get_json()
    assert a.exists() and not b.exists()
    assert [s['reason'] for s in r['results']['skipped']] == ['playing now (or Plex unreadable); try again later']


def test_delete_paths_deletes_nothing_when_plex_is_unreadable(lib, plex):
    a, b = lib
    plex.error = True
    r = aw.app.test_client().post('/delete-paths?apply=1', json={'paths': [str(a), str(b)]}).get_json()
    assert a.exists() and b.exists() and r['skipped_count'] == 2 and r['deleted_count'] == 0


def test_delete_paths_dry_run_does_not_ask_plex(lib, plex):
    a, _ = lib
    r = aw.app.test_client().post('/delete-paths', json={'paths': [str(a)]}).get_json()
    assert r['results']['deleted'][0]['action'] == 'would delete' and plex.reads == 0


# ── arr_health asks before replacing ──────────────────────────────────────────

def test_arr_health_io_busy_uses_the_guard(plex):
    plex._sessions = [sess(FILM)]
    io = aw.ArrHealthIO.__new__(aw.ArrHealthIO)
    assert io.busy(FILM) is True and io.busy(OTHER) is False


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_orphan_scan_leaves_a_dupe_someone_is_playing(tmp_path, monkeypatch, plex):
    movies = [{'title': 'Some Film', 'year': 2001, 'movieFile': {'path': '/m/Some Film (2001) 1080p.mkv'}},
              {'title': 'Other Film', 'year': 2002, 'movieFile': {'path': '/m/Other Film (2002) 1080p.mkv'}}]
    monkeypatch.setattr(aw, 'MOVIES_LIBRARY', str(tmp_path))
    monkeypatch.setattr(aw, 'RADARR_API_KEY', 'k')
    monkeypatch.setattr(aw.requests, 'get', lambda *a, **k: _Resp(movies))
    monkeypatch.setattr(aw, 'deluge_login', lambda *a, **k: None)
    monkeypatch.setattr(aw.session, 'post', lambda *a, **k: _Resp({'result': {}}))
    a, b = tmp_path / 'Some Film (2001) 720p.mkv', tmp_path / 'Other Film (2002) 720p.mkv'
    a.write_bytes(b'x')
    b.write_bytes(b'y')
    plex._sessions = [sess('/media/movies/Some Film (2001) 720p.mkv')]
    r = aw.app.test_client().post('/orphan-scan?delete=1&mode=dupes', json={}).get_json()
    assert a.exists() and not b.exists()
    assert r['deleted'] == 1 and r['skipped_playing'] == 1
