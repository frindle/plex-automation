"""arr_health: outage pushes, removed-from-TVDB pushes, broken-file repair (dry run by default).
Synthetic data and a fake I/O object; no network. Run: python -m pytest test_arr_health.py"""
import importlib.util
import os
import tempfile

import arr_health as ah

T0 = 2_000_000_000.0
MIN = 60
TV = '/data/Media/TV Shows'
EP_PATH = f'{TV}/Show A/Season 01/Show A - S01E02 - Pilot HDTV-720p.mp4'
MOVIE_PATH = '/data/Media/Movies/Film B (2001)/Film B (2001) Bluray-1080p.mkv'


def err(path, t='2026-11-15T10:00:00Z', tail=': ffprobe exited with non-zero exit-code (1 - moov atom not found)'):
    return {'time': t, 'level': 'error', 'message': f'Unable to parse media info from file: {path}{tail}'}


class FakeIO:
    def __init__(self, services=('Sonarr', 'Radarr')):
        self.services = list(services)
        self.health = {s: [] for s in services}
        self.logs = {s: [] for s in services}
        self.down = set()
        self.deluge_up = True
        self.series = [{'id': 5, 'title': 'Show A', 'path': f'{TV}/Show A', 'monitored': True}]
        self.movies = [{'id': 9, 'title': 'Film B', 'path': '/data/Media/Movies/Film B (2001)', 'monitored': True}]
        self.epfiles = {5: [{'id': 50, 'path': EP_PATH, 'mediaInfo': {}}]}
        self.moviefiles = {9: [{'id': 90, 'path': MOVIE_PATH, 'mediaInfo': None}]}
        self.episodes = {5: [{'id': 501, 'episodeFileId': 50, 'seasonNumber': 1, 'episodeNumber': 2, 'monitored': True}]}
        self.history = {('Sonarr', 5): [], ('Radarr', 9): []}
        self.torrents = set()
        self.mtimes = {}
        self.pushes, self.records, self.posts, self.deletes, self.rechecks, self.gets = [], [], [], [], [], []

    def get(self, who, path, params=None):
        self.gets.append((who, path, params))
        if who in self.down:
            raise ConnectionError('down')
        if path == '/api/v3/health':
            return self.health[who]
        if path == '/api/v3/log':
            return {'records': self.logs[who]}
        if path == '/api/v3/series':
            return self.series
        if path == '/api/v3/movie':
            return self.movies
        if path == '/api/v3/episodefile':
            return self.epfiles.get(params['seriesId'], [])
        if path == '/api/v3/moviefile':
            return self.moviefiles.get(params['movieId'], [])
        if path == '/api/v3/episode':
            return self.episodes.get(params['seriesId'], [])
        if path == '/api/v3/history/series':
            return self.history[('Sonarr', params['seriesId'])]
        if path == '/api/v3/history/movie':
            return self.history[('Radarr', params['movieId'])]
        raise AssertionError(f'unexpected GET {who} {path}')

    def post(self, who, path, body):
        self.posts.append((who, path, body))

    def delete(self, who, path):
        self.deletes.append((who, path))

    def deluge_ok(self):
        if self.deluge_up is None:
            raise ConnectionError('refused')
        return self.deluge_up

    def deluge_has(self, h):
        return h in self.torrents

    def deluge_recheck(self, h):
        self.rechecks.append(h)

    def push(self, title, message):
        self.pushes.append((title, message))

    def record(self, category, summary):
        self.records.append((category, summary))

    def file_mtime(self, path):
        return self.mtimes.get(path)

    def log(self, msg):
        pass


def H(source, message, type_='error'):
    return {'source': source, 'type': type_, 'message': message}


# ── outages ───────────────────────────────────────────────────────────────────

def test_outage_pushes_once_after_30_min_and_on_recovery():
    io, st = FakeIO(), {}
    io.health['Sonarr'] = [H('IndexerStatusCheck', 'Indexers unavailable due to failures: Torznab')]
    for m in (0, 5, 25):
        ah.outage_tick(io, st, T0 + m * MIN)
    assert io.pushes == []
    ah.outage_tick(io, st, T0 + 30 * MIN)
    ah.outage_tick(io, st, T0 + 35 * MIN)
    assert len(io.pushes) == 1 and 'Torznab' in io.pushes[0][1] and 'indexers failing' in io.pushes[0][0]
    io.health['Sonarr'] = []
    ah.outage_tick(io, st, T0 + 40 * MIN)
    assert len(io.pushes) == 2 and 'recovered' in io.pushes[1][0] and st['outages'] == {}


def test_short_blip_and_unrelated_health_never_push():
    io, st = FakeIO(), {}
    io.health['Radarr'] = [H('DownloadClientCheck', 'Unable to communicate with Deluge'),
                           H('UpdateCheck', 'update available', 'warning'),
                           H('ServerSideNotificationCheck', 'radarr.servarr.com 521'),
                           H('IndexerStatusCheck', 'info only', 'notice')]
    ah.outage_tick(io, st, T0)
    assert set(st['outages']) == {'Radarr:DownloadClientCheck'}
    io.health['Radarr'] = []
    ah.outage_tick(io, st, T0 + 10 * MIN)
    assert io.pushes == [] and st['outages'] == {}


def test_unreachable_arr_and_deluge_are_outages():
    io, st = FakeIO(), {}
    io.down.add('Radarr')
    io.deluge_up = None
    ah.outage_tick(io, st, T0)
    assert set(st['outages']) == {'Radarr:unreachable', 'Deluge:direct'}
    ah.outage_tick(io, st, T0 + 15 * MIN)
    ah.outage_tick(io, st, T0 + 31 * MIN)
    titles = sorted(p[0] for p in io.pushes)
    assert titles == ['Deluge not answering', 'Radarr not answering']


def test_stale_outage_restarts_its_clock():
    io, st = FakeIO(), {}
    io.deluge_up = False
    ah.outage_tick(io, st, T0)
    # Container was off for hours; the old start time must not count.
    ah.outage_tick(io, st, T0 + 5 * 3600)
    assert io.pushes == [] and st['outages']['Deluge:direct']['since'] == T0 + 5 * 3600


# ── removed from TheTVDB ──────────────────────────────────────────────────────

TRAITORS = H('RemovedSeriesCheck', 'Series The Traitors: New Blood (tvdbid 466894) was removed from TheTVDB')


def test_removed_baseline_is_silent_then_new_ones_push_once():
    io, st = FakeIO(), {}
    ah.removed_tick(io, st, 'Sonarr', [TRAITORS])
    assert io.pushes == [] and 'Sonarr:tvdbid:466894' in st['removed']['seen']
    two = H('RemovedSeriesCheck', 'Series The Traitors: New Blood (tvdbid 466894), Lost Show (tvdbid 12345) were removed from TheTVDB')
    ah.removed_tick(io, st, 'Sonarr', [two])
    ah.removed_tick(io, st, 'Sonarr', [two])
    assert len(io.pushes) == 1
    assert 'Lost Show (tvdbid 12345)' in io.pushes[0][1] and 'TheTVDB' in io.pushes[0][0]


def test_removed_movie_parsed_and_radarr_baselined_separately():
    io, st = FakeIO(), {}
    ah.removed_tick(io, st, 'Sonarr', [TRAITORS])
    ah.removed_tick(io, st, 'Radarr', [])
    ah.removed_tick(io, st, 'Radarr', [H('RemovedMovieCheck', 'Movie Gone Film (tmdbid 777) was removed from TMDb')])
    assert io.pushes == [('Radarr: removed from TMDb', io.pushes[0][1])]
    assert 'Gone Film (tmdbid 777)' in io.pushes[0][1]


def test_removed_entries_ignore_other_sources():
    assert ah.removed_entries([H('IndexerStatusCheck', 'x (tvdbid 1)'), 'junk', None]) == []


# ── broken files ──────────────────────────────────────────────────────────────

def test_log_paths_parsed():
    recs = [err(EP_PATH), err(EP_PATH, t='2026-11-15T12:00:00Z'), err(MOVIE_PATH, tail=''),
            {'message': 'Unable to parse media info from file: relative.mkv: x'}, {'message': 'other'}, None]
    assert ah.mediainfo_error_paths(recs) == {EP_PATH: '2026-11-15T12:00:00Z', MOVIE_PATH: '2026-11-15T10:00:00Z'}


def _pass(io, st, t, apply=False):
    return ah.broken_files_pass(io, st, t, apply=apply)


def test_new_broken_file_is_rescanned_then_confirmed_dry_run_changes_nothing():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    _pass(io, st, T0)
    assert io.posts == [('Sonarr', '/api/v3/command', {'name': 'RescanSeries', 'seriesId': 5})]
    assert st['broken'][EP_PATH]['state'] == 'rescanned'
    _pass(io, st, T0 + 30 * MIN)    # too soon to confirm
    assert st['broken'][EP_PATH]['state'] == 'rescanned' and len(io.posts) == 1
    rows = _pass(io, st, T0 + 6 * 3600)
    b = st['broken'][EP_PATH]
    assert b['state'] == 'confirmed' and b['outcome'] == 'dry run' and 'delete the file in Sonarr' in b['would']
    assert b['title'] == 'Show A S01E02' and rows[0]['path'] == EP_PATH
    assert io.deletes == [] and io.rechecks == [] and len(io.posts) == 1 and io.pushes == []
    assert any('confirmed' in r[1] for r in io.records)


def test_apply_deletes_and_searches_sonarr_and_radarr():
    io, st = FakeIO(), {}
    io.logs['Sonarr'], io.logs['Radarr'] = [err(EP_PATH)], [err(MOVIE_PATH)]
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert ('Sonarr', '/api/v3/episodefile/50') in io.deletes and ('Radarr', '/api/v3/moviefile/90') in io.deletes
    assert ('Sonarr', '/api/v3/command', {'name': 'EpisodeSearch', 'episodeIds': [501]}) in io.posts
    assert ('Radarr', '/api/v3/command', {'name': 'MoviesSearch', 'movieIds': [9]}) in io.posts
    assert ('Radarr', '/api/v3/command', {'name': 'RescanMovie', 'movieId': 9}) in io.posts
    assert st['broken'] == {}


def test_a_file_someone_is_playing_waits_and_is_replaced_once_they_stop():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    io.playing = {EP_PATH}
    io.busy = lambda p: p in io.playing
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert io.deletes == [] and st['broken'][EP_PATH]['outcome'] == 'waiting: someone is playing it'
    io.playing = set()
    _pass(io, st, T0 + 3 * 3600, apply=True)
    assert io.deletes == [('Sonarr', '/api/v3/episodefile/50')]


def test_source_torrent_in_deluge_is_rechecked_before_any_delete():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    h = 'ab' * 20
    io.history[('Sonarr', 5)] = [
        {'episodeId': 999, 'downloadId': 'CD' * 20, 'date': '2026-11-14', 'data': {'importedPath': '/x'}},
        {'episodeId': 501, 'downloadId': h.upper(), 'date': '2026-11-01', 'data': {'importedPath': EP_PATH}}]
    io.torrents.add(h)
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert io.rechecks == [h] and io.deletes == [] and st['broken'][EP_PATH]['state'] == 'rechecked'
    _pass(io, st, T0 + 2.5 * 3600, apply=True)   # too soon after the recheck
    assert io.deletes == []
    _pass(io, st, T0 + 4 * 3600, apply=True)     # recheck didn't fix it
    assert io.rechecks == [h] and io.deletes == [('Sonarr', '/api/v3/episodefile/50')]


def test_recheck_fixes_it_and_nothing_is_deleted():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    h = 'ab' * 20
    io.history[('Sonarr', 5)] = [{'episodeId': 501, 'downloadId': h, 'date': '2026-11-01', 'data': {}}]
    io.torrents.add(h)
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    io.epfiles[5][0]['mediaInfo'] = {'videoCodec': 'x264'}
    _pass(io, st, T0 + 4 * 3600, apply=True)
    assert io.deletes == [] and st['broken'] == {}
    assert any('reads fine again' in r[1] for r in io.records)


def test_readable_file_is_dropped_and_not_looked_up_again_until_a_new_error():
    io, st = FakeIO(services=['Sonarr']), {}
    io.epfiles[5][0]['mediaInfo'] = {'videoCodec': 'h265'}
    io.logs['Sonarr'] = [err(EP_PATH)]
    _pass(io, st, T0)
    assert st['broken'] == {} and io.posts == [] and st['fine'] == {EP_PATH: '2026-11-15T10:00:00Z'}
    n = len(io.gets)
    _pass(io, st, T0 + 6 * 3600)
    assert [g[1] for g in io.gets[n:]] == ['/api/v3/log']
    io.logs['Sonarr'].append(err(EP_PATH, t='2026-11-16T10:00:00Z'))
    io.epfiles[5][0]['mediaInfo'] = {}
    _pass(io, st, T0 + 12 * 3600)
    assert st['broken'][EP_PATH]['state'] == 'rescanned'


def test_file_still_copying_is_left_alone():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    io.mtimes[EP_PATH] = T0 - 10 * MIN
    _pass(io, st, T0, apply=True)
    assert st['broken'][EP_PATH]['state'] == 'copying' and io.posts == []
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert st['broken'][EP_PATH]['state'] == 'rescanned' and io.deletes == []


def test_file_gone_from_arr_is_dropped():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    _pass(io, st, T0)
    io.epfiles[5] = [{'id': 51, 'path': EP_PATH.replace('HDTV-720p.mp4', 'WEBDL-1080p.mkv'), 'mediaInfo': {'videoCodec': 'h265'}}]
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert st['broken'] == {} and io.deletes == []


def test_unmonitored_is_never_deleted():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    io.episodes[5][0]['monitored'] = False
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    b = st['broken'][EP_PATH]
    assert io.deletes == [] and b['outcome'] == 'left alone (not monitored)' and 'not monitored' in b['would']


def _many(io, n):
    io.series = [{'id': i, 'title': f'S{i}', 'path': f'{TV}/S{i}', 'monitored': True} for i in range(n)]
    io.epfiles = {i: [{'id': 100 + i, 'path': f'{TV}/S{i}/e.mkv', 'mediaInfo': {}}] for i in range(n)}
    io.episodes = {i: [{'id': 200 + i, 'episodeFileId': 100 + i, 'monitored': True}] for i in range(n)}
    io.history = {('Sonarr', i): [] for i in range(n)}
    io.logs['Sonarr'] = [err(f'{TV}/S{i}/e.mkv') for i in range(n)]


def test_mass_breakage_changes_nothing_and_pushes_once():
    io, st = FakeIO(services=['Sonarr']), {}
    _many(io, 8)
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    _pass(io, st, T0 + 8 * 3600, apply=True)
    assert io.deletes == [] and len(io.pushes) == 1 and 'nothing was changed' in io.pushes[0][1]
    assert all(b['outcome'] == 'held: too many at once' for b in st['broken'].values())


def test_at_most_three_replacements_per_pass():
    io, st = FakeIO(services=['Sonarr']), {}
    _many(io, 5)
    _pass(io, st, T0, apply=True)
    _pass(io, st, T0 + 2 * 3600, apply=True)
    assert len(io.deletes) == 3 and len(st['broken']) == 2
    assert all(b['outcome'].startswith('waiting') for b in st['broken'].values())
    _pass(io, st, T0 + 8 * 3600, apply=True)
    assert len(io.deletes) == 5 and st['broken'] == {}
    _pass(io, st, T0 + 14 * 3600, apply=True)   # old log errors for replaced files don't come back
    assert st['broken'] == {} and len(io.deletes) == 5


def test_log_read_failure_keeps_entries():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    _pass(io, st, T0)
    io.down.add('Sonarr')
    _pass(io, st, T0 + 2 * 3600)
    assert EP_PATH in st['broken']


def test_digest_html_lists_outages_and_files():
    io, st = FakeIO(services=['Sonarr']), {}
    io.logs['Sonarr'] = [err(EP_PATH)]
    _pass(io, st, T0)
    io.deluge_up = False
    ah.outage_tick(io, st, T0)
    html = ah.digest_html(st)
    assert 'Broken library files: 1' in html and 'Show A S01E02' in html and 'dry run' in html
    assert 'Down right now: 1' in html and 'Deluge not answering' in html
    assert ah.digest_html({}) == ''


# ── webhook wiring ────────────────────────────────────────────────────────────

def _load_webhook(tmp):
    os.environ['ACTIVITY_LOG'] = os.path.join(tmp, 'activity.jsonl')
    os.environ['ARR_HEALTH_STATE'] = os.path.join(tmp, 'arr_health_state.json')
    spec = importlib.util.spec_from_file_location(
        'arr_webhook_health', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
    aw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aw)
    return aw


def test_tick_persists_state_and_runs_broken_pass_on_its_own_interval():
    tmp = tempfile.mkdtemp()
    aw = _load_webhook(tmp)
    assert aw.ARR_HEALTH_APPLY is False
    io = FakeIO(services=['Sonarr'])
    io.logs['Sonarr'] = [err(EP_PATH)]
    io.health['Sonarr'] = [TRAITORS]
    st = aw.arr_health_tick(now=T0, io=io)
    assert st['broken'][EP_PATH]['state'] == 'rescanned' and st['removed']['baselined'] == ['Sonarr']
    n = len(io.posts)
    aw.arr_health_tick(now=T0 + 5 * MIN, io=io)          # broken pass waits for ARR_BROKEN_INTERVAL
    assert len(io.posts) == n
    st = aw.arr_health_tick(now=T0 + 6 * 3600, io=io)
    assert st['broken'][EP_PATH]['state'] == 'confirmed' and io.deletes == []
    assert aw._arr_health_load()['broken'][EP_PATH]['outcome'] == 'dry run'
    assert 'Broken library files: 1' in aw._digest_arr_health_html()
