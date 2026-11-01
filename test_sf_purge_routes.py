"""Frozen tests: purge_stalled_upgrade_torrents and /purge-non-radarr report only non-deferred removals.
Run: python3 test_sf_purge_routes.py"""
import json
import os
import sys
import tempfile

import requests

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
os.environ['UNPACK_STATE_PATH'] = os.path.join(_TMP, 'unpack.json')
os.environ['SEED_STATE_PATH'] = os.path.join(_TMP, 'seed.json')
aw = __import__('arr-webhook')   # alias assigned only here

DAY = 86400
RPC = []
STATE = {'all': {}, 'one': {}, 'fail': False}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_post(self, url, json=None, timeout=None, **kw):
    """Fake Deluge. 'all' is the bulk listing, 'one' overrides the per-torrent status."""
    method, params = json['method'], json.get('params')
    RPC.append((method, params))
    if method == 'auth.login':
        return Resp({'result': True})
    if method == 'core.get_torrent_status':
        if STATE['fail']:
            raise requests.ConnectionError('deluge down')
        src = STATE['one'] if params[0] in STATE['one'] else STATE['all']
        return Resp({'result': dict(src.get(params[0], {}))})
    if method == 'core.get_torrents_status':
        flt = (params[0] or {}).get('id')
        return Resp({'result': {h: t for h, t in STATE['all'].items() if not flt or h in flt}})
    return Resp({'result': None})


requests.Session.post = fake_post


def setup():
    for k in ('REMOVAL_LOG', 'ACTIVITY_LOG', 'UNPACK_STATE_PATH', 'SEED_STATE_PATH'):
        if os.path.exists(os.environ[k]):
            os.remove(os.environ[k])
    del RPC[:]
    STATE['all'] = {}
    STATE['one'] = {}
    STATE['fail'] = False
    aw._snapshots.clear()


def _rows(key):
    try:
        with open(os.environ[key]) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def log_rows():
    return _rows('REMOVAL_LOG')


def acts():
    return [r['category'] for r in _rows('ACTIVITY_LOG')]


def summaries():
    return [r['summary'] for r in _rows('ACTIVITY_LOG')]


def removed_hashes():
    return [p[0] for m, p in RPC if m == 'core.remove_torrent']


def status_calls():
    return [p for m, p in RPC if m == 'core.get_torrent_status']


def full(seeding_time, **kw):
    info = {'name': 'Some.Show.S01E01.1080p', 'seeding_time': seeding_time, 'progress': 100.0,
            'total_uploaded': 5_000_000, 'is_finished': True, 'save_path': '/data/Downloads/Complete'}
    info.update(kw)
    return info


def young(**kw):
    return full(2 * DAY, **kw)


def never(**kw):
    return full(0, progress=0.0, total_uploaded=0, is_finished=False, **kw)


def read_json(key):
    try:
        with open(os.environ[key]) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _stalled(*hashes):
    return {h: {'name': h, 'label': aw.RADARR_UPG_LABEL, 'progress': 0.0, 'total_done': 0, 'state': 'Downloading'}
            for h in hashes}


def test_stalled_mixed():
    setup()
    STATE['all'] = _stalled('a', 'b', 'c')
    STATE['one'] = {'a': never(), 'b': young(), 'c': never()}
    aw.purge_stalled_upgrade_torrents()
    assert sorted(removed_hashes()) == ['a', 'c'], RPC
    assert any('Purged 2 stalled' in s for s in summaries()), summaries()


def test_stalled_all_deferred():
    setup()
    STATE['all'] = _stalled('a')
    STATE['one'] = {'a': young()}
    aw.purge_stalled_upgrade_torrents()
    assert removed_hashes() == [], RPC
    assert 'cleanup' not in acts(), acts()


def test_non_radarr_deferred():
    setup()
    STATE['all'] = {h: {'name': h, 'label': 'tv', 'progress': 100, 'state': 'Seeding', 'total_size': 1} for h in ('a', 'b', 'c')}
    STATE['one'] = {'a': full(30 * DAY), 'b': young(), 'c': young()}
    resp = aw.app.test_client().post('/purge-non-radarr?apply=1')
    body = resp.get_json()
    assert resp.status_code == 200 and body['removed'] == 1 and body['deferred'] == 2, body
    assert body['error_count'] == 0 and removed_hashes() == ['a'], (body, RPC)


TESTS = [
    test_stalled_mixed,
    test_stalled_all_deferred,
    test_non_radarr_deferred,
]

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
    print(f'{len(order) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
