"""Tests for the Seerr webhook that syncs the home arrs' Sonarr/Radarr-type import lists at request time.
Synthetic data, fake HTTP; no network. Run: python -m pytest test_seerr_sync.py"""
import importlib.util
import os
import tempfile


def _load():
    tmp = tempfile.mkdtemp()
    os.environ['ACTIVITY_LOG'] = os.path.join(tmp, 'activity.jsonl')
    os.environ['ARR_HEALTH_STATE'] = os.path.join(tmp, 'arr_health_state.json')
    spec = importlib.util.spec_from_file_location(
        'arr_webhook_seerr', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
    aw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aw)
    aw.SONARR_API_KEY = aw.RADARR_API_KEY = 'k'
    aw.SONARR_URL, aw.RADARR_URL = 'http://sonarr.invalid', 'http://radarr.invalid'   # conftest gives both one URL
    return aw


class Resp:
    def __init__(self, body=None):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeRequests:
    def __init__(self, lists):
        self.lists, self.posts, self.gets = lists, [], []

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return Resp(self.lists[url.split('/api/')[0]])

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url.split('/api/')[0], json))
        return Resp({})


RADARR_LISTS = [
    {'id': 3, 'implementation': 'RadarrImport', 'enabled': True, 'enableAuto': True},
    {'id': 4, 'implementation': 'RadarrImport', 'enabled': False, 'enableAuto': True},
    {'id': 5, 'implementation': 'RadarrImport', 'enabled': True, 'enableAuto': False},
    {'id': 6, 'implementation': 'TraktListImport', 'enabled': True, 'enableAuto': True},
]
SONARR_LISTS = [
    {'id': 1, 'implementation': 'SonarrImport', 'enableAutomaticAdd': True},
    {'id': 2, 'implementation': 'SonarrImport', 'enableAutomaticAdd': True},
    {'id': 7, 'implementation': 'PlexImport', 'enableAutomaticAdd': True},
]


def _fake(aw):
    fr = FakeRequests({aw.RADARR_URL: RADARR_LISTS, aw.SONARR_URL: SONARR_LISTS})
    aw.requests = fr
    return fr


def test_only_enabled_auto_arr_lists_are_synced():
    aw = _load()
    fr = _fake(aw)
    assert aw.sync_arr_import_lists('Radarr') == 1
    assert aw.sync_arr_import_lists('Sonarr') == 2
    assert fr.posts == [(aw.RADARR_URL, {'name': 'ImportListSync', 'definitionId': 3}),
                        (aw.SONARR_URL, {'name': 'ImportListSync', 'definitionId': 1}),
                        (aw.SONARR_URL, {'name': 'ImportListSync', 'definitionId': 2})]


def test_no_key_means_no_calls():
    aw = _load()
    fr = _fake(aw)
    aw.RADARR_API_KEY = ''
    assert aw.sync_arr_import_lists('Radarr') == 0 and fr.gets == []


def test_job_syncs_at_each_time_and_survives_errors():
    aw = _load()
    fr = _fake(aw)
    slept = []
    calls = {'n': 0}
    real = aw.sync_arr_import_lists

    def flaky(who):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('down')
        return real(who)
    aw.sync_arr_import_lists = flaky
    aw._seerr_sync_job(['Radarr'], sleep=slept.append)
    assert slept == [20, 100]
    assert calls['n'] == 2 and len(fr.posts) == 1     # first pass raised, second synced


class FakeThread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        FakeThread.started.append((self.target.__name__, self.args))


def test_webhook_routes_by_media_type_and_ignores_other_events():
    aw = _load()
    FakeThread.started = []
    aw.threading.Thread = FakeThread
    try:
        c = aw.app.test_client()
        for body in ({'notification_type': 'MEDIA_AUTO_APPROVED', 'media': {'media_type': 'movie'}},
                     {'notification_type': 'MEDIA_APPROVED', 'media': {'media_type': 'tv'}},
                     {'notification_type': 'MEDIA_APPROVED'},
                     {'notification_type': 'MEDIA_PENDING', 'media': {'media_type': 'movie'}},
                     {'notification_type': 'TEST_NOTIFICATION'}):
            assert c.post('/webhook/seerr', json=body).status_code == 200
        assert c.post('/webhook/seerr', data='not json').status_code == 200
    finally:
        import threading
        aw.threading.Thread = threading.Thread
    assert FakeThread.started == [('_seerr_sync_job', (['Radarr'],)), ('_seerr_sync_job', (['Sonarr'],)),
                                  ('_seerr_sync_job', (['Sonarr', 'Radarr'],))]
