"""Frozen tests: read_removal_log reads the removal audit log (active + rotated files) and
GET /removal-log serves it read-only. Run: python3 test_removal_log_endpoint.py"""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'audit', 'removal-audit.jsonl')
aw = __import__('arr-webhook')   # alias assigned only here
aw.app.config['TESTING'] = True
CLIENT = aw.app.test_client()
ACTIVE = os.environ['REMOVAL_LOG']


def entry(ts, h, fn='remove_torrent'):
    return {'ts': ts, 'fn': fn, 'hash': h, 'name': f'name-{h}', 'reason': 'files deleted'}


def write(path, rows, raw=None):
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
        if raw:
            f.write(raw)


def setup(active=None, rotated=None, raw=None):
    shutil.rmtree(os.path.dirname(ACTIVE), ignore_errors=True)
    os.makedirs(os.path.dirname(ACTIVE))
    for suffix, rows in (rotated or {}).items():
        write(f'{ACTIVE}.{suffix}', rows)
    if active is not None:
        write(ACTIVE, active, raw)


ROTATED = {
    '20261001T080000': [entry('2026-10-01T08:00:00', 'a1'), entry('2026-10-02T09:00:00', 'a2')],
    '20261008T080000': [entry('2026-10-08T08:00:00', 'b1')],
}
ACTIVE_ROWS = [entry('2026-10-15T08:00:00', 'c1'), entry('2026-10-16T09:30:00', 'c2')]


def test_reads_rotated_then_active_oldest_first():
    setup(ACTIVE_ROWS, ROTATED)
    rows = aw.read_removal_log()
    assert [r['hash'] for r in rows] == ['a1', 'a2', 'b1', 'c1', 'c2'], rows


def test_limit_keeps_the_newest_entries():
    setup(ACTIVE_ROWS, ROTATED)
    assert [r['hash'] for r in aw.read_removal_log(limit=2)] == ['c1', 'c2']
    assert [r['hash'] for r in aw.read_removal_log(limit=4)] == ['a2', 'b1', 'c1', 'c2']


def test_since_filters_by_timestamp():
    setup(ACTIVE_ROWS, ROTATED)
    rows = aw.read_removal_log(since='2026-10-08T08:00:00')
    assert [r['hash'] for r in rows] == ['b1', 'c1', 'c2'], rows
    assert aw.read_removal_log(since='2027-01-01') == []


def test_missing_files_and_bad_lines_are_tolerated():
    setup(None, None)
    assert aw.read_removal_log() == []
    setup(ACTIVE_ROWS, ROTATED, raw='{not json\n\n')
    assert [r['hash'] for r in aw.read_removal_log()] == ['a1', 'a2', 'b1', 'c1', 'c2']


def test_endpoint_returns_entries_and_validates_limit():
    setup(ACTIVE_ROWS, ROTATED)
    r = CLIENT.get('/removal-log?since=2026-10-08T00:00:00&limit=2')
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body['ok'] is True and body['count'] == 2, body
    assert [e['hash'] for e in body['entries']] == ['c1', 'c2'], body
    assert CLIENT.get('/removal-log').get_json()['count'] == 5
    bad = CLIENT.get('/removal-log?limit=abc')
    assert bad.status_code == 400 and bad.get_json()['ok'] is False, bad.status_code
    assert CLIENT.post('/removal-log').status_code == 405


TESTS = [
    test_reads_rotated_then_active_oldest_first,
    test_limit_keeps_the_newest_entries,
    test_since_filters_by_timestamp,
    test_missing_files_and_bad_lines_are_tolerated,
    test_endpoint_returns_entries_and_validates_limit,
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
