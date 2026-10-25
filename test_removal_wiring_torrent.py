"""Frozen tests: remove_torrent writes one audit entry per torrent actually removed,
attributed to the calling function. Run: python3 test_removal_wiring_torrent.py"""
import os
import sys
import tempfile

import requests

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
aw = __import__('arr-webhook')   # alias assigned only here

CALLS = []      # audit entries recorded through log_removal
RPC = []        # (method, params) sent to the fake Deluge
STATE = {'status': {}, 'fail_remove': False}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_post(self, url, json=None, timeout=None, **kw):
    method, params = json['method'], json.get('params')
    RPC.append((method, params))
    if method == 'core.remove_torrent' and STATE['fail_remove']:
        raise requests.ConnectionError('deluge down')
    if method == 'core.get_torrent_status':
        return Resp({'result': dict(STATE['status'])})
    return Resp({'result': None})


def fake_log_removal(fn_name, torrent_hash, name=None, reason=None, extra=None):
    CALLS.append({'fn_name': fn_name, 'hash': torrent_hash, 'name': name, 'reason': reason, 'extra': extra or {}})


requests.Session.post = fake_post
aw.log_removal = fake_log_removal


def setup(status=None, fail_remove=False):
    del CALLS[:]
    del RPC[:]
    STATE['status'] = status or {}
    STATE['fail_remove'] = fail_remove


def cleanup_probe(*args, **kwargs):
    """Stands in for a real caller such as cleanup_superseded."""
    return aw.remove_torrent(*args, **kwargs)


OLD_ENOUGH = 60 * 86400
DOWNLOADS = '/data/Downloads/Complete'


def test_logs_removal_with_caller_name_hash_and_reason():
    setup({'save_path': DOWNLOADS, 'seeding_time': OLD_ENOUGH})
    cleanup_probe('HASH1')
    assert [m for m, p in RPC if m == 'core.remove_torrent'] == ['core.remove_torrent'], RPC
    assert len(CALLS) == 1, CALLS
    c = CALLS[0]
    assert c['fn_name'] == 'cleanup_probe', c
    assert c['hash'] == 'HASH1', c
    assert c['extra'].get('remove_data') is True, c
    assert isinstance(c['reason'], str) and c['reason'], c


def test_seed_obligation_downgrade_is_logged_as_files_kept():
    setup({'save_path': DOWNLOADS, 'seeding_time': 3600})
    cleanup_probe('HASH2')
    assert len(CALLS) == 1, CALLS
    assert CALLS[0]['extra'].get('remove_data') is False, CALLS[0]
    assert CALLS[0]['hash'] == 'HASH2', CALLS[0]


def test_name_comes_from_caller_supplied_info():
    setup()
    info = {'name': 'Some.Movie.2020.1080p', 'save_path': DOWNLOADS, 'seeding_time': OLD_ENOUGH}
    cleanup_probe('HASH3', info=info)
    assert len(CALLS) == 1, CALLS
    assert CALLS[0]['name'] == 'Some.Movie.2020.1080p', CALLS[0]
    setup({'save_path': DOWNLOADS, 'seeding_time': OLD_ENOUGH})
    cleanup_probe('HASH4')
    assert CALLS[0]['name'] is None, CALLS[0]


def test_remove_data_false_is_logged_without_status_fetch():
    setup()
    cleanup_probe('HASH5', remove_data=False)
    assert [m for m, p in RPC if m == 'core.get_torrent_status'] == [], RPC
    assert len(CALLS) == 1, CALLS
    assert CALLS[0]['hash'] == 'HASH5' and CALLS[0]['extra'].get('remove_data') is False, CALLS[0]


def test_failed_removal_is_not_logged():
    setup({'save_path': DOWNLOADS, 'seeding_time': OLD_ENOUGH}, fail_remove=True)
    raised = False
    try:
        cleanup_probe('HASH6')
    except requests.ConnectionError:
        raised = True
    assert raised, 'the Deluge failure must still reach the caller'
    assert CALLS == [], CALLS


TESTS = [
    test_logs_removal_with_caller_name_hash_and_reason,
    test_seed_obligation_downgrade_is_logged_as_files_kept,
    test_name_comes_from_caller_supplied_info,
    test_remove_data_false_is_logged_without_status_fetch,
    test_failed_removal_is_not_logged,
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
