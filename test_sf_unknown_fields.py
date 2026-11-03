"""remove_torrent's seed floor with missing or None status fields: a field nobody knows is never read as 0, a
missing seed time is fetched, and a None from the caller does not hide the value Deluge reports.
Run: python3 test_sf_unknown_fields.py"""
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
aw = __import__('arr-webhook')

DAY = 86400
RPC = []
STATE = {'one': {}, 'fail': False}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_post(self, url, json=None, timeout=None, **kw):
    """Fake Deluge: 'one' is the per-torrent status, 'fail' makes the status fetch raise."""
    method, params = json['method'], json.get('params')
    RPC.append((method, params))
    if method == 'auth.login':
        return Resp({'result': True})
    if method == 'core.get_torrent_status':
        if STATE['fail']:
            raise requests.ConnectionError('deluge down')
        return Resp({'result': dict(STATE['one'].get(params[0], {}))})
    return Resp({'result': None})


requests.Session.post = fake_post


def setup():
    for k in ('REMOVAL_LOG', 'ACTIVITY_LOG'):
        if os.path.exists(os.environ[k]):
            os.remove(os.environ[k])
    del RPC[:]
    STATE['one'] = {}
    STATE['fail'] = False
    aw._snapshots.clear()


def _rows(key):
    try:
        with open(os.environ[key]) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def removed_hashes():
    return [p[0] for m, p in RPC if m == 'core.remove_torrent']


def status_calls():
    return [p for m, p in RPC if m == 'core.get_torrent_status']


def summaries():
    return [r['summary'] for r in _rows('ACTIVITY_LOG') if r['category'] == 'seed-floor']


def full(seeding_time, **kw):
    info = {'name': 'Some.Show.S01E01.1080p', 'seeding_time': seeding_time, 'progress': 100.0,
            'total_uploaded': 5_000_000, 'is_finished': True, 'save_path': '/data/Downloads/Complete'}
    info.update(kw)
    return info


def test_missing_seed_time_is_fetched():
    # The caller knows progress, upload and finished state but not the seed time: ask Deluge, do not defer forever.
    setup()
    STATE['one']['H1'] = full(30 * DAY)
    info = full(None)
    del info['seeding_time']
    assert aw.remove_torrent('H1', remove_data=False, info=info) is True
    assert len(status_calls()) == 1 and removed_hashes() == ['H1'], RPC
    assert _rows('REMOVAL_LOG')[-1]['seeding_days'] == 30.0, _rows('REMOVAL_LOG')


def test_missing_upload_count_is_not_never_seeded():
    # Seed time 0 and unfinished, but Deluge does not report the upload count: it may have uploaded, keep it.
    setup()
    status = full(0, progress=40.0, is_finished=False)
    del status['total_uploaded']
    STATE['one']['H2'] = status
    assert aw.remove_torrent('H2', remove_data=False) == aw.DEFERRED
    assert removed_hashes() == [], RPC
    setup()
    STATE['fail'] = True
    partial = full(0, progress=40.0, is_finished=False)
    del partial['total_uploaded']
    assert aw.remove_torrent('H2', remove_data=False, info=partial) == aw.DEFERRED
    assert removed_hashes() == [], RPC


def test_none_progress_defers_without_crashing():
    setup()
    STATE['fail'] = True
    info = full(0, progress=None, total_uploaded=0, is_finished=False)
    for rd in (False, True):
        assert aw.remove_torrent('H3', remove_data=rd, info=info) == aw.DEFERRED
    assert removed_hashes() == [], RPC


def test_caller_none_does_not_hide_deluge_value():
    # A None from the caller is unknown: Deluge's real progress (never-seeded partial) is used, so it may go.
    setup()
    STATE['one']['H4'] = full(0, name='From.Deluge', progress=12.0, total_uploaded=0, is_finished=False)
    assert aw.remove_torrent('H4', remove_data=False, info={'name': None, 'seeding_time': 0, 'progress': None}) is True
    assert removed_hashes() == ['H4'], RPC
    assert _rows('REMOVAL_LOG')[-1]['name'] == 'From.Deluge', _rows('REMOVAL_LOG')


def test_deferral_message_says_unknown():
    setup()
    STATE['one']['H5'] = {'name': 'No.Seed.Time'}
    assert aw.remove_torrent('H5', remove_data=False) == aw.DEFERRED
    assert summaries() and 'unknown' in summaries()[-1] and 'None' not in summaries()[-1], summaries()
    setup()
    assert aw.remove_torrent('H6', remove_data=False, info=full(5 * DAY)) == aw.DEFERRED
    assert '5.0d' in summaries()[-1], summaries()


TESTS = [
    test_missing_seed_time_is_fetched,
    test_missing_upload_count_is_not_never_seeded,
    test_none_progress_defers_without_crashing,
    test_caller_none_does_not_hide_deluge_value,
    test_deferral_message_says_unknown,
]

if __name__ == '__main__':
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f'RESULT {t.__name__} PASS')
        except Exception as e:
            failed += 1
            print(f'RESULT {t.__name__} FAIL {type(e).__name__}: {e}')
    print(f'{len(TESTS) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
