"""dedup_via_sonarr must not supersede a finished, not-yet-imported upgrade
(Sonarr parity with the Radarr pending-import guard). No framework, no live
services. Run: python test_sonarr_pending_import.py"""
import datetime


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def main():
    aw = __import__('arr-webhook')
    aw.SONARR_URL, aw.SONARR_API_KEY = 'http://sonarr', 'k'
    real = aw.requests
    now = datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc)

    def hist_for(records):
        aw.requests = type('R', (), {'get': staticmethod(lambda url, **kw: _Resp({'records': records}))})()

    try:
        hist_for([{'eventType': 'grabbed', 'date': '2026-10-08T00:00:00Z'}])
        assert aw._sonarr_import_pending('h', now=now) is True
        hist_for([{'eventType': 'grabbed', 'date': '2026-10-08T00:00:00Z'},
                  {'eventType': 'downloadFolderImported', 'date': '2026-10-08T01:00:00Z'}])
        assert aw._sonarr_import_pending('h', now=now) is False
        hist_for([{'eventType': 'grabbed', 'date': '2026-09-01T00:00:00Z'}])
        assert aw._sonarr_import_pending('h', now=now) is False

        # end to end: a fresh un-imported season pack in a season whose keeper
        # is singles, plus an old imported pack -> only the stale one is hidden.
        fresh, stale = 'f' * 40, 's' * 40
        torrents = {
            fresh: {'name': 'Show.2024.S01.2160p.WEB.H.265-NEW', 'label': 'sonarr-upgrade', 'progress': 100.0},
            stale: {'name': 'Show.2024.S01.1080p.WEB.H.264-OLD', 'label': 'sonarr', 'progress': 100.0},
        }
        hist = {
            fresh: [{'eventType': 'grabbed', 'date': '2026-10-08T00:00:00Z'}],
            stale: [{'eventType': 'grabbed', 'date': '2026-09-01T00:00:00Z'}],
        }

        def get(url, **kw):
            if url.endswith('/api/v3/series'):
                return _Resp([{'id': 1, 'title': 'Show'}])
            if url.endswith('/api/v3/history'):
                return _Resp({'records': hist[kw['params']['downloadId'].lower()]})
            return _Resp([])
        aw.requests = type('R', (), {'get': staticmethod(get)})()
        aw.SONARR_UPG_LABEL = 'sonarr-upgrade'
        aw.deluge_login = lambda: None
        aw.ensure_label_exists = lambda: None
        aw.get_all_torrents = lambda: torrents
        aw.get_sonarr_series_titles = lambda sid: {'show'}
        aw.requests.get = staticmethod(lambda url, **kw: (
            _Resp([{'relativePath': 'x.mkv'}]) if url.endswith('/episodefile') else get(url, **kw)))
        aw._sonarr_series_imported_download_ids = lambda sid: set()
        aw._sonarr_latest_keeper_by_episode_key = lambda sid: {}
        aw._sonarr_latest_source_title_by_episode_key = lambda sid: {}
        aw._sonarr_keeper_pack_info = lambda sid: ({'e' * 40}, set())
        aw._sonarr_keeper_single_seasons = lambda sid: {1}
        done = []
        aw.supersede_torrent = lambda h: done.append(h)
        aw.record_activity = lambda *a, **k: None
        aw.dedup_via_sonarr()
        assert fresh not in done, 'fresh un-imported upgrade pack must be protected'
        assert stale in done, 'old stuck pack is still superseded'
    finally:
        aw.requests = real
    print('test_sonarr_pending_import: all assertions passed')


if __name__ == '__main__':
    main()
