"""Yearly-upgrade batch machinery: ordering, cursor walk, interval gate, and
the /run-monthly-upgrade route -- bounded one-batch-per-service passes."""
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
sys_modules_patch = __import__('sys').modules.setdefault("target", target)
spec.loader.exec_module(target)


def test_sort_ids_by_year_desc_ordering():
    items = [
        {'id': 1, 'year': 2019},
        {'id': 2, 'firstAired': '2023-05-01T00:00:00Z'},
        {'id': 3},
        {'id': 4, 'year': 2023},
        {'id': 5, 'firstAired': 'not-a-date'},
    ]
    ordered = target.sort_ids_by_year_desc(items)
    assert [i['id'] for i in ordered] == [2, 4, 1, 3, 5]
    # input list not mutated
    assert [i['id'] for i in items] == [1, 2, 3, 4, 5]


def test_sort_ids_by_year_desc_stable_ties():
    items = [{'id': 'a', 'year': 2020}, {'id': 'b', 'firstAired': '2020-01-01'},
             {'id': 'c'}]
    ordered = target.sort_ids_by_year_desc(items)
    assert [i['id'] for i in ordered] == ['a', 'b', 'c']


def test_advance_upgrade_cursor_wraparound(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 3)
    idx, cur = target.advance_upgrade_cursor(0, 7)
    assert (list(idx), cur) == ([0, 1, 2], 3)
    idx, cur = target.advance_upgrade_cursor(cur, 7)
    assert (list(idx), cur) == ([3, 4, 5], 6)
    idx, cur = target.advance_upgrade_cursor(cur, 7)
    assert (list(idx), cur) == ([6, 0, 1], 2)


def test_advance_upgrade_cursor_stale_clamp_and_empty(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 3)
    idx, cur = target.advance_upgrade_cursor(49, 4)
    assert (list(idx), cur) == ([3, 0, 1], 2)
    assert target.advance_upgrade_cursor(5, 0) == ([], 0)


def test_interval_gate(monkeypatch):
    from datetime import datetime, timedelta
    now = datetime(2026, 1, 15, 12, 0, 0)
    interval = target.UPGRADE_BATCH_INTERVAL_DAYS
    assert target.upgrade_batch_due({}, now) is True          # first-ever poll
    recent = {'last_run': (now - timedelta(days=interval - 1)).isoformat()}
    assert target.upgrade_batch_due(recent, now) is False     # inside window
    exact = {'last_run': (now - timedelta(days=interval)).isoformat()}
    assert target.upgrade_batch_due(exact, now) is True       # boundary
    old = {'last_run': (now - timedelta(days=interval + 10)).isoformat()}
    assert target.upgrade_batch_due(old, now) is True         # past window
    assert target.upgrade_batch_due({'last_run': 'garbage'}, now) is True


def test_run_monthly_upgrade_one_batch_per_service(monkeypatch):
    calls = []
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)
    monkeypatch.setattr(target, 'radarr_bulk_search', lambda: calls.append('radarr'))
    monkeypatch.setattr(target, 'sonarr_bulk_search', lambda: calls.append('sonarr'))
    monkeypatch.setattr(target, 'purge_stalled_upgrade_torrents', lambda label=None: None)
    monkeypatch.setattr(target, 'relabel_radarr_upgrades', lambda: None)
    monkeypatch.setattr(target, 'verify_and_fix_labels', lambda services=('radarr', 'sonarr'): [])
    client = target.app.test_client()
    r = client.post('/run-monthly-upgrade?service=radarr&skip_waits=1')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and body['service'] == 'radarr'
    import time as _t
    for _ in range(50):
        if calls:
            break
        _t.sleep(0.02)
    assert calls.count('radarr') == 1      # exactly one bounded batch pass
    assert 'sonarr' not in calls           # no full-catalog / other-service sweep


def test_run_monthly_upgrade_invalid_service(monkeypatch):
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)
    client = target.app.test_client()
    r = client.post('/run-monthly-upgrade?service=plex')
    assert r.status_code == 400
    body = r.get_json()
    assert body['ok'] is False and 'error' in body


def _bulk_search_setup(monkeypatch, lane_ids, movie_ids=(1, 2, 3, 4), at_cutoff=(), refresh=None):
    posted = []
    saved = []
    refreshed = []

    class Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._p

    def fake_get(url, **kw):
        return Resp([{'id': i, 'monitored': True, 'year': 2020, 'hasFile': True,
                      'movieFile': {'qualityCutoffNotMet': i not in at_cutoff}} for i in movie_ids])

    def fake_refresh(service, ids):
        refreshed.append(list(ids))
        return list(ids) if refresh is None else refresh(ids)

    def fake_post(url, **kw):
        posted.append(kw['json'])
        return Resp({'id': 7})

    monkeypatch.setattr(target.requests, 'get', fake_get)
    monkeypatch.setattr(target.requests, 'post', fake_post)
    monkeypatch.setattr(target, 'refresh_and_confirm', fake_refresh)
    monkeypatch.setattr(target, '_load_upgrade_state', lambda: {})
    monkeypatch.setattr(target, '_save_upgrade_state', lambda st: saved.append(st))
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 10)
    if isinstance(lane_ids, Exception):
        def boom():
            raise lane_ids
        monkeypatch.setattr(target, 'radarr_lane_movie_ids', boom)
    else:
        monkeypatch.setattr(target, 'radarr_lane_movie_ids', lambda: set(lane_ids))
    _bulk_search_setup.refreshed = refreshed
    return posted, saved


def test_radarr_bulk_search_skips_movies_already_in_lane(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, {2, 3})
    target.radarr_bulk_search()
    assert len(posted) == 1
    assert sorted(posted[0]['movieIds']) == [1, 4]
    assert saved  # cursor still advances


def test_radarr_bulk_search_all_in_lane_posts_nothing(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, {1, 2, 3, 4})
    target.radarr_bulk_search()
    assert posted == []
    assert saved


def test_radarr_bulk_search_fails_closed_when_lane_unreadable(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, RuntimeError('deluge down'))
    target.radarr_bulk_search()
    assert posted == []
    assert saved == []  # cursor not advanced, retried next pass


def test_radarr_lane_movie_ids_maps_both_lane_labels(monkeypatch):
    torrents = {
        'aaa': {'label': target.RADARR_UPG_LABEL},
        'bbb': {'label': target.RADARR_UPG_PRIORITY_LABEL},
        'ccc': {'label': 'radarr'},
        'ddd': {'label': target.RADARR_UPG_LABEL},
    }
    monkeypatch.setattr(target, 'deluge_login', lambda: None)
    monkeypatch.setattr(target, 'get_all_torrents', lambda force=False: torrents)
    ids = {'aaa': 11, 'bbb': 22, 'ccc': 33, 'ddd': None}
    monkeypatch.setattr(target, '_radarr_grab_identity', lambda h, strict=False: (ids[h], 0))
    assert target.radarr_lane_movie_ids() == {11, 22}


def test_radarr_lane_movie_ids_fails_closed_on_history_error(monkeypatch):
    import pytest
    torrents = {'aaa': {'label': target.RADARR_UPG_LABEL}}
    monkeypatch.setattr(target, 'deluge_login', lambda: None)
    monkeypatch.setattr(target, 'get_all_torrents', lambda force=False: torrents)
    monkeypatch.setattr(target, 'RADARR_API_KEY', 'k')

    def boom(*a, **kw):
        raise RuntimeError('radarr down')
    monkeypatch.setattr(target.requests, 'get', boom)
    with pytest.raises(RuntimeError):
        target.radarr_lane_movie_ids()


def _sonarr_bulk_setup(monkeypatch, lane_ids, series_ids=(1, 2, 3, 4), at_cutoff=()):
    posted = []
    saved = []

    class Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._p

    monkeypatch.setattr(target.requests, 'get',
                        lambda url, **kw: Resp([{'id': i, 'monitored': True, 'year': 2020} for i in series_ids]))
    monkeypatch.setattr(target.requests, 'post', lambda url, **kw: (posted.append(kw['json']), Resp({'id': 7}))[1])
    monkeypatch.setattr(target, 'refresh_and_confirm', lambda service, ids: list(ids))
    monkeypatch.setattr(target, '_sonarr_wanted_series_ids', lambda: set(series_ids) - set(at_cutoff))
    monkeypatch.setattr(target, '_load_upgrade_state', lambda: {})
    monkeypatch.setattr(target, '_save_upgrade_state', lambda st: saved.append(st))
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 10)
    if isinstance(lane_ids, Exception):
        def boom():
            raise lane_ids
        monkeypatch.setattr(target, 'sonarr_lane_series_ids', boom)
    else:
        monkeypatch.setattr(target, 'sonarr_lane_series_ids', lambda: set(lane_ids))
    return posted, saved


def test_sonarr_bulk_search_skips_series_already_in_lane(monkeypatch):
    posted, saved = _sonarr_bulk_setup(monkeypatch, {2, 3})
    target.sonarr_bulk_search()
    assert sorted(p['seriesId'] for p in posted) == [1, 4]
    assert saved


def test_sonarr_bulk_search_fails_closed_when_lane_unreadable(monkeypatch):
    posted, saved = _sonarr_bulk_setup(monkeypatch, RuntimeError('deluge down'))
    target.sonarr_bulk_search()
    assert posted == []
    assert saved == []


def test_sonarr_lane_series_ids_maps_both_labels_and_fails_closed(monkeypatch):
    import pytest
    torrents = {
        'aaa': {'label': target.SONARR_UPG_LABEL},
        'bbb': {'label': target.SONARR_UPG_PRIORITY_LABEL},
        'ccc': {'label': 'sonarr'},
    }
    monkeypatch.setattr(target, 'deluge_login', lambda: None)
    monkeypatch.setattr(target, 'get_all_torrents', lambda force=False: torrents)
    ids = {'aaa': 11, 'bbb': 22, 'ccc': 33}
    monkeypatch.setattr(target, '_sonarr_grab_identity',
                        lambda h, strict=False: (ids[h], frozenset({1}), 0))
    assert target.sonarr_lane_series_ids() == {11, 22}

    monkeypatch.setattr(target, 'SONARR_API_KEY', 'k')
    monkeypatch.undo()
    monkeypatch.setattr(target, 'deluge_login', lambda: None)
    monkeypatch.setattr(target, 'get_all_torrents', lambda force=False: torrents)
    monkeypatch.setattr(target, 'SONARR_API_KEY', 'k')
    monkeypatch.setattr(target.requests, 'get', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    with pytest.raises(RuntimeError):
        target.sonarr_lane_series_ids()


# ── refill: a pass searches no more items than the lane has free slots ──────

def test_advance_upgrade_cursor_size_override(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 12)
    assert target.advance_upgrade_cursor(5, 20, 1) == ([5], 6)
    assert target.advance_upgrade_cursor(19, 20, 2) == ([19, 0], 1)
    assert target.advance_upgrade_cursor(0, 20, 0) == ([0], 1)  # never an empty pass
    assert target.advance_upgrade_cursor(0, 20)[1] == 12      # default unchanged


def test_pass_size_clamps_to_batch_size(monkeypatch):
    monkeypatch.setattr(target, 'UPGRADE_BATCH_SIZE', 12)
    assert target._pass_size(None) == 12
    assert target._pass_size(3) == 3
    assert target._pass_size(50) == 12
    assert target._pass_size(0) == 1


def test_radarr_bulk_search_limit_shrinks_pass(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, set())
    target.radarr_bulk_search(limit=1)
    assert len(posted) == 1 and len(posted[0]['movieIds']) == 1
    assert saved[-1]['radarr']['cursor'] == 1


def test_sonarr_bulk_search_limit_shrinks_pass(monkeypatch):
    posted, saved = _sonarr_bulk_setup(monkeypatch, set())
    target.sonarr_bulk_search(limit=2)
    assert len(posted) == 2
    assert saved[-1]['sonarr']['cursor'] == 2


def test_monthly_upgrade_cycle_passes_limit(monkeypatch):
    got = []
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)
    monkeypatch.setattr(target, 'purge_stalled_upgrade_torrents', lambda label=None: None)
    monkeypatch.setattr(target, 'radarr_bulk_search', lambda limit=None: got.append(limit))
    monkeypatch.setattr(target, 'relabel_radarr_upgrades', lambda: 0)
    monkeypatch.setattr(target, 'enforce_upgrade_lane_cap', lambda: {})
    monkeypatch.setattr(target, '_load_upgrade_state', lambda: {})
    monkeypatch.setattr(target, '_save_upgrade_state', lambda st: None)
    target.monthly_upgrade_cycle('radarr', 0, 0, limit=3)
    target.monthly_upgrade_cycle('radarr', 0, 0)
    assert got == [3, None]


# --- refresh and confirm before an upgrade search -----------------------------------------------------------

def test_pick_upgrade_batch_passes_over_cutoff_met_and_lane():
    ids = [10, 11, 12, 13, 14, 15]
    batch, nxt = target.pick_upgrade_batch(ids, 1, 2, wanted={11, 13, 14, 15}, skip={13})
    assert batch == [11, 14]
    assert nxt == 5  # index after the last id looked at (14)


def test_pick_upgrade_batch_wraps_and_looks_at_each_id_once():
    batch, nxt = target.pick_upgrade_batch([1, 2, 3], 2, 5, wanted={1, 3})
    assert batch == [3, 1]
    assert nxt == 2
    assert target.pick_upgrade_batch([], 0, 3, wanted={1}) == ([], 0)
    assert target.pick_upgrade_batch([1, 2], 9, 1, wanted=set()) == ([], 1)  # stale cursor clamps


def test_radarr_wants_search():
    w = target.radarr_wants_search
    assert w({'monitored': True, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': True}})
    assert not w({'monitored': True, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': False}})
    assert w({'monitored': True, 'hasFile': False, 'isAvailable': True})
    assert not w({'monitored': True, 'hasFile': False, 'isAvailable': False})
    assert not w({'monitored': False, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': True}})
    assert not w(None)


def test_radarr_bulk_search_only_searches_movies_below_cutoff(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, set(), at_cutoff={2, 3})
    target.radarr_bulk_search()
    assert _bulk_search_setup.refreshed == [[1, 4]]
    assert [p['movieIds'] for p in posted] == [[1, 4]]


def test_radarr_bulk_search_drops_what_refresh_shows_is_done(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, set(), refresh=lambda ids: [i for i in ids if i != 1])
    target.radarr_bulk_search()
    assert [p['movieIds'] for p in posted] == [[2, 3, 4]]
    assert saved


def test_radarr_bulk_search_refresh_failure_searches_nothing(monkeypatch):
    posted, saved = _bulk_search_setup(monkeypatch, set(), refresh=lambda ids: None)
    target.radarr_bulk_search()
    assert posted == []
    assert saved == []  # cursor not advanced, retried next pass


def test_sonarr_bulk_search_only_searches_wanted_series(monkeypatch):
    posted, saved = _sonarr_bulk_setup(monkeypatch, set(), at_cutoff={1, 3})
    target.sonarr_bulk_search()
    assert sorted(p['seriesId'] for p in posted) == [2, 4]


def test_sonarr_bulk_search_refresh_failure_searches_nothing(monkeypatch):
    posted, saved = _sonarr_bulk_setup(monkeypatch, set())
    monkeypatch.setattr(target, 'refresh_and_confirm', lambda service, ids: None)
    target.sonarr_bulk_search()
    assert posted == [] and saved == []


class _R:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


def _refresh_env(monkeypatch, status, movies):
    posts, gets = [], []
    monkeypatch.setattr(target.time, 'sleep', lambda s: None)

    def fake_post(url, **kw):
        posts.append((url, kw['json']))
        return _R({'id': 42})

    def fake_get(url, **kw):
        gets.append(url)
        if url.endswith('/api/v3/command/42'):
            return _R({'status': status})
        return _R(movies[int(url.rsplit('/', 1)[1])])

    monkeypatch.setattr(target.requests, 'post', fake_post)
    monkeypatch.setattr(target.requests, 'get', fake_get)
    return posts, gets


def test_refresh_and_confirm_radarr_rechecks_after_refresh(monkeypatch):
    movies = {1: {'monitored': True, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': True}},
              2: {'monitored': True, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': False}}}
    posts, gets = _refresh_env(monkeypatch, 'completed', movies)
    assert target.refresh_and_confirm('radarr', [1, 2]) == [1]
    assert posts[0][1] == {'name': 'RefreshMovie', 'movieIds': [1, 2]}
    assert gets[0].endswith('/api/v3/command/42')  # waited for the refresh before re-checking


def test_refresh_and_confirm_failed_or_unfinished_refresh_is_none(monkeypatch):
    movies = {1: {'monitored': True, 'hasFile': True, 'movieFile': {'qualityCutoffNotMet': True}}}
    _refresh_env(monkeypatch, 'failed', movies)
    assert target.refresh_and_confirm('radarr', [1]) is None
    _refresh_env(monkeypatch, 'started', movies)
    monkeypatch.setattr(target, 'UPGRADE_REFRESH_WAIT_S', 0)
    assert target.refresh_and_confirm('radarr', [1]) is None
    assert target.refresh_and_confirm('radarr', []) == []


def test_refresh_and_confirm_sonarr_body(monkeypatch):
    posts, _ = _refresh_env(monkeypatch, 'completed', {})
    monkeypatch.setattr(target, '_sonarr_series_wants_search', lambda sid: sid == 5)
    assert target.refresh_and_confirm('sonarr', [5, 6]) == [5]
    assert posts[0][1] == {'name': 'RefreshSeries', 'seriesIds': [5, 6]}


def test_sonarr_series_wants_search(monkeypatch):
    def env(series, files):
        def fake_get(url, **kw):
            return _R(files if url.endswith('/episodefile') else series)
        monkeypatch.setattr(target.requests, 'get', fake_get)
    env({'monitored': True, 'statistics': {'episodeFileCount': 3, 'episodeCount': 4}}, [])
    assert target._sonarr_series_wants_search(1)  # a monitored episode is missing
    env({'monitored': True, 'statistics': {'episodeFileCount': 4, 'episodeCount': 4}},
        [{'qualityCutoffNotMet': False}, {'qualityCutoffNotMet': True}])
    assert target._sonarr_series_wants_search(1)
    env({'monitored': True, 'statistics': {'episodeFileCount': 4, 'episodeCount': 4}},
        [{'qualityCutoffNotMet': False}])
    assert not target._sonarr_series_wants_search(1)
    env({'monitored': False, 'statistics': {'episodeFileCount': 0, 'episodeCount': 4}}, [])
    assert not target._sonarr_series_wants_search(1)


def test_sonarr_wanted_series_ids_pages_both_lists(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        kind = url.rsplit('/', 1)[1]
        calls.append((kind, kw['params']['page']))
        if kind == 'cutoff':
            if kw['params']['page'] == 1:
                return _R({'totalRecords': 1500, 'records': [{'seriesId': 1}, {'seriesId': 2}]})
            return _R({'totalRecords': 1500, 'records': [{'seriesId': 3}]})
        return _R({'totalRecords': 1, 'records': [{'seriesId': 9}]})

    monkeypatch.setattr(target.requests, 'get', fake_get)
    assert target._sonarr_wanted_series_ids() == {1, 2, 3, 9}
    assert calls == [('cutoff', 1), ('cutoff', 2), ('missing', 1)]
