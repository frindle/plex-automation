"""Frozen test: queue-dupe cleanups must not removeFromClient when Deluge state is unknown."""
import os
import sys

from unittest import mock

aw = __import__('arr-webhook')


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


DELETES = []
_PATCHES = []


def _teardown():
    while _PATCHES:
        _PATCHES.pop().stop()


def _setup(torrents):
    """torrents=None -> get_all_torrents raises (Deluge unreachable)."""
    _teardown()
    DELETES.clear()

    def get(url, headers=None, params=None, timeout=None, **kw):
        if url.endswith('/api/v3/queue'):
            return _Resp({'records': QUEUE})
        if url.endswith('/api/v3/history'):
            return _Resp({'records': []})
        raise AssertionError(f'unexpected GET {url}')

    def delete(url, headers=None, params=None, timeout=None, json=None, **kw):
        DELETES.append((url, dict(params or {}), json))
        return _Resp({})

    def fetch(*a, **k):
        if torrents is None:
            raise Exception('deluge down')
        return torrents

    for name, fn in (('get', get), ('delete', delete)):
        p = mock.patch.object(aw.requests, name, fn)
        p.start()
        _PATCHES.append(p)
    ns = aw.cleanup_radarr_queue_dupes.__globals__
    p = mock.patch.dict(ns, {
        'deluge_login': lambda: None, 'get_all_torrents': fetch,
        'set_torrent_label': lambda h, label: None,
        'record_activity': lambda *a, **k: None, 'log_removal': lambda *a, **k: None,
        '_promote_keeper': lambda *a, **k: None,
        'RADARR_URL': 'http://radarr', 'RADARR_API_KEY': 'k', 'RADARR_UPG_LABEL': 'radarr-upgrade',
        'SONARR_URL': 'http://sonarr', 'SONARR_API_KEY': 'k', 'SONARR_UPG_LABEL': 'sonarr-upgrade'})
    p.start()
    _PATCHES.append(p)


QUEUE = []


def _radarr_queue():
    QUEUE[:] = [
        {'id': 7, 'movieId': 9, 'downloadId': 'CCC', 'title': 'Movie.Good', 'customFormatScore': 100,
         'size': 100, 'sizeleft': 100},
        {'id': 8, 'movieId': 9, 'downloadId': 'DDD', 'title': 'Movie.Worse', 'customFormatScore': 20,
         'size': 100, 'sizeleft': 100},
    ]


def _sonarr_queue():
    QUEUE[:] = [
        {'id': 10, 'seriesId': 7, 'episodeId': 101, 'downloadId': 'EEE', 'title': 'Show.S01E01.Good',
         'customFormatScore': 100, 'size': 100, 'sizeleft': 100},
        {'id': 11, 'seriesId': 7, 'episodeId': 101, 'downloadId': 'FFF', 'title': 'Show.S01E01.Worse',
         'customFormatScore': 20, 'size': 100, 'sizeleft': 100},
    ]


def test_radarr_unknown_deluge_never_removes_from_client():
    _radarr_queue()
    _setup(None)
    n = aw.cleanup_radarr_queue_dupes()
    assert DELETES == [], f'deletes: {DELETES!r}'
    assert n == 0, f'removed count: {n!r}'


def test_sonarr_unknown_deluge_never_removes_from_client():
    _sonarr_queue()
    _setup(None)
    n = aw.cleanup_sonarr_queue_dupes()
    assert DELETES == [], f'deletes: {DELETES!r}'
    assert not n, f'removed count: {n!r}'


def test_radarr_reachable_deluge_still_removes_from_client():
    _radarr_queue()
    _setup({})
    n = aw.cleanup_radarr_queue_dupes()
    assert len(DELETES) == 1, f'deletes: {DELETES!r}'
    assert DELETES[0][0].endswith('/api/v3/queue/8'), DELETES
    assert DELETES[0][1].get('removeFromClient') is True, DELETES
    assert n == 1, f'removed count: {n!r}'


def test_sonarr_reachable_deluge_still_removes_from_client():
    _sonarr_queue()
    _setup({})
    n = aw.cleanup_sonarr_queue_dupes()
    assert len(DELETES) == 1, f'deletes: {DELETES!r}'
    assert DELETES[0][1].get('removeFromClient') is True, DELETES
    assert DELETES[0][2] == {'ids': [11]}, DELETES
    assert n == 1, f'removed count: {n!r}'


TESTS = [test_radarr_unknown_deluge_never_removes_from_client,
         test_sonarr_unknown_deluge_never_removes_from_client,
         test_radarr_reachable_deluge_still_removes_from_client,
         test_sonarr_reachable_deluge_still_removes_from_client]

if __name__ == '__main__':
    order = TESTS[::-1] if os.environ.get('TEST_ORDER') == 'reverse' else TESTS
    failed = 0
    for t in order:
        try:
            t()
            print(f'RESULT {t.__name__} PASS')
        except Exception as e:
            failed += 1
            print(f'RESULT {t.__name__} FAIL {type(e).__name__}: {e}')
    sys.exit(1 if failed else 0)
