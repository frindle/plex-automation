"""Self-checks for the "superseded but still downloading" fix and the
pending-import guard (Spider-Man: Brand New Day, 2026-10).

Bug 1: a duplicate loser still DOWNLOADING was relabeled 'superseded' but kept
its 30 KB/s lane cap forever. Policy: never remove it; keep the label, lift the
cap (resume if paused), let it finish and seed; cleanup_superseded reaps it. Bug 2: the keeper (bottom of the queue) starved while
the losers held the download slots. Bug 3: dedup_via_radarr superseded a
FINISHED upgrade that Radarr had not imported yet, hiding it from Radarr and
auto-rescue forever.

No framework, no live services. Run: python test_supersede_inflight.py
"""
import datetime


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def _install(aw, status, boom=False):
    """status: hash -> {'state','progress','total_done'}. Records RPC + side effects."""
    rec = {'rpc': [], 'label': [], 'move': [], 'activity': []}

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            if boom:
                raise RuntimeError('deluge down')
            m, params = json['method'], json['params']
            rec['rpc'].append((m, params))
            if m == 'core.get_torrents_status':
                ids = params[0].get('id') or []
                return _Resp({'result': {h: status[h] for h in ids if h in status}})
            return _Resp({'result': True})

    aw.session = S()
    aw.deluge_login = lambda: None
    aw.set_torrent_label = lambda h, label: rec['label'].append((h, label))
    aw.move_torrent_storage = lambda h, d: rec['move'].append((h, d))
    aw.record_activity = lambda cat, summary: rec['activity'].append(cat)
    return rec


def _removed(rec):
    return [p for m, p in rec['rpc'] if m == 'core.remove_torrent']


def main():
    aw = __import__('arr-webhook')
    SUP = aw.SUPERSEDED_LABEL

    def opts(rec):
        return [p for m, p in rec['rpc'] if m == 'core.set_torrent_options']

    def resumed(rec):
        return [p for m, p in rec['rpc'] if m == 'core.resume_torrent']

    # Incomplete torrent: NEVER removed; keeps `superseded` label; cap cleared;
    # not moved mid-download.
    for st, pr in (('Downloading', 0.11), ('Queued', 12.5), ('Paused', 40.0)):
        rec = _install(aw, {'H': {'state': st, 'progress': pr, 'total_done': 5}})
        aw.supersede_torrent('H')
        assert _removed(rec) == [], (st, 'must never remove an incomplete torrent', rec['rpc'])
        assert ('H', SUP) in rec['label'], st
        assert opts(rec) == [[['H'], {'max_download_speed': -1}]], (st, rec['rpc'])
        assert rec['move'] == [], 'no mid-download move'
        assert (resumed(rec) == [[['H']]]) == (st == 'Paused'), (st, rec['rpc'])

    # Queued at exactly 0%: still labeled (existing reaper path), nothing removed here
    rec = _install(aw, {'H': {'state': 'Queued', 'progress': 0, 'total_done': 0}})
    aw.supersede_torrent('H')
    assert _removed(rec) == [] and ('H', SUP) in rec['label']

    # Finished (seeding) torrent: normal relabel + move, no cap change, never removed
    rec = _install(aw, {'H': {'state': 'Seeding', 'progress': 100.0, 'total_done': 99}})
    aw.supersede_torrent('H')
    assert _removed(rec) == [] and opts(rec) == [] and ('H', SUP) in rec['label'] and ('H', aw.SEEDING_DIR) in rec['move']

    # Probe failure fails safe to the relabel path
    rec = _install(aw, {}, boom=True)
    aw.supersede_torrent('H')
    assert _removed(rec) == [] and ('H', SUP) in rec['label']

    # ── cleanup_radarr_queue_dupes end to end ────────────────────────────
    aw.RADARR_URL, aw.RADARR_API_KEY, aw.RADARR_UPG_LABEL = 'http://radarr', 'k', 'radarr-upgrade'
    real_requests = aw.requests
    keeper, loser = 'k' * 40, 'l' * 40
    try:
        rec = _install(aw, {loser: {'state': 'Downloading', 'progress': 0.1, 'total_done': 5}})
        torrents = {
            keeper: {'name': 'Movie 2026 DUDU', 'label': 'radarr-upgrade', 'state': 'Queued', 'progress': 0.0},
            loser: {'name': 'Movie 2026 HONE', 'label': 'radarr', 'state': 'Downloading', 'progress': 0.1},
        }
        aw.get_all_torrents = lambda: torrents
        queue = [{'id': 7, 'movieId': 1, 'downloadId': loser.upper(), 'title': 'Movie 2026 HONE',
                  'customFormatScore': 100, 'size': 100, 'sizeleft': 99}]
        hist = {'records': [{'eventType': 'grabbed', 'date': '2026-10-06T00:00:00Z', 'movieId': 1,
                             'data': {'customFormatScore': 116}}]}
        deleted = []
        aw.requests = type('R', (), {
            'get': staticmethod(lambda url, **kw: _Resp({'records': queue}) if url.endswith('/queue') else _Resp(hist)),
            'delete': staticmethod(lambda url, **kw: deleted.append(url) or _Resp({})),
        })()
        assert aw.cleanup_radarr_queue_dupes() == 1
        assert deleted == ['http://radarr/api/v3/queue/7']
        assert _removed(rec) == [], 'downloading loser must never be removed'
        assert (loser, SUP) in rec['label'], 'loser stays labeled superseded'
        assert ([[loser], {'max_download_speed': -1}]) in [p for m, p in rec['rpc'] if m == 'core.set_torrent_options'], 'cap must be lifted so it can finish'
        assert ('core.queue_top', [[keeper]]) in rec['rpc'], 'surviving keeper must be moved to the top'

        # ── pending-import guard ─────────────────────────────────────────
        now = datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc)

        def hist_for(records):
            aw.requests = type('R', (), {'get': staticmethod(lambda url, **kw: _Resp({'records': records}))})()

        hist_for([{'eventType': 'grabbed', 'date': '2026-10-08T00:00:00Z'}])
        assert aw._radarr_import_pending('h', now=now) is True, 'fresh un-imported grab is protected'
        hist_for([{'eventType': 'grabbed', 'date': '2026-10-08T00:00:00Z'},
                  {'eventType': 'downloadFolderImported', 'date': '2026-10-08T01:00:00Z'}])
        assert aw._radarr_import_pending('h', now=now) is False, 'imported -> not pending'
        hist_for([{'eventType': 'grabbed', 'date': '2026-09-01T00:00:00Z'}])
        assert aw._radarr_import_pending('h', now=now) is False, 'old stuck grab may be superseded'

        def boom_get(url, **kw):
            raise RuntimeError('radarr down')
        aw.requests = type('R', (), {'get': staticmethod(boom_get)})()
        assert aw._radarr_import_pending('h', now=now) is True, 'lookup error fails safe to protect'
    finally:
        aw.requests = real_requests

    print('test_supersede_inflight: all assertions passed')


if __name__ == '__main__':
    main()
