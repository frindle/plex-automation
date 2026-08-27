"""Self-checks for cleanup_radarr_queue_dupes -- the pass that is supposed to
stop the same movie being downloaded twice.

The case that matters is the one the old code could not see at all. Radarr's
Deluge client only reports torrents carrying its category label, so as soon as
this service relabels a throttled upgrade to RADARR_UPG_LABEL the torrent stops
appearing in /api/v3/queue. Grouping queue records alone therefore found one
entry per movie and removed nothing, while Deluge sat on two 0%-complete
releases of the same film. Case 1 reproduces that exactly; it removes nothing
against the queue-only logic.

No framework, no live Radarr/Deluge. Run: python test_queue_dupes.py
"""


class _Resp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self._ok:
            raise Exception('HTTP error')


def _stub(aw, queue_records, torrents, history):
    """Fake Radarr + Deluge.

    queue_records -- what /api/v3/queue returns
    torrents      -- Deluge get_all_torrents() result
    history       -- hash (lowercase) -> list of history records

    Returns (deleted_queue_ids, label_writes).
    """
    deleted = []
    labeled = []

    def get(url, headers=None, params=None, timeout=None, **kw):
        if url.endswith('/api/v3/queue'):
            return _Resp({'records': queue_records})
        if url.endswith('/api/v3/history'):
            dl = (params or {}).get('downloadId', '')
            return _Resp({'records': history.get(dl.lower(), [])})
        raise AssertionError(f'unexpected GET {url}')

    def delete(url, headers=None, params=None, timeout=None, **kw):
        deleted.append(int(url.rsplit('/', 1)[1]))
        return _Resp({})

    aw.requests = type('R', (), {'get': staticmethod(get), 'delete': staticmethod(delete)})()
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: torrents
    aw.set_torrent_label = lambda h, label: labeled.append((h, label))
    aw.record_activity = lambda *a, **k: None
    return deleted, labeled


def _grab(movie_id, score, date='2026-08-20T00:00:00Z'):
    return {'eventType': 'grabbed', 'date': date, 'movieId': movie_id,
            'data': {'customFormatScore': score}}


def _queued(name):
    return {'name': name, 'label': 'radarr-upgrade', 'state': 'Queued', 'progress': 0.0}


def run():
    aw = __import__('arr-webhook')
    aw.RADARR_URL = 'http://radarr'
    aw.RADARR_API_KEY = 'testkey'
    aw.RADARR_UPG_LABEL = 'radarr-upgrade'
    real_requests = aw.requests

    try:
        # ── 1. the reported bug ──────────────────────────────────────────
        # Two releases of Father Stu, both grabbed, both relabeled into the
        # throttled lane, both sitting at 0%. Radarr's queue shows NOTHING
        # because neither torrent carries the 'radarr' category any more.
        # Queue-only grouping sees zero candidates and removes nothing; the
        # duplicate has to be found through Deluge + grab history.
        torrents = {
            'AAA': _queued('Father.Stu.2022.2160p.MA.WEB-DL.DDP5.1.HDR.H.265-RUDR.mkv'),
            'BBB': _queued('Father.Stu.2022.2160p.MA.WEB-DL.DDP5.1.HDR.H.265-RUDR.mkv'),
        }
        history = {'aaa': [_grab(42, 100)], 'bbb': [_grab(42, 50)]}
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [('bbb', aw.SUPERSEDED_LABEL)], labeled

        # ── 2. queue-visible duplicates still work as before ─────────────
        queue = [
            {'id': 7, 'movieId': 9, 'downloadId': 'CCC', 'title': 'Movie.Good', 'customFormatScore': 100,
             'size': 100, 'sizeleft': 100},
            {'id': 8, 'movieId': 9, 'downloadId': 'DDD', 'title': 'Movie.Worse', 'customFormatScore': 20,
             'size': 100, 'sizeleft': 100},
        ]
        deleted, labeled = _stub(aw, queue, {}, {})
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [8], deleted
        assert labeled == [], labeled

        # ── 3. mixed: one visible, one throttled, equal score ────────────
        # Score ties break on bytes already pulled -- discarding the entry
        # that is 40% done would throw away real bandwidth.
        queue = [{'id': 11, 'movieId': 77, 'downloadId': 'EEE', 'title': 'JL.Atmos',
                  'customFormatScore': 100, 'size': 100, 'sizeleft': 60}]
        torrents = {'FFF': _queued('Zack.Snyders.Justice.League.2021.2160p.DV.HDR-RUDR.mkv')}
        deleted, labeled = _stub(aw, queue, torrents, {'fff': [_grab(77, 100)]})
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [('fff', aw.SUPERSEDED_LABEL)], labeled

        # ── 4. nothing to do / nothing unsafe to touch ───────────────────
        # One throttled torrent per movie is not a duplicate; a different
        # movie in the lane is not a duplicate either; and a finished
        # torrent is never relabeled (it may be seeding -- hit-and-run).
        torrents = {
            'GGG': _queued('Lone.Upgrade.2020.mkv'),
            'HHH': _queued('Other.Film.2019.mkv'),
            'III': dict(_queued('Finished.Dupe.2020.mkv'), progress=100.0, state='Seeding'),
            'JJJ': _queued('Finished.Dupe.2020.Other.Release.mkv'),
        }
        history = {'ggg': [_grab(1, 10)], 'hhh': [_grab(2, 10)],
                   'iii': [_grab(3, 10)], 'jjj': [_grab(3, 10)]}
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 5. a torrent with no grab history is left alone ──────────────
        torrents = {'KKK': _queued('Manual.Add.2020.mkv'), 'LLL': _queued('Manual.Add.2020.Alt.mkv')}
        deleted, labeled = _stub(aw, [], torrents, {'kkk': [_grab(5, 10)]})
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 6. one failed removal must not abort the rest of the pass ────
        queue = [
            {'id': 1, 'movieId': 4, 'downloadId': 'M1', 'title': 'A.Keep', 'customFormatScore': 100,
             'size': 100, 'sizeleft': 100},
            {'id': 2, 'movieId': 4, 'downloadId': 'M2', 'title': 'A.Drop', 'customFormatScore': 10,
             'size': 100, 'sizeleft': 100},
            {'id': 3, 'movieId': 5, 'downloadId': 'M3', 'title': 'B.Keep', 'customFormatScore': 100,
             'size': 100, 'sizeleft': 100},
            {'id': 4, 'movieId': 5, 'downloadId': 'M4', 'title': 'B.Drop', 'customFormatScore': 10,
             'size': 100, 'sizeleft': 100},
        ]
        deleted, labeled = _stub(aw, queue, {}, {})
        good_delete = aw.requests.delete

        def flaky_delete(url, **kw):
            if url.endswith('/2'):
                raise Exception('queue record vanished')
            return good_delete(url, **kw)

        aw.requests = type('R', (), {'get': staticmethod(aw.requests.get),
                                     'delete': staticmethod(flaky_delete)})()
        aw.cleanup_radarr_queue_dupes()
        assert deleted == [4], deleted

        # ── 7. grab identity uses the most recent grab ───────────────────
        _stub(aw, [], {}, {'zzz': [_grab(1, 10, '2026-01-01T00:00:00Z'),
                                   _grab(2, 55, '2026-08-01T00:00:00Z'),
                                   {'eventType': 'downloadFolderImported', 'date': '2026-08-02T00:00:00Z',
                                    'movieId': 99}]})
        assert aw._radarr_grab_identity('ZZZ') == (2, 55)
        assert aw._radarr_grab_identity('missing') == (None, 0)
    finally:
        aw.requests = real_requests

    print('test_queue_dupes: all assertions passed')


if __name__ == '__main__':
    run()
