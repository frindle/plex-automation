"""Library RAR sets: unpack when the *arr is missing a video in the set,
delete when the *arr already has every one, leave alone (skip) when unsure,
and retry on lsar/API errors. Also the torrent-level RAR check used by the
21-day seed cleanup. Run: python test_rar_set_action.py"""
import importlib.util
import json
import os
import tempfile
import types

import rar_extract

# A private copy of the module: stubs here must not leak into other tests.
_spec = importlib.util.spec_from_file_location(
    'arr_webhook_rar_action', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)
aw.DELUGE_PATH_MAP = [('/data/Media/TV Shows', '/media/tv'), ('/data/Media/Movies', '/media/movies')]
aw.SONARR_API_KEY = aw.RADARR_API_KEY = 'k'
aw.SONARR_URL, aw.RADARR_URL = 'http://sonarr', 'http://radarr'

SERIES = [{'id': 1, 'title': 'Show', 'path': '/data/Media/TV Shows/Show'},
          {'id': 2, 'title': 'Show Two', 'path': '/data/Media/TV Shows/Show Two'}]
MOVIES = [{'id': 9, 'title': 'Film', 'path': '/data/Media/Movies/Film (2001)', 'hasFile': True},
          {'id': 8, 'title': 'Other', 'path': '/data/Media/Movies/Other (2002)', 'hasFile': False}]


class _Resp:
    def __init__(self, p, status=200):
        self._p, self.status = p, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f'HTTP {self.status}')


def _requests(episodes=None, fail=None):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if fail and fail in url:
            return _Resp(None, 503)
        if url.endswith('/api/v3/series'):
            return _Resp(SERIES)
        if url.endswith('/api/v3/movie'):
            return _Resp(MOVIES)
        if url.endswith('/api/v3/episode'):
            return _Resp([{'seasonNumber': s, 'episodeNumber': e, 'hasFile': h}
                          for (s, e), h in (episodes or {}).items()])
        raise AssertionError(url)
    return types.SimpleNamespace(get=get, calls=calls)


def _set(first):
    return {'first': first, 'volumes': [first], 'extras': []}


def _ls(*files):
    return lambda first: {'ok': True, 'files': list(files)}


TV = '/media/tv/Show/Season 1/x.rar'


def test_episodes_in_name():
    e = aw._episodes_in_name
    assert e('Show.S01E05.720p.mkv') == [(1, 5)]
    assert e('dir/Show.S02E01E02.mkv') == [(2, 1), (2, 2)]
    assert e('Show.S02E01-E03.mkv') == [(2, 1), (2, 2), (2, 3)]
    assert e('Show.S02E01-03.1080p.mkv') == [(2, 1), (2, 2), (2, 3)]
    assert e('Show.S02E01-720p.mkv') == [(2, 1)]
    assert e('show.s10e100.mkv') == [(10, 100)]
    assert e('Show.2019.10.01.mkv') == []
    assert e('Show 1x05.mkv') == []


def test_torrent_has_rar():
    f = aw._torrent_has_rar
    assert f([{'path': 'R/r.rar'}, {'path': 'R/r.r00'}, {'path': 'R/r.nfo'}])
    assert not f([{'path': 'R/r.mkv'}, {'path': 'R/subs.rar'}])
    assert not f([{'path': 'R/r.mkv'}])
    assert not f(None)
    # a sample video alone does not make it a plain-video release
    assert f([{'path': 'R/r.rar'}, {'path': 'R/Sample/r-sample.mkv'}])


def test_sonarr_all_present_deletes():
    aw.requests = _requests({(1, 1): True, (1, 2): True})
    d = aw.rar_set_action(_set(TV), {}, _ls('Show.S01E01.mkv', 'Show.S01E02.mkv', 'Show.S01E01.nfo'))
    assert d['action'] == 'delete' and d['arr'] == 'Show', d


def test_sonarr_one_missing_unpacks():
    aw.requests = _requests({(1, 1): True, (1, 2): False})
    d = aw.rar_set_action(_set(TV), {}, _ls('Show.S01E01.mkv', 'Show.S01E02.mkv'))
    assert d['action'] == 'unpack' and '1 of 2' in d['reason'], d


def test_sonarr_unknown_episode_skips():
    aw.requests = _requests({(1, 1): True})
    d = aw.rar_set_action(_set(TV), {}, _ls('Show.S01E01.mkv', 'Show.S01E07.mkv'))
    assert d['action'] == 'skip' and 'S01E07' in d['reason'], d


def test_sonarr_unparsed_name_skips():
    aw.requests = _requests({(1, 1): True})
    d = aw.rar_set_action(_set(TV), {}, _ls('show-episode-one.mkv'))
    assert d['action'] == 'skip' and 'SxxEyy' in d['reason'], d


def test_deepest_series_folder_wins():
    aw.requests = _requests({(1, 1): True})
    d = aw.rar_set_action(_set('/media/tv/Show Two/S1/x.rar'), {}, _ls('Show.Two.S01E01.mkv'))
    assert d['arr'] == 'Show Two', d


def test_no_video_inside_skips():
    aw.requests = _requests()
    d = aw.rar_set_action(_set(TV), {}, _ls('subs/en.srt', 'Show.S01E01-sample.mkv'))
    assert d['action'] == 'skip' and aw.requests.calls == [], d


def test_lsar_failure_is_error():
    aw.requests = _requests()
    d = aw.rar_set_action(_set(TV), {}, lambda f: {'ok': False, 'error': 'lsar exit 1'})
    assert d['action'] == 'error', d


def test_api_failure_is_error():
    aw.requests = _requests({(1, 1): True}, fail='/episode')
    d = aw.rar_set_action(_set(TV), {}, _ls('Show.S01E01.mkv'))
    assert d['action'] == 'error', d
    aw.requests = _requests(fail='/series')
    d = aw.rar_set_action(_set(TV), {}, _ls('Show.S01E01.mkv'))
    assert d['action'] == 'error', d


def test_radarr():
    aw.requests = _requests()
    cache = {}
    d = aw.rar_set_action(_set('/media/movies/Film (2001)/f.rar'), cache, _ls('Film.2001.mkv'))
    assert d['action'] == 'delete' and d['arr'] == 'Film', d
    d = aw.rar_set_action(_set('/media/movies/Other (2002)/o.rar'), cache, _ls('Other.2002.mkv'))
    assert d['action'] == 'unpack', d
    # the item lists are fetched once per pass
    assert sum(u.endswith('/movie') for u in aw.requests.calls) == 1


def test_no_owner_skips():
    aw.requests = _requests()
    d = aw.rar_set_action(_set('/media/movies/Nobody/n.rar'), {}, _ls('Nobody.mkv'))
    assert d['action'] == 'skip', d


def test_process_queue_routes_each_action():
    with tempfile.TemporaryDirectory() as d:
        qp = os.path.join(d, 'q.json')
        q = [dict(_set(f'/media/tv/Show/{n}.rar'), attempts=0) for n in ('del', 'unp', 'skp', 'err')]
        q.append(dict(_set('/media/tv/Show/old.rar'), attempts=3, status='failed'))
        with open(qp, 'w') as f:
            json.dump(q, f)
        acts = {'del': 'delete', 'unp': 'unpack', 'skp': 'skip', 'err': 'error'}
        names = ('RAR_QUEUE_PATH', 'deluge_login', '_owned_local_paths', 'record_activity',
                 '_rescan_arr_for', 'rar_set_action', 'rar_extract')
        saved = {k: getattr(aw, k) for k in names}
        try:
            _run_queue(qp, acts)
        finally:
            for k, v in saved.items():
                setattr(aw, k, v)


def _run_queue(qp, acts):
    seen, deleted, unpacked = [], [], []
    aw.RAR_QUEUE_PATH = qp
    aw.deluge_login = lambda: None
    aw._owned_local_paths = lambda: set()
    aw.record_activity = lambda *a, **k: None
    aw._rescan_arr_for = lambda p: 'rescanned'

    def decide(e, cache):
        seen.append(e['first'])
        return {'action': acts[os.path.basename(e['first'])[:3]], 'reason': 'r'}
    aw.rar_set_action = decide
    aw.rar_extract = types.SimpleNamespace(
        load_queue=rar_extract.load_queue, save_queue=rar_extract.save_queue,
        MAX_ATTEMPTS=rar_extract.MAX_ATTEMPTS,
        delete_set=lambda e: deleted.append(e['first']) or {'ok': True, 'removed': [e['first']]},
        extract=lambda e: unpacked.append(e['first']) or {'ok': True, 'files': ['x.mkv']})
    r = aw.process_rar_queue()
    assert r['ok'], r
    assert '/media/tv/Show/old.rar' not in seen
    assert deleted == ['/media/tv/Show/del.rar'] and unpacked == ['/media/tv/Show/unp.rar']
    left = {e['first']: e for e in rar_extract.load_queue(qp)}
    assert set(left) == {'/media/tv/Show/skp.rar', '/media/tv/Show/err.rar', '/media/tv/Show/old.rar'}
    assert left['/media/tv/Show/skp.rar']['status'] == 'skipped'
    assert left['/media/tv/Show/err.rar']['attempts'] == 1 and 'status' not in left['/media/tv/Show/err.rar']
    assert [x['action'] for x in r['done']] == ['delete', 'unpack']
    assert len(r['skipped']) == 1 and len(r['failed']) == 1
    # a second pass leaves the skipped set alone and retries the error
    seen.clear()
    aw.process_rar_queue()
    assert seen == ['/media/tv/Show/err.rar'], seen


if __name__ == '__main__':
    for n, f in list(globals().items()):
        if n.startswith('test_') and callable(f):
            f()
            print('ok', n)
