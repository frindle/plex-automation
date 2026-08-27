"""Self-checks for cleanup_sonarr_queue_dupes -- the pass that is supposed to
stop the same episodes being downloaded twice.

Same blind spot as the Radarr pass (see test_queue_dupes.py): Sonarr's Deluge
client only reports torrents carrying its category label, so as soon as this
service relabels a throttled upgrade to SONARR_UPG_LABEL the torrent stops
appearing in /api/v3/queue. Sonarr had no dedupe pass of its own at all, so
that population was never policed from either side.

What is different here is IDENTITY. A movie is one movieId, but a TV grab
covers a SET of episodes -- a single episode, a multi-episode file, or a whole
season pack -- so seriesId is not an identity: two different episodes of one
series are not duplicates (case 2). Duplicates are grabs whose episode sets
overlap, and a candidate is only dropped when its set is a SUBSET of a
keeper's, so the keeper delivers everything the loser would (cases 1, 3) and
packs that merely straddle each other are both kept (case 4).

No framework, no live Sonarr/Deluge. Run: python test_sonarr_queue_dupes.py
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
    """Fake Sonarr + Deluge.

    queue_records -- what /api/v3/queue returns (one record PER EPISODE,
                     the way Sonarr actually reports a season pack)
    torrents      -- Deluge get_all_torrents() result
    history       -- hash (lowercase) -> list of history records

    Returns (deleted_queue_id_batches, label_writes).
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

    def delete(url, headers=None, params=None, timeout=None, json=None, **kw):
        assert url.endswith('/api/v3/queue/bulk'), url
        assert params.get('blocklist') is False, params
        deleted.append(sorted(json['ids']))
        return _Resp({})

    aw.requests = type('R', (), {'get': staticmethod(get), 'delete': staticmethod(delete)})()
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: torrents
    aw.set_torrent_label = lambda h, label: labeled.append((h, label))
    aw.record_activity = lambda *a, **k: None
    return deleted, labeled


def _grab(series_id, episode_id, score, date='2026-08-20T00:00:00Z'):
    """One 'grabbed' history record. Sonarr writes one of these per episode
    a release covers, all sharing the same downloadId."""
    return {'eventType': 'grabbed', 'date': date, 'seriesId': series_id,
            'episodeId': episode_id, 'data': {'customFormatScore': score}}


def _queued(name):
    return {'name': name, 'label': 'sonarr-upgrade', 'state': 'Queued', 'progress': 0.0}


def _qrec(qid, series_id, episode_id, download_id, title, score, size=100, sizeleft=100):
    return {'id': qid, 'seriesId': series_id, 'episodeId': episode_id,
            'downloadId': download_id, 'title': title, 'customFormatScore': score,
            'size': size, 'sizeleft': sizeleft}


def run():
    aw = __import__('arr-webhook')
    aw.SONARR_URL = 'http://sonarr'
    aw.SONARR_API_KEY = 'testkey'
    aw.SONARR_UPG_LABEL = 'sonarr-upgrade'
    real_requests = aw.requests

    try:
        # ── 1. the blind spot: season pack vs single episode ─────────────
        # Both grabbed, both relabeled into the throttled lane, both at 0%.
        # Sonarr's queue shows NOTHING because neither torrent carries the
        # 'sonarr' category any more, so this can only be found through
        # Deluge + grab history. The single episode is inside the pack, so
        # it is the loser -- even though it scores HIGHER. Dropping the pack
        # to keep one better episode would leave nine episodes with nothing
        # in flight; keeping the pack costs at most a later per-episode
        # upgrade, which Sonarr does on its own.
        torrents = {
            'AAA': _queued('The.Show.S01.2160p.WEB-DL.DDP5.1.H.265-RUDR'),
            'BBB': _queued('The.Show.S01E05.2160p.WEB-DL.DDP5.1.HDR.H.265-RUDR.mkv'),
        }
        history = {
            'aaa': [_grab(7, ep, 50) for ep in range(101, 111)],
            'bbb': [_grab(7, 105, 300)],
        }
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [('bbb', aw.SUPERSEDED_LABEL)], labeled

        # ── 2. two different episodes of one series are NOT duplicates ────
        # The whole reason seriesId cannot be the identity. A naive port of
        # the Radarr pass (group by seriesId) throws one of these away.
        torrents = {
            'CCC': _queued('The.Show.S02E01.2160p-RUDR.mkv'),
            'DDD': _queued('The.Show.S02E02.2160p-RUDR.mkv'),
        }
        history = {'ccc': [_grab(7, 201, 100)], 'ddd': [_grab(7, 202, 900)]}
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 3. queue-visible dupes, and per-episode record folding ────────
        # Sonarr reports a season pack as one queue record PER EPISODE, all
        # sharing a downloadId. Those must fold into ONE 3-episode candidate
        # (otherwise the pack looks like three single-episode grabs) and be
        # removed as one batch. Here the pack is the keeper and the
        # higher-scoring single episode inside it is removed.
        queue = [
            _qrec(10, 7, 301, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
            _qrec(11, 7, 302, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
            _qrec(12, 7, 303, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
            _qrec(13, 7, 302, 'FFF', 'The.Show.S03E02.WEB-DL-RUDR.mkv', 500),
        ]
        deleted, labeled = _stub(aw, queue, {}, {})
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [[13]], deleted
        assert labeled == [], labeled

        # ── 4. straddling multi-episode files are both kept ───────────────
        # E01-E05 and E04-E10 overlap, but neither delivers everything the
        # other does, so dropping either loses episodes.
        torrents = {
            'GGG': _queued('The.Show.S04E01-E05.WEB-DL-RUDR.mkv'),
            'HHH': _queued('The.Show.S04E04-E10.WEB-DL-RUDR.mkv'),
        }
        history = {
            'ggg': [_grab(7, ep, 100) for ep in (401, 402, 403, 404, 405)],
            'hhh': [_grab(7, ep, 100) for ep in (404, 405, 406, 407, 408, 409, 410)],
        }
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 5. a finished torrent is never relabeled ──────────────────────
        # It may be seeding, and pulling its label to 'superseded' feeds it
        # to cleanup_superseded -- a hit-and-run. It is skipped entirely, so
        # the in-flight duplicate of the same episode is left alone too.
        torrents = {
            'III': dict(_queued('The.Show.S05E01.PROPER-RUDR.mkv'), progress=100.0, state='Seeding'),
            'JJJ': _queued('The.Show.S05E01.WEB-DL-RUDR.mkv'),
        }
        history = {'iii': [_grab(7, 501, 100)], 'jjj': [_grab(7, 501, 900)]}
        deleted, labeled = _stub(aw, [], torrents, history)
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 6. a torrent with no grab history is left alone ───────────────
        torrents = {'KKK': _queued('Manual.Add.S06E01.mkv'), 'LLL': _queued('Manual.Add.S06E01.Alt.mkv')}
        deleted, labeled = _stub(aw, [], torrents, {'kkk': [_grab(7, 601, 10)]})
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [], deleted
        assert labeled == [], labeled

        # ── 7. one failed removal must not abort the rest of the pass ─────
        queue = [
            _qrec(1, 21, 701, 'M1', 'A.Keep', 100),
            _qrec(2, 21, 701, 'M2', 'A.Drop', 10),
            _qrec(3, 22, 801, 'M3', 'B.Keep', 100),
            _qrec(4, 22, 801, 'M4', 'B.Drop', 10),
        ]
        deleted, labeled = _stub(aw, queue, {}, {})
        good_delete = aw.requests.delete

        def flaky_delete(url, **kw):
            if kw.get('json', {}).get('ids') == [2]:
                raise Exception('queue record vanished')
            return good_delete(url, **kw)

        aw.requests = type('R', (), {'get': staticmethod(aw.requests.get),
                                     'delete': staticmethod(flaky_delete)})()
        aw.cleanup_sonarr_queue_dupes()
        assert deleted == [[4]], deleted

        # ── 8. grab identity: union of episodes, score from latest grab ───
        # A season pack's identity is the union of its per-episode grab
        # records; non-grab events (imports) contribute nothing.
        _stub(aw, [], {}, {'zzz': [_grab(7, 901, 10, '2026-01-01T00:00:00Z'),
                                   _grab(7, 902, 10, '2026-01-01T00:00:00Z'),
                                   _grab(7, 903, 55, '2026-08-01T00:00:00Z'),
                                   {'eventType': 'downloadFolderImported',
                                    'date': '2026-08-02T00:00:00Z',
                                    'seriesId': 99, 'episodeId': 999}]})
        assert aw._sonarr_grab_identity('ZZZ') == (7, frozenset({901, 902, 903}), 55)
        assert aw._sonarr_grab_identity('missing') == (None, frozenset(), 0)

        # ── 9. no API key: pass is a no-op, nothing is fetched or touched ─
        deleted, labeled = _stub(aw, [], {'AAA': _queued('x')}, {})
        aw.SONARR_API_KEY = ''
        aw.cleanup_sonarr_queue_dupes()
        aw.SONARR_API_KEY = 'testkey'
        assert deleted == [], deleted
        assert labeled == [], labeled
    finally:
        aw.requests = real_requests

    print('test_sonarr_queue_dupes: all assertions passed')


if __name__ == '__main__':
    run()
