"""Frozen tests: a Queued superseded torrent is purged only if it never started or
its seed obligation is met (progress is not a criterion; Deluge reports 0% for
Queued torrents that seeded earlier), purge_queued_superseded refuses batches
over 5, and its removal-log entries carry the torrent name and seeding_time_s.
Run: python3 test_purge_guard.py"""
import json
import os
import sys
import tempfile

import requests

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
aw = __import__('arr-webhook')   # alias assigned only here

RPC = []        # (method, params) sent to the fake Deluge
STATE = {'fail_hashes': set()}
DAY = 86400


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
    if method == 'core.remove_torrent' and params[0] in STATE['fail_hashes']:
        raise requests.ConnectionError('deluge down')
    if method == 'core.get_torrent_status':
        return Resp({'result': {'save_path': '/data/Downloads/Complete', 'seeding_time': 0}})
    return Resp({'result': None})


requests.Session.post = fake_post


def setup():
    if os.path.exists(os.environ['REMOVAL_LOG']):
        os.remove(os.environ['REMOVAL_LOG'])
    del RPC[:]
    STATE['fail_hashes'] = set()


def removed_hashes():
    return [p[0] for m, p in RPC if m == 'core.remove_torrent']


def log_rows():
    try:
        with open(os.environ['REMOVAL_LOG']) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def torrent(name, **kw):
    info = {'label': aw.SUPERSEDED_LABEL, 'state': 'Queued', 'name': name, 'progress': 0,
            'seeding_time': 0, 'active_time': 0, 'total_uploaded': 0, 'is_finished': False}
    info.update(kw)
    return info


def hashes(torrents):
    return [t['hash'] for t in aw.queued_superseded_targets(torrents)]


def test_never_started_queued_torrent_is_purged():
    assert hashes({'a': torrent('a')}) == ['a']


def test_queued_zero_progress_but_seeded_earlier_is_not_purged():
    torrents = {
        'seeded': torrent('seeded', seeding_time=3 * DAY),
        'active': torrent('active', active_time=7200),
        'uploaded': torrent('uploaded', total_uploaded=5_000_000),
        'finished': torrent('finished', is_finished=True),
    }
    assert hashes(torrents) == [], hashes(torrents)


def test_seed_window_met_is_purged_even_with_history():
    torrents = {
        'done': torrent('done', seeding_time=aw.SEED_DAYS * DAY, active_time=99, total_uploaded=1, is_finished=True),
        'short': torrent('short', seeding_time=aw.SEED_DAYS * DAY - 1, active_time=99, total_uploaded=1),
    }
    assert hashes(torrents) == ['done'], hashes(torrents)


def test_progress_is_not_a_criterion_and_other_filters_hold():
    torrents = {
        'p100': torrent('p100', progress=100),                       # never started, any progress: eligible
        'notqueued': torrent('notqueued', state='Seeding'),
        'otherlabel': torrent('otherlabel', label='radarr'),
        'miss': {'label': aw.SUPERSEDED_LABEL, 'state': 'Queued', 'name': 'miss'},   # fields absent = never started
    }
    assert hashes(torrents) == ['p100', 'miss'], hashes(torrents)


def test_target_shape_keeps_existing_keys_and_adds_seeding_time():
    t = aw.queued_superseded_targets({'a': torrent('a', progress=0)})[0]
    for k in ('hash', 'name', 'progress', 'state'):
        assert k in t, t
    assert t['hash'] == 'a' and t['name'] == 'a' and t['state'] == 'Queued', t


def test_batch_of_six_removes_nothing():
    setup()
    targets = [{'hash': f'h{i}', 'name': f'n{i}', 'progress': 0, 'state': 'Queued', 'seeding_time': 0} for i in range(6)]
    result = aw.purge_queued_superseded(targets)
    assert tuple(result) == (0, []), result
    assert removed_hashes() == [], RPC
    assert log_rows() == [], log_rows()


def test_batch_of_five_is_removed():
    setup()
    targets = [{'hash': f'h{i}', 'name': f'n{i}', 'progress': 0, 'state': 'Queued', 'seeding_time': 0} for i in range(5)]
    removed, failed = aw.purge_queued_superseded(targets)
    assert removed == 5 and failed == [], (removed, failed)
    assert removed_hashes() == [f'h{i}' for i in range(5)], RPC


def test_failure_in_batch_is_reported_and_others_continue():
    setup()
    STATE['fail_hashes'] = {'h1'}
    targets = [{'hash': f'h{i}', 'name': f'n{i}', 'progress': 0, 'state': 'Queued', 'seeding_time': 0} for i in range(3)]
    removed, failed = aw.purge_queued_superseded(targets)
    assert removed == 2, (removed, failed)
    assert len(failed) == 1 and failed[0]['hash'] == 'h1' and failed[0]['error'], failed


def test_removal_log_has_name_and_seeding_time_s():
    setup()
    targets = [
        {'hash': 'aaa', 'name': 'Some.Movie.2020.1080p', 'progress': 0, 'state': 'Queued', 'seeding_time': 0},
        {'hash': 'bbb', 'name': 'Old.Show.S01', 'progress': 0, 'state': 'Queued', 'seeding_time': aw.SEED_DAYS * DAY + 5},
    ]
    removed, failed = aw.purge_queued_superseded(targets)
    assert removed == 2 and failed == [], (removed, failed)
    rows = {r['hash']: r for r in log_rows()}
    assert set(rows) == {'aaa', 'bbb'}, rows
    assert rows['aaa']['name'] == 'Some.Movie.2020.1080p', rows['aaa']
    assert rows['aaa']['seeding_time_s'] == 0, rows['aaa']
    assert rows['bbb']['name'] == 'Old.Show.S01', rows['bbb']
    assert rows['bbb']['seeding_time_s'] == aw.SEED_DAYS * DAY + 5, rows['bbb']
    for r in rows.values():                      # existing entry fields are unchanged
        assert r['fn'] == 'purge_queued_superseded' and 'ts' in r and 'reason' in r and 'remove_data' in r, r


TESTS = [
    test_never_started_queued_torrent_is_purged,
    test_queued_zero_progress_but_seeded_earlier_is_not_purged,
    test_seed_window_met_is_purged_even_with_history,
    test_progress_is_not_a_criterion_and_other_filters_hold,
    test_target_shape_keeps_existing_keys_and_adds_seeding_time,
    test_batch_of_six_removes_nothing,
    test_batch_of_five_is_removed,
    test_failure_in_batch_is_reported_and_others_continue,
    test_removal_log_has_name_and_seeding_time_s,
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
