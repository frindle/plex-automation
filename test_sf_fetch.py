"""Frozen tests: remove_torrent fetches the torrent status whenever seed time (or progress/uploaded/finished) is unknown, even for remove_data=False, and its removal-log row carries name and seeding_days.
Run: python3 test_sf_fetch.py"""
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


def test_fetch_when_unknown():
    setup()
    STATE['one']['H1'] = full(30 * DAY, name='Fetched.Name.S01')
    aw.remove_torrent('H1', remove_data=False)
    calls = status_calls()
    assert len(calls) == 1 and calls[0][0] == 'H1', RPC
    assert {'name', 'seeding_time', 'save_path', 'progress', 'total_uploaded', 'is_finished'} <= set(calls[0][1]), calls
    assert removed_hashes() == ['H1'], RPC
    row = log_rows()[-1]
    assert row.get('name') == 'Fetched.Name.S01' and row.get('seeding_time_s') == 30 * DAY and row.get('seeding_days') == 30.0, row


def test_complete_info_no_fetch():
    setup()
    aw.remove_torrent('H2', remove_data=False, info=full(25 * DAY))
    assert status_calls() == [], RPC
    assert removed_hashes() == ['H2'], RPC
    row = log_rows()[-1]
    assert row.get('name') == 'Some.Show.S01E01.1080p' and row.get('seeding_days') == 25.0, row


def test_partial_info_fetches():
    setup()
    STATE['one']['H3'] = full(50 * DAY, name='From.Deluge')
    aw.remove_torrent('H3', remove_data=False, info={'name': 'From.Caller', 'seeding_time': 40 * DAY})
    assert status_calls() == [], 'seed time met: no fetch needed'
    setup()
    STATE['one']['H3'] = never(name='From.Deluge')
    aw.remove_torrent('H3', remove_data=False, info={'name': 'From.Caller', 'seeding_time': 0})
    assert len(status_calls()) == 1, RPC
    assert log_rows()[-1]['name'] == 'From.Caller', log_rows()


def test_data_removal_logged():
    setup()
    aw.remove_torrent('H4', remove_data=True, info=full(33 * DAY))
    assert removed_hashes() == ['H4'], RPC
    row = log_rows()[-1]
    assert row.get('remove_data') is True and row.get('seeding_days') == 33.0, row
    assert row.get('name') == 'Some.Show.S01E01.1080p', row
    setup()
    STATE['all']['H5'] = {'save_path': '/data/Media/Movies'}
    info = full(33 * DAY)
    del info['save_path']
    aw.remove_torrent('H5', remove_data=True, info=info)
    assert log_rows()[-1].get('remove_data') is False, 'library path kept'
    setup()
    STATE['one']['H6'] = full(31 * DAY, name='Data.Fetched')
    aw.remove_torrent('H6')
    row = log_rows()[-1]
    assert row.get('name') == 'Data.Fetched' and row.get('seeding_days') == 31.0, 'data fetch'
    assert {'name', 'progress', 'total_uploaded', 'is_finished'} <= set(status_calls()[0][1]), 'fields'


TESTS = [
    test_fetch_when_unknown,
    test_complete_info_no_fetch,
    test_partial_info_fetches,
    test_data_removal_logged,
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
