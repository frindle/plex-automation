"""Self-checks for grab-time deduping -- dedupe_grabbed_release(), wired into
handle_grab().

The scheduled passes (cleanup_radarr_queue_dupes / cleanup_sonarr_queue_dupes,
covered by test_queue_dupes.py and test_sonarr_queue_dupes.py) already find a
duplicate of a throttled upgrade. But they run daily and weekly, so the second
copy of a film could burn bandwidth for a day before anything noticed. These
checks are about doing it AT GRAB TIME: handle_grab moves the throttled grab
into the -upgrade lane, and the same pass is then run immediately, scoped to
just that movie or series.

Two things make it non-trivial and both are covered here:

  * ORDER. The grab is only observable to the pass once it is in the lane
    (that is where the pass looks), so the call sits after the relabel, not
    before it. Case 8 checks a grab that is never throttled never triggers it.
  * THE HISTORY RACE. The *arr can fire the Grab webhook before its own
    'grabbed' record is queryable by downloadId, and without that record the
    torrent has no identity. We poll for it rather than sleeping a guessed
    interval (case 5), and if it never turns up we do nothing at all (case 4).

Sonarr keeps its episode-SET identity throughout: the scope is the series, but
elimination is still subset-only (cases 9-11).

No framework, no live Radarr/Sonarr/Deluge. Run: python test_grab_dedupe.py
"""

GB = 1024 ** 3


class _Resp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self._ok:
            raise Exception('HTTP error')


class _Stub:
    """Fake Radarr + Sonarr + Deluge, shared by every case.

    torrents -- Deluge get_all_torrents() result, keyed by lowercase hash.
                set_torrent_label mutates it, so the world the dedupe pass
                reads is the world handle_grab just wrote.
    history  -- lowercase hash -> list of records, or a callable taking the
                hash (used to make the grab record show up late).
    """

    def __init__(self, aw, torrents=None, history=None, radarr_queue=(),
                 sonarr_queue=(), has_file=True):
        self.aw = aw
        self.torrents = torrents if torrents is not None else {}
        self.history = history if history is not None else {}
        self.radarr_queue = list(radarr_queue)
        self.sonarr_queue = list(sonarr_queue)
        self.has_file = has_file
        self.deleted = []        # Radarr queue ids / Sonarr id batches
        self.labeled = []        # (hash, label) writes
        self.history_calls = []  # every /api/v3/history lookup
        self.sleeps = []
        self.posts = []

        aw.requests = type('R', (), {'get': staticmethod(self.get),
                                     'delete': staticmethod(self.delete)})()
        aw.session = type('S', (), {'post': staticmethod(self.post)})()
        aw.time = type('T', (), {'sleep': staticmethod(self.sleeps.append)})()
        aw.deluge_login = lambda: None
        aw.get_all_torrents = lambda: dict(self.torrents)
        aw.set_torrent_label = self.set_torrent_label
        aw.ensure_label_exists_named = lambda label: None
        aw.record_activity = lambda *a, **k: None

    # ── Deluge ──────────────────────────────────────────────────────────
    def set_torrent_label(self, h, label):
        self.labeled.append((h, label))
        if h in self.torrents:
            self.torrents[h]['label'] = label

    def post(self, url, json=None, timeout=None, **kw):
        method = (json or {}).get('method')
        self.posts.append(method)
        if method == 'core.get_torrent_status':
            h = json['params'][0]
            t = self.torrents.get(h, {})
            return _Resp({'result': {'label': t.get('label'),
                                     'progress': t.get('progress', 0.0),
                                     'save_path': t.get('save_path', '/data/Downloads/x')}})
        return _Resp({'result': None})

    # ── Radarr / Sonarr HTTP ────────────────────────────────────────────
    def _lookup_history(self, dl):
        if callable(self.history):
            return self.history(dl)
        return self.history.get(dl, [])

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        if url.endswith('/api/v3/history'):
            dl = (params or {}).get('downloadId', '').lower()
            self.history_calls.append(dl)
            return _Resp({'records': self._lookup_history(dl)})
        if url.endswith('/api/v3/queue'):
            q = self.radarr_queue if url.startswith(self.aw.RADARR_URL) else self.sonarr_queue
            return _Resp({'records': q})
        if '/api/v3/movie/' in url:
            return _Resp({'hasFile': self.has_file, 'year': 2022})
        if '/api/v3/episode/' in url:
            return _Resp({'hasFile': self.has_file})
        raise AssertionError(f'unexpected GET {url}')

    def delete(self, url, headers=None, params=None, timeout=None, json=None, **kw):
        assert params.get('blocklist') is False, params
        assert params.get('removeFromClient') is True, params
        if url.endswith('/api/v3/queue/bulk'):
            self.deleted.append(sorted(json['ids']))
        else:
            self.deleted.append(int(url.rsplit('/', 1)[1]))
        return _Resp({})


# ── payload / fixture helpers ───────────────────────────────────────────
def _lane(name, label='radarr-upgrade', progress=0.0):
    return {'name': name, 'label': label, 'state': 'Queued', 'progress': progress,
            'save_path': '/data/Downloads/x'}


def _rgrab(movie_id, score, date='2026-08-20T00:00:00Z'):
    return {'eventType': 'grabbed', 'date': date, 'movieId': movie_id,
            'data': {'customFormatScore': score}}


def _sgrab(series_id, episode_id, score, date='2026-08-20T00:00:00Z'):
    return {'eventType': 'grabbed', 'date': date, 'seriesId': series_id,
            'episodeId': episode_id, 'data': {'customFormatScore': score}}


def _radarr_payload(download_id, movie_id=42, size_gb=20):
    return {'eventType': 'Grab', 'downloadId': download_id,
            'movie': {'id': movie_id}, 'release': {'size': int(size_gb * GB)}}


def _sonarr_payload(download_id, episode_id, size_gb=20):
    return {'eventType': 'Grab', 'downloadId': download_id,
            'episodes': [{'id': episode_id}], 'release': {'size': int(size_gb * GB)}}


def _rqrec(qid, movie_id, download_id, title, score, size=100, sizeleft=100):
    return {'id': qid, 'movieId': movie_id, 'downloadId': download_id, 'title': title,
            'customFormatScore': score, 'size': size, 'sizeleft': sizeleft}


def run():
    aw = __import__('arr-webhook')
    aw.RADARR_URL, aw.RADARR_API_KEY = 'http://radarr', 'testkey'
    aw.SONARR_URL, aw.SONARR_API_KEY = 'http://sonarr', 'testkey'
    aw.RADARR_UPG_LABEL, aw.SONARR_UPG_LABEL = 'radarr-upgrade', 'sonarr-upgrade'
    real = (aw.requests, aw.session, aw.time, aw.deluge_login, aw.get_all_torrents,
            aw.set_torrent_label, aw.ensure_label_exists_named, aw.record_activity)

    try:
        # ── 1. new grab is the WORSE release: it is the one superseded ────
        # 'aaa' is already in the lane at score 100. Radarr, blind to it
        # because the relabel took it out of its own queue, grabs 'bbb' at
        # score 50. handle_grab throttles 'bbb' and the scoped pass drops it
        # on the spot -- before it has pulled a byte.
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.2160p.WEB-DL-OTHER.mkv', label='radarr')},
                  history={'aaa': [_rgrab(42, 100)], 'bbb': [_rgrab(42, 50)]})
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'radarr-upgrade'), ('bbb', aw.SUPERSEDED_LABEL)], s.labeled

        # ── 2. new grab is the BETTER release: the old one goes instead ───
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.1080p-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv', label='radarr')},
                  history={'aaa': [_rgrab(42, 50)], 'bbb': [_rgrab(42, 100)]})
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'radarr-upgrade'), ('aaa', aw.SUPERSEDED_LABEL)], s.labeled
        assert s.torrents['bbb']['label'] == 'radarr-upgrade', s.torrents['bbb']

        # ── 3. no duplicate: the grab is throttled and nothing else moves ─
        s = _Stub(aw,
                  torrents={'aaa': _lane('Other.Film.2019.mkv'),
                            'bbb': _lane('Father.Stu.2022.2160p-RUDR.mkv', label='radarr')},
                  history={'aaa': [_rgrab(7, 100)], 'bbb': [_rgrab(42, 50)]})
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'radarr-upgrade')], s.labeled

        # ── 4. identity never resolves: nothing destructive, and bounded ──
        # Radarr has no grab record for this hash. Without one the torrent
        # has no movieId, so there is no way to know what it duplicates --
        # the pass must not guess. It also must not poll forever.
        tries = getattr(aw, 'GRAB_DEDUPE_TRIES', 6)
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.2160p.WEB-DL-OTHER.mkv', label='radarr')},
                  history={'aaa': [_rgrab(42, 100)]})
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'radarr-upgrade')], s.labeled
        assert s.history_calls == ['bbb'] * tries, s.history_calls
        # one 3s settle sleep in handle_grab + one wait between each retry
        assert s.sleeps == [3] + [aw.GRAB_DEDUPE_DELAY] * (tries - 1), s.sleeps

        # ── 5. identity arrives LATE: polling catches it, a sleep would not ─
        # The Grab webhook can beat Radarr's own history write. The first two
        # lookups come back empty and the third has the record.
        seen = []

        def late_history(dl):
            if dl == 'aaa':
                return [_rgrab(42, 100)]
            seen.append(dl)
            return [_rgrab(42, 50)] if len(seen) > 2 else []

        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.2160p.WEB-DL-OTHER.mkv', label='radarr')},
                  history=late_history)
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.labeled == [('bbb', 'radarr-upgrade'), ('bbb', aw.SUPERSEDED_LABEL)], s.labeled

        # ── 6. loser still visible in the queue goes through the queue API ─
        # The *arr refreshes its queue on a timer, so right after the relabel
        # the new grab can still have a stale queue record. It must then be
        # removed properly (blocklist False, removeFromClient True) and NOT
        # counted twice -- exactly one action for it.
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.WEB-DL-OTHER.mkv', label='radarr')},
                  history={'aaa': [_rgrab(42, 100)], 'bbb': [_rgrab(42, 50)]},
                  radarr_queue=[_rqrec(31, 42, 'BBB', 'Father.Stu.2022.WEB-DL-OTHER.mkv', 50)])
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.deleted == [31], s.deleted
        assert s.labeled == [('bbb', 'radarr-upgrade')], s.labeled

        # ── 7. the scoped pass touches ONLY the movie just grabbed ────────
        # Movie 7 has its own pile-up in the lane. A grab for movie 42 is not
        # licence to go sweeping; that is the scheduled pass's job.
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.WEB-DL-OTHER.mkv', label='radarr'),
                            'ccc': _lane('Other.Film.2019.A.mkv'),
                            'ddd': _lane('Other.Film.2019.B.mkv')},
                  history={'aaa': [_rgrab(42, 100)], 'bbb': [_rgrab(42, 50)],
                           'ccc': [_rgrab(7, 100)], 'ddd': [_rgrab(7, 10)]})
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.labeled == [('bbb', 'radarr-upgrade'), ('bbb', aw.SUPERSEDED_LABEL)], s.labeled

        # ── 8. a grab that is never throttled never triggers the pass ─────
        # Under the 10GB threshold, handle_grab returns before it touches
        # Deluge at all -- so nothing is relabeled and no history is read.
        s = _Stub(aw,
                  torrents={'aaa': _lane('Father.Stu.2022.2160p.HDR-RUDR.mkv'),
                            'bbb': _lane('Father.Stu.2022.WEB-DL-OTHER.mkv', label='radarr')},
                  history={'aaa': [_rgrab(42, 100)], 'bbb': [_rgrab(42, 50)]})
        aw.handle_grab(_radarr_payload('BBB', size_gb=4), 'Radarr')
        assert s.labeled == [], s.labeled
        assert s.history_calls == [], s.history_calls
        # ...and neither does a grab that is not an upgrade at all.
        s = _Stub(aw,
                  torrents={'bbb': _lane('Father.Stu.2022.WEB-DL-OTHER.mkv', label='radarr')},
                  history={}, has_file=False)
        aw.handle_grab(_radarr_payload('BBB'), 'Radarr')
        assert s.labeled == [], s.labeled

        # ── 9. Sonarr: new single episode lands inside a running season pack ─
        # Scope is the series, but elimination is still subset-only. The new
        # grab is one episode of a pack already in flight, so it is redundant
        # and goes -- even though it scores higher, because dropping the pack
        # would leave nine episodes with nothing downloading.
        s = _Stub(aw,
                  torrents={'aaa': _lane('The.Show.S01.2160p-RUDR', label='sonarr-upgrade'),
                            'bbb': _lane('The.Show.S01E05.2160p-RUDR.mkv', label='sonarr')},
                  history={'aaa': [_sgrab(7, ep, 50) for ep in range(101, 111)],
                           'bbb': [_sgrab(7, 105, 300)]})
        aw.handle_grab(_sonarr_payload('BBB', 105), 'Sonarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'sonarr-upgrade'), ('bbb', aw.SUPERSEDED_LABEL)], s.labeled

        # ── 10. Sonarr: new season pack swallows a running single episode ──
        s = _Stub(aw,
                  torrents={'aaa': _lane('The.Show.S01E05.2160p-RUDR.mkv', label='sonarr-upgrade'),
                            'bbb': _lane('The.Show.S01.2160p-RUDR', label='sonarr')},
                  history={'aaa': [_sgrab(7, 105, 300)],
                           'bbb': [_sgrab(7, ep, 50) for ep in range(101, 111)]})
        aw.handle_grab(_sonarr_payload('BBB', 105), 'Sonarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'sonarr-upgrade'), ('aaa', aw.SUPERSEDED_LABEL)], s.labeled

        # ── 11. Sonarr: a different episode of the same series survives ────
        # The scope is seriesId, so both releases are compared -- and the
        # episode-set rule is what stops the scoped pass deleting real
        # content the way naive seriesId grouping would.
        s = _Stub(aw,
                  torrents={'aaa': _lane('The.Show.S02E01-RUDR.mkv', label='sonarr-upgrade'),
                            'bbb': _lane('The.Show.S02E02-RUDR.mkv', label='sonarr')},
                  history={'aaa': [_sgrab(7, 201, 900)], 'bbb': [_sgrab(7, 202, 100)]})
        aw.handle_grab(_sonarr_payload('BBB', 202), 'Sonarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'sonarr-upgrade')], s.labeled

        # ── 12. a finished duplicate is never relabeled (hit-and-run) ──────
        # 'aaa' has the same episode and is at 100% -- it may be seeding, and
        # feeding it to cleanup_superseded would risk the tracker. It is
        # skipped, so the new grab has nothing to be redundant against.
        s = _Stub(aw,
                  torrents={'aaa': dict(_lane('The.Show.S05E01.PROPER-RUDR.mkv',
                                              label='sonarr-upgrade', progress=100.0),
                                        state='Seeding'),
                            'bbb': _lane('The.Show.S05E01.WEB-DL-RUDR.mkv', label='sonarr')},
                  history={'aaa': [_sgrab(7, 501, 900)], 'bbb': [_sgrab(7, 501, 100)]})
        aw.handle_grab(_sonarr_payload('BBB', 501), 'Sonarr')
        assert s.deleted == [], s.deleted
        assert s.labeled == [('bbb', 'sonarr-upgrade')], s.labeled

        # ── 13. the scheduled passes still work unscoped ───────────────────
        # The backstop must not have been narrowed: called with no scope the
        # pass still sweeps every movie, which is what covers a grab whose
        # webhook was missed or that happened while the service was down.
        s = _Stub(aw,
                  torrents={'aaa': _lane('A.2022.Good.mkv'), 'bbb': _lane('A.2022.Bad.mkv'),
                            'ccc': _lane('B.2019.Good.mkv'), 'ddd': _lane('B.2019.Bad.mkv')},
                  history={'aaa': [_rgrab(42, 100)], 'bbb': [_rgrab(42, 50)],
                           'ccc': [_rgrab(7, 100)], 'ddd': [_rgrab(7, 10)]})
        assert aw.cleanup_radarr_queue_dupes() == 2
        assert sorted(s.labeled) == [('bbb', aw.SUPERSEDED_LABEL),
                                     ('ddd', aw.SUPERSEDED_LABEL)], s.labeled
    finally:
        (aw.requests, aw.session, aw.time, aw.deluge_login, aw.get_all_torrents,
         aw.set_torrent_label, aw.ensure_label_exists_named, aw.record_activity) = real

    print('test_grab_dedupe: all assertions passed')


if __name__ == '__main__':
    run()
