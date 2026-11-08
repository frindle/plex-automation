"""_library_has_file accepts copy-mode imports proven by *arr history, and
keeps files whenever that proof is missing or a lookup fails.
Run: python test_library_has_file_copy_import.py"""
import os
import tempfile

import episode_retention

aw = __import__('arr-webhook')

HASH = 'abcdef0123456789'
DL = '/data/Downloads/Complete'
LIBDIR = '/data/Media/TV Shows'


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f'HTTP {self.status}')


def _setup(d, files, lib_files):
    dl, lib = os.path.join(d, 'dl'), os.path.join(d, 'lib')
    for rel in files:
        os.makedirs(os.path.dirname(os.path.join(dl, rel)), exist_ok=True)
        open(os.path.join(dl, rel), 'wb').close()
    for rel in lib_files:
        os.makedirs(os.path.dirname(os.path.join(lib, rel)), exist_ok=True)
        open(os.path.join(lib, rel), 'wb').close()
    aw.DELUGE_PATH_MAP = [(DL, dl), (LIBDIR, lib)]
    aw.RADARR_API_KEY = aw.SONARR_API_KEY = 'k'
    aw._arr_extras_cache.clear()
    return dl, lib


def _torrent(files, h=HASH):
    return {'hash': h, 'save_path': DL, 'files': [{'path': p} for p in files]}


def _fake_get(history, extras=None, fail=None, calls=None):
    """history: list of (dropped_rel, imported_rel) for Sonarr; Radarr has none."""
    def get(url, headers=None, params=None, timeout=None):
        if calls is not None:
            calls.append((url, params))
        if fail and fail in url:
            raise RuntimeError('down')
        if url.endswith('/config/mediamanagement'):
            if extras is None:
                return _Resp({'importExtraFiles': False})
            return _Resp({'importExtraFiles': True, 'extraFileExtensions': extras})
        assert params['downloadId'] == HASH.upper(), params
        if url.startswith(aw.SONARR_URL):
            recs = [{'eventType': 'grabbed', 'data': {}}] + [
                {'eventType': 'downloadFolderImported',
                 'data': {'droppedPath': f'{DL}/{a}', 'importedPath': f'{LIBDIR}/{b}'}}
                for a, b in history]
            return _Resp({'records': recs})
        return _Resp({'records': []})
    return get


EP = 'Show.S01E05.1080p/Show.S01E05.1080p.mkv'
LIBEP = 'Show/Season 01/Show - S01E05.mkv'


def run():
    orig_get = aw.requests.get
    try:
        with tempfile.TemporaryDirectory() as d:
            # copy-imported single episode + nfo + sample -> True
            files = [EP, 'Show.S01E05.1080p/Show.nfo', 'Show.S01E05.1080p/Sample/sample.mkv']
            _setup(d, files, [LIBEP])
            calls = []
            aw.requests.get = _fake_get([(EP, LIBEP)], calls=calls)
            assert aw._library_has_file(_torrent(files)) is True
            assert any(p and p.get('downloadId') == HASH.upper() for _, p in calls)

        with tempfile.TemporaryDirectory() as d:
            # history says imported, but the library file is gone (renamed/deleted) -> keep
            _setup(d, [EP], [])
            aw.requests.get = _fake_get([(EP, LIBEP)])
            assert aw._library_has_file(_torrent([EP])) is False

        with tempfile.TemporaryDirectory() as d:
            # no import record -> keep
            _setup(d, [EP], [LIBEP])
            aw.requests.get = _fake_get([])
            assert aw._library_has_file(_torrent([EP])) is False

        with tempfile.TemporaryDirectory() as d:
            # either *arr lookup failing -> keep (fail closed)
            _setup(d, [EP], [LIBEP])
            for down in (aw.RADARR_URL, aw.SONARR_URL):
                aw.requests.get = _fake_get([(EP, LIBEP)], fail=down)
                assert aw._library_has_file(_torrent([EP])) is False, down

        with tempfile.TemporaryDirectory() as d:
            # no hash on the torrent dict -> keep
            _setup(d, [EP], [LIBEP])
            aw.requests.get = _fake_get([(EP, LIBEP)])
            t = _torrent([EP])
            t.pop('hash')
            assert aw._library_has_file(t) is False

        with tempfile.TemporaryDirectory() as d:
            # importedPath outside the media tree never counts
            _setup(d, [EP], [])
            dl = os.path.join(d, 'dl')
            aw.requests.get = lambda url, **kw: _Resp({'records': [] if aw.RADARR_URL in url else [
                {'eventType': 'downloadFolderImported',
                 'data': {'droppedPath': f'{DL}/{EP}', 'importedPath': f'{DL}/{EP}'}}]})
            assert os.path.isfile(os.path.join(dl, EP))
            assert aw._library_has_file(_torrent([EP])) is False

        with tempfile.TemporaryDirectory() as d:
            # season pack: one episode not imported -> keep the whole pack
            e1, e2 = 'Pack/Show.S01E01.mkv', 'Pack/Show.S01E02.mkv'
            l1, l2 = 'Show/Season 01/E01.mkv', 'Show/Season 01/E02.mkv'
            _setup(d, [e1, e2], [l1, l2])
            aw.requests.get = _fake_get([(e1, l1)])
            assert aw._library_has_file(_torrent([e1, e2])) is False
            aw.requests.get = _fake_get([(e1, l1), (e2, l2)])
            assert aw._library_has_file(_torrent([e1, e2])) is True

        with tempfile.TemporaryDirectory() as d:
            # subtitles: kept unless the *arr imports that extension as an extra file
            sub = 'Show.S01E05.1080p/Subs/English.srt'
            _setup(d, [EP, sub], [LIBEP])
            aw.requests.get = _fake_get([(EP, LIBEP)], extras=None)
            assert aw._library_has_file(_torrent([EP, sub])) is False
            aw._arr_extras_cache.clear()
            aw.requests.get = _fake_get([(EP, LIBEP)], extras='srt,nfo')
            assert aw._library_has_file(_torrent([EP, sub])) is True
            aw._arr_extras_cache.clear()
            aw.requests.get = _fake_get([(EP, LIBEP)], extras='ass')
            assert aw._library_has_file(_torrent([EP, sub])) is False
            aw._arr_extras_cache.clear()
            aw.requests.get = _fake_get([(EP, LIBEP)], extras='srt', fail='/config/')
            assert aw._library_has_file(_torrent([EP, sub])) is False

        with tempfile.TemporaryDirectory() as d:
            # hardlinked main video needs no history lookup at all
            dl, lib = _setup(d, [EP], [])
            os.makedirs(os.path.join(lib, 'Show'), exist_ok=True)
            os.link(os.path.join(dl, EP), os.path.join(lib, 'Show', 'x.mkv'))

            def boom(*a, **k):
                raise AssertionError('no lookup expected')
            aw.requests.get = boom
            assert aw._library_has_file(_torrent([EP])) is True

        with tempfile.TemporaryDirectory() as d:
            # only extras / no main video -> keep; missing file -> keep
            _setup(d, ['X/sample.mkv', 'X/info.nfo'], [])
            aw.requests.get = _fake_get([])
            assert aw._library_has_file(_torrent(['X/sample.mkv', 'X/info.nfo'])) is False
            assert aw._library_has_file(_torrent(['X/nope.mkv'])) is False
            assert aw._library_has_file(_torrent([])) is False
    finally:
        aw.requests.get = orig_get

    # extras classification
    assert episode_retention.is_main_video('Movie (2020)/Movie.2020.2160p.mkv')
    assert not episode_retention.is_main_video('Movie/Featurettes/Making Of.mkv')
    assert not episode_retention.is_main_video('Movie/Movie-trailer.mkv')
    assert not episode_retention.is_main_video('Movie/sample-movie.mkv')
    assert not episode_retention.is_main_video('Movie/movie.nfo')

    # retention threads the hash into the library check
    seen = []
    torrents = {'h1': {'name': 'Show.S01E05.1080p', 'label': 'sonarr', 'state': 'Seeding',
                       'progress': 100.0, 'seeding_time': 40 * 86400, 'save_path': DL,
                       'files': [{'path': EP}]}}
    episode_retention.episode_retention(torrents, lambda h, rd: None,
                                        lambda t: seen.append(t.get('hash')) or True, env={})
    assert seen == ['h1'], seen
    print('test_library_has_file_copy_import: all assertions passed')


def test_library_has_file_copy_import():
    run()


if __name__ == '__main__':
    run()
