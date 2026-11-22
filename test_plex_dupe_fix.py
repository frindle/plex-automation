"""/plex-dupe-fix: only an identical full path is a ghost, a same-name copy never borrows the keeper's torrent,
the keeper's own file is never deleted, only copies on disk are ranked (a Plex ghost of a replaced file
never wins and gets the real file deleted), and the daily run writes a report and pushes only on apply failures."""
import json
import os

import pytest

aw = __import__('arr-webhook')


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


def _m(path, res='1080', size=5.0, mid=1):
    return {'file': path, 'resolution': res, 'size_gb': size, 'media_id': mid}


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {'entries': [], 'torrents': {}, 'superseded': []}

    def scan():
        return aw.jsonify({'ok': True, 'multi_version': state['entries']}), 200

    monkeypatch.setattr(aw, 'PLEX_TOKEN', 'tok')
    monkeypatch.setattr(aw, 'plex_dupe_scan', scan)
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'ensure_label_exists', lambda: None)
    monkeypatch.setattr(aw, '_load_plex_dupe_keep', lambda: set())
    monkeypatch.setattr(aw, 'session', type('S', (), {
        'post': staticmethod(lambda *a, **k: _Resp({'result': state['torrents']}))})())
    monkeypatch.setattr(aw, 'supersede_torrent', lambda h: state['superseded'].append(h))
    # Plex paths are written relative to tmp_path in these tests.
    monkeypatch.setattr(aw, '_translate_plex_path', lambda p: str(tmp_path / p.lstrip('/')))
    # *arr views: local paths are the arr paths here; movies/series/episodefiles come from state['arr'].
    state['arr'] = {'movie': [], 'series': [], 'episodefile': {}}
    monkeypatch.setattr(aw, '_local_to_deluge', lambda p: p)
    monkeypatch.setattr(aw, '_arr_items', lambda kind, cache: state['arr'][kind])
    monkeypatch.setattr(aw, '_arr_episode_files', lambda sid, cache: state['arr']['episodefile'].get(sid, []))
    state['root'] = tmp_path
    return state


def _file(state, rel):
    p = state['root'] / rel.lstrip('/')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'x')
    return p


def _run(apply=False):
    qs = {'apply': '1'} if apply else {}
    with aw.app.test_request_context('/plex-dupe-fix', query_string=qs):
        resp, status = aw.plex_dupe_fix()
    assert status == 200
    return resp.get_json()


def test_identical_full_path_is_a_ghost(world):
    f = '/media/movies/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(f), _m(f, mid=2)]}]
    out = _run()
    assert out['counts']['same_file_ghost'] == 1 and out['counts']['quality_upgrade'] == 0


def test_same_filename_in_two_folders_is_not_a_ghost(world):
    a = '/media/movies/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    b = '/media/movies2/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    _file(world, a), _file(world, b)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(a, size=6), _m(b, size=5, mid=2)]}]
    out = _run()
    assert out['counts']['same_file_ghost'] == 0 and out['counts']['quality_upgrade'] == 1
    sup = out['plan']['quality_upgrade'][0]['supersede']
    assert [s['file'] for s in sup] == [b]


def test_duplicate_keeper_path_never_becomes_a_loser(world):
    keep = '/media/movies/Some Movie (2021)/Some.Movie.2021.2160p.mkv'
    low = '/media/movies/Some Movie (2021)/Some.Movie.2021.720p.mkv'
    _file(world, keep), _file(world, low)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie',
                         'media': [_m(keep, '4k', 40), _m(keep, '4k', 40, 2), _m(low, '720', 2, 3)]}]
    out = _run()
    sup = out['plan']['quality_upgrade'][0]['supersede']
    assert [s['file'] for s in sup] == [low]


def test_same_name_copy_with_a_seeding_torrent_is_skipped_not_deleted(world):
    a = '/media/movies/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    b = '/media/movies2/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    _file(world, a)
    fb = _file(world, b)
    world['torrents'] = {'H1': {'name': 'Some.Movie.2021.1080p.mkv', 'save_path': '/data/Downloads/movies',
                                'label': 'radarr', 'files': [{'path': 'Some.Movie.2021.1080p.mkv'}]}}
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(a, size=6), _m(b, size=5, mid=2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['torrent_hash'] is None and act['result'].startswith('skipped:')
    assert world['superseded'] == [] and fb.exists()


def test_same_name_copy_without_any_torrent_is_deleted(world):
    a = '/media/movies/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    b = '/media/movies2/Some Movie (2021)/Some.Movie.2021.1080p.mkv'
    fa, fb = _file(world, a), _file(world, b)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(a, size=6), _m(b, size=5, mid=2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('deleted ') and not fb.exists() and fa.exists()


def test_different_name_loser_still_uses_basename_torrent_match(world):
    keep = '/media/movies/Some Movie (2021)/Some.Movie.2021.2160p.mkv'
    low = '/media/movies/Some Movie (2021)/Some.Movie.2021.720p.mkv'
    _file(world, keep), _file(world, low)
    world['torrents'] = {'H2': {'name': 'Some.Movie.2021.720p.mkv', 'save_path': '/data/Downloads/movies',
                                'label': 'radarr', 'files': []}}
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(keep, '4k', 40), _m(low, '720', 2, 2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['torrent_hash'] == 'H2' and world['superseded'] == ['H2']


def test_loser_that_is_the_keepers_file_on_disk_is_not_deleted(world):
    keep = '/media/movies/Some Movie (2021)/Some.Movie.2021.2160p.mkv'
    alias = '/media/alias/Some.Movie.2021.2160p.MKV'
    fk = _file(world, keep)
    al = world['root'] / alias.lstrip('/')
    al.parent.mkdir(parents=True)
    os.symlink(fk, al)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie',
                         'media': [_m(keep, '4k', 40), _m(alias, '720', 2, 2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'] == 'skipped: same file on disk as the keeper' and fk.exists()


def test_same_physical_file_rules(tmp_path):
    a = tmp_path / 'a.mkv'
    a.write_bytes(b'x')
    link = tmp_path / 'hard.mkv'
    os.link(a, link)
    other = tmp_path / 'b.mkv'
    other.write_bytes(b'y')
    assert aw._same_physical_file(str(a), str(a))
    assert not aw._same_physical_file(str(a), str(link))  # separate hardlinks
    assert not aw._same_physical_file(str(a), str(other))
    assert not aw._same_physical_file('', str(a))


# ── daily run ──

@pytest.fixture
def daily(monkeypatch, tmp_path):
    seen = {'pushes': [], 'activity': [], 'body': None, 'qs': []}
    report = tmp_path / 'r' / 'last.json'
    monkeypatch.setattr(aw, 'PLEX_DUPE_FIX_REPORT', str(report))
    monkeypatch.setattr(aw, 'send_pushover', lambda title, message, url=None: seen['pushes'].append((title, message)))
    monkeypatch.setattr(aw, 'record_activity', lambda cat, summary: seen['activity'].append((cat, summary)))

    def fake_fix():
        seen['qs'].append(dict(aw.request.args))
        body = seen['body']
        if isinstance(body, Exception):
            raise body
        return aw.jsonify(body), (200 if body.get('ok') else 500)
    monkeypatch.setattr(aw, 'plex_dupe_fix', fake_fix)
    seen['report'] = report
    return seen


def _body(results):
    return {'ok': True, 'apply': True,
            'counts': {'quality_upgrade': 1, 'same_file_ghost': 2},
            'plan': {'quality_upgrade': [{'title': 'T', 'supersede': [
                {'basename': f'f{i}.mkv', 'result': r} for i, r in enumerate(results)]}]}}


def test_daily_off_does_nothing(monkeypatch, daily):
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'off')
    assert aw.plex_dupe_fix_daily() is None
    assert daily['qs'] == [] and not daily['report'].exists()


def test_daily_defaults_to_dry_and_writes_report(monkeypatch, daily):
    monkeypatch.delenv('PLEX_DUPE_FIX_DAILY', raising=False)
    daily['body'] = _body([None, None])
    rep = aw.plex_dupe_fix_daily()
    assert daily['qs'] == [{}] and rep['mode'] == 'dry'
    saved = json.loads(daily['report'].read_text())
    assert saved['counts'] == {'quality_upgrade': 1, 'same_file_ghost': 2} and saved['failures'] == []
    assert daily['pushes'] == []
    assert daily['activity'] == [('plex_dupe_fix', 'dry: 1 titles, 2 extra copies, 2 ghosts, 0 stale, 0 failed')]


def test_daily_bogus_mode_is_dry(monkeypatch, daily):
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'yes please')
    daily['body'] = _body([])
    assert aw.plex_dupe_fix_daily()['mode'] == 'dry' and daily['qs'] == [{}]


def test_daily_apply_pushes_only_on_failures(monkeypatch, daily):
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'apply')
    daily['body'] = _body(['deleted /x', 'torrent superseded + moved to /s', 'skipped: check by hand'])
    rep = aw.plex_dupe_fix_daily()
    assert daily['qs'] == [{'apply': '1'}] and rep['failures'] == [] and daily['pushes'] == []
    daily['body'] = _body(['deleted /x', 'PERMISSION DENIED at /y — mount may be ro', 'FAILED: boom'])
    rep = aw.plex_dupe_fix_daily()
    assert len(rep['failures']) == 2 and len(daily['pushes']) == 1
    assert 'PERMISSION DENIED' in daily['pushes'][0][1] and 'FAILED: boom' in daily['pushes'][0][1]


def test_daily_dry_never_pushes_even_when_scan_fails(monkeypatch, daily):
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'dry')
    daily['body'] = {'ok': False, 'error': 'scan failed: plex down'}
    rep = aw.plex_dupe_fix_daily()
    assert rep['ok'] is False and daily['pushes'] == []
    assert daily['activity'][-1][1] == 'dry: did not run (scan failed: plex down)'


def test_daily_apply_pushes_when_the_run_crashes(monkeypatch, daily):
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'apply')
    daily['body'] = RuntimeError('deluge gone')
    rep = aw.plex_dupe_fix_daily()
    assert rep['status'] == 500 and len(daily['pushes']) == 1 and 'deluge gone' in daily['pushes'][0][1]
    assert json.loads(daily['report'].read_text())['ok'] is False


def test_scheduler_runs_daily_fix_and_survives_its_crash(monkeypatch):
    calls = []
    for name in ('cleanup_superseded', 'cleanup_radarr_queue_dupes', 'cleanup_sonarr_queue_dupes',
                 'dedup_via_radarr', 'dedup_via_sonarr', 'cleanup_unpacked_torrents'):
        monkeypatch.setattr(aw, name, lambda name=name: calls.append(name))

    def boom():
        calls.append('plex_dupe_fix_daily')
        raise RuntimeError('x')
    monkeypatch.setattr(aw, 'plex_dupe_fix_daily', boom)

    class Stop(Exception):
        pass

    def stop(_s):
        raise Stop
    monkeypatch.setattr(aw.time, 'sleep', stop)
    with pytest.raises(Stop):
        aw.cleanup_scheduler()
    assert calls.index('plex_dupe_fix_daily') == calls.index('dedup_via_sonarr') + 1
    assert calls[-1] == 'cleanup_unpacked_torrents'


def test_same_name_copy_uses_a_torrent_that_matches_only_its_own_folder(world):
    a = '/media/movies/A/Some.Movie.2021.1080p.mkv'
    b = '/media/movies/B/Some.Movie.2021.1080p.mkv'
    _file(world, a), _file(world, b)
    world['torrents'] = {
        'HB': {'name': 'B', 'save_path': '/data/Downloads', 'label': 'radarr',
               'files': [{'path': 'B/Some.Movie.2021.1080p.mkv'}]},
        'HT': {'name': 'Some.Movie.2021.1080p.mkv', 'save_path': '/data/Downloads', 'label': 'radarr',
               'files': [{'path': 'Some.Movie.2021.1080p.mkv'}]},
    }
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(a, size=6), _m(b, size=5, mid=2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['torrent_hash'] == 'HB' and world['superseded'] == ['HB']


def test_torrent_path_hits_lists_every_match():
    t = {'X': {'name': 'f.mkv', 'save_path': '/d', 'files': [{'path': 'f.mkv'}]},
         'Y': {'name': 'Show', 'save_path': '/d', 'files': [{'path': 'Show/f.mkv'}]},
         'Z': {'name': 'g.mkv', 'save_path': '/d', 'files': [{'path': 'g.mkv'}]}}
    assert aw._torrent_path_hits(t, '/media/Show/f.mkv') == ['X', 'Y']
    assert aw._torrent_path_hits(t, '/d/f.mkv') == ['X']
    assert aw._torrent_path_hits(t, '') == []


# ── copies missing from disk (2026-11-20: Plex still listed the WEBRips an upgrade replaced; the bigger ghost
#    won the ranking and the real Bluray files were deleted) ──

def test_ghost_keeper_never_gets_the_real_file_deleted(world):
    ghost = '/media/tv/Show/Show.S01E01.1080p.WEBRip.x265.mkv'
    real = '/media/tv/Show/Show - S01E01 - Pilot Bluray-1080p.mkv'
    fr = _file(world, real)
    world['entries'] = [{'plex_key': '1', 'title': 'Show S01E01',
                         'media': [_m(ghost, size=1.83), _m(real, size=1.12, mid=2)]}]
    out = _run(apply=True)
    assert out['counts']['quality_upgrade'] == 0 and out['counts']['stale_entry'] == 1
    st = out['plan']['stale_entry'][0]
    assert st['on_disk'] == [os.path.basename(real)] and st['missing'] == [os.path.basename(ghost)]
    assert fr.exists()


def test_no_copy_on_disk_is_stale_and_hints_at_the_path_map(world):
    a = '/media/movies/M/M.2160p.mkv'
    b = '/media/movies/M/M.1080p.mkv'
    world['entries'] = [{'plex_key': '1', 'title': 'M', 'media': [_m(a, '4k', 30), _m(b, size=8, mid=2)]}]
    out = _run(apply=True)
    assert out['counts']['quality_upgrade'] == 0
    assert 'path map' in out['plan']['stale_entry'][0]['action']


def test_ghost_is_left_out_of_the_ranking_when_two_real_copies_remain(world):
    ghost = '/media/movies/M/M.2160p.mkv'
    mid_q = '/media/movies/M/M.1080p.mkv'
    low = '/media/movies/M/M.720p.mkv'
    fm, fl = _file(world, mid_q), _file(world, low)
    world['entries'] = [{'plex_key': '1', 'title': 'M',
                         'media': [_m(ghost, '4k', 40), _m(mid_q, '1080', 5, 2), _m(low, '720', 2, 3)]}]
    out = _run(apply=True)
    q = out['plan']['quality_upgrade'][0]
    assert q['keep']['file'] == 'M.1080p.mkv' and [s['file'] for s in q['supersede']] == [low]
    assert fm.exists() and not fl.exists()


def test_keeper_gone_by_delete_time_is_skipped(world, monkeypatch):
    keep = '/media/movies/M/M.2160p.mkv'
    low = '/media/movies/M/M.720p.mkv'
    fk, fl = _file(world, keep), _file(world, low)

    def check_then_vanish(entry, keeper, loser, state):
        fk.unlink()
        return None, None
    monkeypatch.setattr(aw, '_dupe_check', check_then_vanish)
    world['entries'] = [{'plex_key': '1', 'title': 'M', 'media': [_m(keep, '4k', 40), _m(low, '720', 2, 2)]}]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'] == 'skipped: keeper not on disk' and fl.exists()


def _two_copies(world):
    a = '/media/movies/Some Movie (2021)/Some.Movie.2021.2160p.mkv'
    b = '/media/movies/Some Movie (2021)/Some.Movie.2021.720p.mkv'
    fa, fb = _file(world, a), _file(world, b)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie', 'media': [_m(a, '4k', 40), _m(b, '720', 2, 2)]}]
    return fa, fb


def test_copy_radarr_tracks_is_never_deleted(world):
    fa, fb = _two_copies(world)
    world['arr']['movie'] = [{'id': 7, 'path': str(fb.parent), 'movieFile': {'relativePath': fb.name}}]
    act = _run(apply=True)['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('skipped: Radarr tracks this copy') and fb.exists() and fa.exists()


def test_copy_is_deleted_when_radarr_tracks_the_kept_one(world):
    fa, fb = _two_copies(world)
    world['arr']['movie'] = [{'id': 7, 'path': str(fa.parent), 'movieFile': {'path': str(fa)}}]
    act = _run(apply=True)['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('deleted ') and not fb.exists() and fa.exists()


def test_episode_file_sonarr_tracks_is_never_deleted(world):
    keep = '/media/tv/Show/Season 01/Show.S01E02.1080p.mkv'
    low = '/media/tv/Show/Season 01/Show.S01E02.720p.mkv'
    fk, fl = _file(world, keep), _file(world, low)
    world['entries'] = [{'plex_key': '9', 'title': 'Show', 'media': [_m(keep, '1080', 3), _m(low, '720', 2, 2)]}]
    world['arr']['series'] = [{'id': 3, 'path': str(world['root'] / 'media/tv/Show')}]
    world['arr']['episodefile'] = {3: [{'relativePath': 'Season 01/Show.S01E02.1080p.mkv'},
                                       {'path': str(fl)}]}
    act = _run(apply=True)['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('skipped: Sonarr tracks this copy') and fl.exists() and fk.exists()


def test_arr_api_error_keeps_the_file(world, monkeypatch):
    fa, fb = _two_copies(world)

    def down(kind, cache):
        raise aw.requests.ConnectionError('down')
    monkeypatch.setattr(aw, '_arr_items', down)
    act = _run(apply=True)['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('skipped: could not ask Sonarr/Radarr') and fb.exists()
    assert not act['result'].startswith(aw._DUPE_FIX_FAIL_PREFIXES)


def test_arr_tracker_of_reads_the_real_shapes(monkeypatch):
    monkeypatch.setattr(aw, '_local_to_deluge', lambda p: p.replace('/media/movies', '/data/Media/Movies'))
    cache = {'movie': [{'id': 1, 'path': '/data/Media/Movies/A (2020)', 'movieFile': {'relativePath': 'A.mkv'}}],
             'series': []}
    assert aw._arr_tracker_of('/media/movies/A (2020)/A.mkv', cache) == 'Radarr'
    assert aw._arr_tracker_of('/media/movies/A (2020)/A.720p.mkv', cache) is None
    assert aw._arr_tracker_of('/media/movies/B (2020)/A.mkv', cache) is None
