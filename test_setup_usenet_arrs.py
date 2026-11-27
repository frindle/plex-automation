"""scripts/setup-usenet-arrs.py against an in-memory fake of the arr API: dry run by default, idempotent,
Prowlarr first, delay profile, path mapping, and no key ever printed."""
import copy
import importlib.util
import json
import os

import pytest

spec = importlib.util.spec_from_file_location('setup_usenet', os.path.join('scripts', 'setup-usenet-arrs.py'))
su = importlib.util.module_from_spec(spec)
spec.loader.exec_module(su)

SAB_KEY, ARR_KEY, IX_KEY = 'sabkey-0000', 'arrkey-1111', 'ixkey-2222'
ENV = {'RADARR_URL': 'http://radarr.invalid:7878', 'RADARR_API_KEY': ARR_KEY,
       'SONARR_URL': 'http://sonarr.invalid:8989', 'SONARR_API_KEY': ARR_KEY,
       'SABNZBD_URL': 'http://sab.invalid:8080', 'SABNZBD_API_KEY': SAB_KEY,
       'NEWZNAB_INDEXERS': json.dumps([{'name': 'ix-one', 'url': 'https://ix.invalid/api', 'apikey': IX_KEY}])}


PRIO_OPTIONS = [{'value': -100, 'name': 'Default'}, {'value': -2, 'name': 'Paused'}, {'value': 0, 'name': 'Normal'},
                {'value': 2, 'name': 'Force'}]


def schema_sab(cat_field):
    prio = ('recentMoviePriority', 'olderMoviePriority') if cat_field == 'movieCategory' \
        else ('recentTvPriority', 'olderTvPriority')
    return {'implementation': 'Sabnzbd', 'protocol': 'usenet', 'fields': [
        {'name': n, 'value': None} for n in ('host', 'port', 'useSsl', 'urlBase', 'apiKey', cat_field)] + [
        {'name': n, 'value': -100, 'selectOptions': PRIO_OPTIONS} for n in prio]}


SCHEMA_NEWZNAB = {'implementation': 'Newznab', 'protocol': 'usenet',
                  'fields': [{'name': n, 'value': None} for n in ('baseUrl', 'apiPath', 'apiKey', 'categories')]}


class FakeApi:
    def __init__(self, app, clients=None, indexers=None, profiles=None, maps=None, prefix='/api/v3'):
        cat = 'movieCategory' if app == 'Radarr' else 'tvCategory'
        self.d = {'/downloadclient': clients or [], '/remotepathmapping': maps or [],
                  '/rootfolder': [{'path': '/data/Media'}], '/indexer': indexers or [],
                  '/delayprofile': profiles if profiles is not None else [
                      {'id': 1, 'tags': [], 'preferredProtocol': 'torrent', 'usenetDelay': 120, 'torrentDelay': 30,
                       'enableUsenet': True, 'enableTorrent': True, 'order': 2147483647}]}
        self.schemas = {'/downloadclient/schema': [schema_sab(cat)], '/indexer/schema': [SCHEMA_NEWZNAB]}
        self.writes = []

    def get(self, path):
        return copy.deepcopy(self.schemas[path] if path in self.schemas else self.d[path])

    def post(self, path, body):
        self.writes.append(('POST', path, copy.deepcopy(body)))
        body = {**body, 'id': len(self.d.setdefault(path, [])) + 10}
        self.d[path].append(body)
        return body

    def put(self, path, body):
        self.writes.append(('PUT', path, copy.deepcopy(body)))
        base, _, i = path.rpartition('/')
        self.d[base] = [body if str(x.get('id')) == i else x for x in self.d[base]]
        return body


def run(env, apis, apply=False, capsys=None):
    made = []

    def make_api(url, key, prefix='/api/v3'):
        made.append(url)
        for k, a in apis.items():
            if url.startswith(f'http://{k.lower()}.invalid'):
                return a
        raise AssertionError(url)

    rc = su.main(['--apply'] if apply else [], env=env, make_api=make_api)
    return rc


@pytest.fixture
def apis():
    return {'Radarr': FakeApi('Radarr'), 'Sonarr': FakeApi('Sonarr')}


def test_dry_run_is_the_default_and_writes_nothing(apis, capsys):
    assert run(ENV, apis) == 0
    assert all(not a.writes for a in apis.values())
    out = capsys.readouterr().out
    assert 'dry run' in out and 'would add the SABnzbd download client' in out and 'would add the indexer' in out
    assert 'would set the default delay profile' in out


def test_apply_adds_client_indexer_and_delay_profile(apis, capsys):
    assert run(ENV, apis, apply=True) == 0
    for app, a in apis.items():
        kinds = [(m, p) for m, p, _ in a.writes]
        assert ('POST', '/downloadclient') in kinds and ('POST', '/indexer') in kinds and ('PUT', '/delayprofile/1') in kinds
        client = next(b for m, p, b in a.writes if p == '/downloadclient')
        f = su.fields_map(client)
        assert f['host'] == 'sab.invalid' and f['port'] == 8080 and f['useSsl'] is False and f['apiKey'] == SAB_KEY
        assert f['movieCategory' if app == 'Radarr' else 'tvCategory'] == app.lower()
        ix = next(b for m, p, b in a.writes if p == '/indexer')
        assert su.fields_map(ix)['baseUrl'] == 'https://ix.invalid' and su.fields_map(ix)['apiPath'] == '/api'
        assert ix['enableRss'] and ix['enableAutomaticSearch'] and ix['enableInteractiveSearch']
        prof = a.d['/delayprofile'][0]
        assert (prof['preferredProtocol'], prof['usenetDelay'], prof['torrentDelay']) == ('usenet', 0, 0)


def test_second_apply_changes_nothing(apis, capsys):
    run(ENV, apis, apply=True)
    for a in apis.values():
        a.writes.clear()
    capsys.readouterr()
    assert run(ENV, apis, apply=True) == 0
    assert all(not a.writes for a in apis.values())
    assert '0 change(s) made' in capsys.readouterr().out


def test_no_key_is_ever_printed(apis, capsys):
    run(ENV, apis, apply=True)
    run(ENV, apis, apply=False)
    out = capsys.readouterr().out
    for secret in (SAB_KEY, ARR_KEY, IX_KEY):
        assert secret not in out
    assert 'ix.invalid' not in out, 'indexer addresses stay out of the output too'


def test_api_repr_and_errors_hide_the_key(monkeypatch):
    a = su.Api('http://x.invalid', ARR_KEY)
    assert ARR_KEY not in repr(a)

    def boom(*args, **kw):
        raise OSError(f'failed for {ARR_KEY}')
    monkeypatch.setattr(su.urllib.request, 'urlopen', boom)
    with pytest.raises(su.ApiError) as e:
        a.get('/x')
    assert ARR_KEY not in str(e.value) and 'x.invalid' not in str(e.value)


def test_existing_client_with_a_wrong_category_is_updated(apis):
    existing = {**schema_sab('movieCategory'), 'id': 3, 'name': 'SABnzbd', 'enable': True}
    su.set_fields(existing, {'host': 'sab.invalid', 'port': 8080, 'useSsl': False, 'urlBase': '', 'movieCategory': 'old',
                             'apiKey': '********'})
    apis['Radarr'].d['/downloadclient'] = [existing]
    run(ENV, apis, apply=True)
    puts = [(m, p) for m, p, _ in apis['Radarr'].writes if p.startswith('/downloadclient')]
    assert puts == [('PUT', '/downloadclient/3')]


def test_prowlarr_configured_adds_the_indexer_there_only(apis, capsys):
    prow = FakeApi('Prowlarr')
    apis['Prowlarr'] = prow
    env = {**ENV, 'PROWLARR_URL': 'http://prowlarr.invalid:9696', 'PROWLARR_API_KEY': 'pk'}
    assert run(env, apis, apply=True) == 0
    assert [p for m, p, _ in prow.writes] == ['/indexer']
    assert all('/indexer' not in [p for m, p, _ in a.writes] for a in (apis['Radarr'], apis['Sonarr']))
    assert 'Prowlarr in use' in capsys.readouterr().out


def test_prowlarr_synced_indexers_stop_a_direct_add(apis, capsys):
    synced = {'implementation': 'Newznab', 'fields': [{'name': 'baseUrl', 'value': 'http://prowlarr.invalid:9696/1/'}]}
    apis['Radarr'].d['/indexer'] = [synced]
    run(ENV, apis, apply=True)
    assert '/indexer' not in [p for m, p, _ in apis['Radarr'].writes]
    assert 'looks_synced' in capsys.readouterr().out
    assert '/indexer' in [p for m, p, _ in apis['Sonarr'].writes], 'the other app has no Prowlarr: direct add'


def test_path_mapping_only_when_the_paths_differ(apis):
    run({**ENV, 'SABNZBD_COMPLETE_DIR': '/data/Downloads/usenet', 'SABNZBD_ARR_VISIBLE_DIR': '/data/Downloads/usenet'},
        apis, apply=True)
    assert '/remotepathmapping' not in [p for a in apis.values() for m, p, _ in a.writes]
    run({**ENV, 'SABNZBD_COMPLETE_DIR': '/downloads/complete', 'SABNZBD_ARR_VISIBLE_DIR': '/data/Downloads/usenet'},
        apis, apply=True)
    m = next(b for _, p, b in apis['Radarr'].writes if p == '/remotepathmapping')
    assert (m['host'], m['remotePath'], m['localPath']) == ('sab.invalid', '/downloads/complete/', '/data/Downloads/usenet/')
    n = len(apis['Radarr'].writes)
    run({**ENV, 'SABNZBD_COMPLETE_DIR': '/downloads/complete', 'SABNZBD_ARR_VISIBLE_DIR': '/data/Downloads/usenet'},
        apis, apply=True)
    assert len([1 for _, p, _ in apis['Radarr'].writes if p == '/remotepathmapping']) == 1 and len(apis['Radarr'].writes) >= n


def test_tagged_delay_profiles_are_left_alone(apis):
    apis['Radarr'].d['/delayprofile'] = [{'id': 2, 'tags': [5], 'preferredProtocol': 'torrent', 'usenetDelay': 99}]
    run(ENV, apis, apply=True)
    assert not [1 for m, p, _ in apis['Radarr'].writes if p.startswith('/delayprofile')]


def test_layout_is_reported_without_writes(apis, capsys):
    apis['Radarr'].d['/downloadclient'] = [{'implementation': 'Deluge', 'protocol': 'torrent', 'enable': True,
                                            'fields': [{'name': 'category', 'value': 'radarr'}]}]
    apis['Radarr'].d['/remotepathmapping'] = [{'remotePath': '/dl/', 'localPath': '/data/Downloads/'}]
    run(ENV, apis)
    out = capsys.readouterr().out
    assert "client: Deluge category='radarr'" in out and 'remote path mapping: /dl/ -> /data/Downloads/' in out
    assert 'root folder: /data/Media' in out


def test_missing_settings_skip_instead_of_failing(apis, capsys):
    env = {k: v for k, v in ENV.items() if not k.startswith('SABNZBD') and k != 'NEWZNAB_INDEXERS'}
    assert run(env, apis) == 0
    out = capsys.readouterr().out
    assert 'download client skipped' in out and 'indexers skipped' in out


def test_client_is_matched_by_host_not_the_first_sabnzbd_type(apis):
    other = {**schema_sab('movieCategory'), 'id': 7, 'name': 'AltMount', 'enable': True}
    su.set_fields(other, {'host': 'altmount.invalid', 'port': 8080, 'useSsl': False, 'urlBase': '', 'movieCategory': 'radarr',
                          'apiKey': '********'})
    apis['Radarr'].d['/downloadclient'] = [other]
    run(ENV, apis, apply=True)
    w = [(m, p) for m, p, _ in apis['Radarr'].writes if p.startswith('/downloadclient')]
    assert w == [('POST', '/downloadclient')], 'the other SABnzbd-type client is left alone and a new one added'
    assert apis['Radarr'].d['/downloadclient'][0]['id'] == 7


def test_generic_newznab_schema_is_chosen_by_name(apis):
    preset = {'implementation': 'Newznab', 'name': 'SomePreset', 'fields': [{'name': 'baseUrl', 'value': 'https://preset.invalid'},
                                                                           {'name': 'apiPath', 'value': '/x'}, {'name': 'apiKey', 'value': None}]}
    generic = {**SCHEMA_NEWZNAB, 'name': 'Newznab'}
    apis['Radarr'].schemas['/indexer/schema'] = [preset, generic]
    run(ENV, apis, apply=True)
    ix = next(b for m, p, b in apis['Radarr'].writes if p == '/indexer')
    assert ix['name'] != 'SomePreset' and su.fields_map(ix)['baseUrl'] == 'https://ix.invalid'


# -- the watch hold's priority: every grab arrives Paused ----------------------------------------------------

HOLD_ENV = {**ENV, 'WATCH_HOLD': 'on'}
PRIOS = {'Radarr': ('recentMoviePriority', 'olderMoviePriority'), 'Sonarr': ('recentTvPriority', 'olderTvPriority')}


def test_hold_on_sets_recent_and_older_priority_to_paused_from_the_schema_options(apis):
    assert run(HOLD_ENV, apis, apply=True) == 0
    for app, a in apis.items():
        f = su.fields_map(next(b for m, p, b in a.writes if p == '/downloadclient'))
        assert [f[n] for n in PRIOS[app]] == [-2, -2]


def test_hold_on_uses_the_paused_value_the_app_lists(apis):
    for a in apis.values():
        for f in a.schemas['/downloadclient/schema'][0]['fields']:
            for o in f.get('selectOptions') or []:
                if o['name'] == 'Paused':
                    o['value'] = -7
    run(HOLD_ENV, apis, apply=True)
    f = su.fields_map(next(b for m, p, b in apis['Radarr'].writes if p == '/downloadclient'))
    assert f['recentMoviePriority'] == -7


def test_hold_on_second_apply_changes_nothing_and_an_existing_client_is_updated(apis):
    run(ENV, apis, apply=True)                              # a client without the hold exists already
    for a in apis.values():
        a.writes.clear()
    run(HOLD_ENV, apis, apply=True)
    assert [(m, p) for m, p, _ in apis['Radarr'].writes] == [('PUT', '/downloadclient/10')]
    for a in apis.values():
        a.writes.clear()
    run(HOLD_ENV, apis, apply=True)
    assert all(not a.writes for a in apis.values())


def test_hold_not_on_leaves_the_priority_alone_and_puts_a_stale_pause_back(apis, capsys):
    run(ENV, apis, apply=True)
    f = su.fields_map(next(b for m, p, b in apis['Radarr'].writes if p == '/downloadclient'))
    assert f['recentMoviePriority'] == -100
    run(HOLD_ENV, apis, apply=True)
    for a in apis.values():
        a.writes.clear()
    capsys.readouterr()
    run(ENV, apis, apply=True)
    client = apis['Radarr'].d['/downloadclient'][0]
    assert su.fields_map(client)['recentMoviePriority'] == -100 and su.fields_map(client)['olderMoviePriority'] == -100
    assert 'WATCH_HOLD is not on' in capsys.readouterr().out


def test_dry_run_with_the_hold_writes_nothing(apis):
    run(HOLD_ENV, apis, apply=False)
    assert all(not a.writes for a in apis.values())


def test_an_app_without_priority_fields_is_skipped_quietly(apis):
    for a in apis.values():
        sch = a.schemas['/downloadclient/schema'][0]
        sch['fields'] = [f for f in sch['fields'] if 'Priority' not in f['name']]
    assert run(HOLD_ENV, apis, apply=True) == 0
    assert any(p == '/downloadclient' for m, p, _ in apis['Radarr'].writes)
