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


def make_targets(n):
    return [{'hash': f'h{i}', 'name': f'n{i}', 'progress': 0, 'state': 'Queued', 'seeding_time': 0}
            for i in range(n)]


def test_never_started_queued_torrent_is_purged_and_filters_hold():
    torrents = {
        'a': torrent('a'),
        'p100': torrent('p100', progress=100),                       # progress is not a criterion
        'notqueued': torrent('notqueued', state='Seeding'),
        'otherlabel': torrent('otherlabel', label='radarr'),
        'miss': {'label': aw.SUPERSEDED_LABEL, 'state': 'Queued', 'name': 'miss'},   # absent fields = never started
    }
    targets = aw.queued_superseded_targets(torrents)
    assert [t['hash'] for t in targets] == ['a', 'p100', 'miss'], targets
    first = targets[0]
    assert first['name'] == 'a' and first['state'] == 'Queued' and 'progress' in first, first
    assert first['seeding_time'] == 0, first


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
    targets = aw.queued_superseded_targets(torrents)
    assert [t['hash'] for t in targets] == ['done'], targets
    assert targets[0]['seeding_time'] == aw.SEED_DAYS * DAY, targets


def test_batch_over_five_removes_nothing_and_five_is_removed():
    setup()
    result = aw.purge_queued_superseded(make_targets(6))
    assert tuple(result) == (0, []), result
    assert removed_hashes() == [], RPC
    assert log_rows() == [], log_rows()
    removed, failed = aw.purge_queued_superseded(make_targets(5))
    assert removed == 5 and failed == [], (removed, failed)
    assert removed_hashes() == [f'h{i}' for i in range(5)], RPC
    setup()
    STATE['fail_hashes'] = {'h1'}
    removed, failed = aw.purge_queued_superseded(make_targets(3))
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
    test_never_started_queued_torrent_is_purged_and_filters_hold,
    test_queued_zero_progress_but_seeded_earlier_is_not_purged,
    test_seed_window_met_is_purged_even_with_history,
    test_batch_over_five_removes_nothing_and_five_is_removed,
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
