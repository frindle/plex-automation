"""Frozen tests for the torrent-removal audit log helper (log_removal and
_rotate_removal_log in arr-webhook.py).

Run: python3 test_removal_audit_log.py
"""
import glob
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

aw = __import__('arr-webhook')   # alias assigned only here

TMP = tempfile.mkdtemp()


def setup():
    """Fresh, empty log location for every test; defaults restored."""
    d = tempfile.mkdtemp(dir=TMP)
    aw.REMOVAL_LOG = os.path.join(d, 'removal-audit.jsonl')
    aw.REMOVAL_LOG_MAX_BYTES = 5 * 1024 * 1024
    return aw.REMOVAL_LOG


def iso(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).isoformat(timespec='seconds')


def seed_active(path, days_ago, hash_='OLD'):
    with open(path, 'w') as f:
        f.write(json.dumps({'ts': iso(days_ago), 'fn': 'seed', 'hash': hash_}) + '\n')


def rotated(path):
    return sorted(glob.glob(path + '.*'))


def lines(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def test_appends_one_json_line_with_fields():
    p = setup()
    aw.log_removal('purge_stalled_upgrade_torrents', '1149CD84FB', 'cTurtle.Movie.2020', 'stalled',
                   {'radarr_id': 1176, 'fn': 'IGNORED', 'ts': 'IGNORED'})
    aw.log_removal('cleanup_radarr_queue_dupes', 'ABC')
    got = lines(p)
    assert len(got) == 2, got
    first = got[0]
    assert first['fn'] == 'purge_stalled_upgrade_torrents', first
    assert first['hash'] == '1149CD84FB', first
    assert first['name'] == 'cTurtle.Movie.2020', first
    assert first['reason'] == 'stalled', first
    assert first['radarr_id'] == 1176, first
    assert first['ts'] != 'IGNORED' and datetime.fromisoformat(first['ts']), first
    assert got[1]['fn'] == 'cleanup_radarr_queue_dupes' and got[1]['hash'] == 'ABC', got[1]
    assert rotated(p) == [], rotated(p)


def test_never_raises_into_callers():
    setup()
    aw.REMOVAL_LOG = os.path.join(TMP, 'no-such-dir', 'deeper', 'x.jsonl')
    r1 = aw.log_removal('f', 'H')
    assert r1 is None, r1
    aw.REMOVAL_LOG = TMP            # a directory, not a file
    r2 = aw.log_removal('f', 'H', 'n', 'r', {'obj': object()})
    assert r2 is None, r2
    p = setup()
    r3 = aw.log_removal('f', 'H', 'n', 'r', {'obj': object(), 'when': datetime.now()})
    assert r3 is None, r3
    assert len(lines(p)) == 1, 'an unserialisable extra value must not drop the entry'


def test_old_active_file_rotates_weekly():
    p = setup()
    seed_active(p, 8, 'OLD')
    aw.log_removal('f', 'NEW')
    rot = rotated(p)
    assert len(rot) == 1, rot
    assert [e['hash'] for e in lines(rot[0])] == ['OLD'], lines(rot[0])
    assert [e['hash'] for e in lines(p)] == ['NEW'], lines(p)


def test_recent_file_not_rotated_until_too_big():
    p = setup()
    seed_active(p, 2, 'OLD')
    aw.log_removal('f', 'NEW')
    assert rotated(p) == [], rotated(p)
    assert [e['hash'] for e in lines(p)] == ['OLD', 'NEW'], lines(p)
    aw.REMOVAL_LOG_MAX_BYTES = 10
    aw.log_removal('f', 'THIRD')
    assert len(rotated(p)) == 1, rotated(p)
    assert [e['hash'] for e in lines(p)] == ['THIRD'], lines(p)


def test_keeps_only_four_newest_rotated_files():
    p = setup()
    stamps = ['20260101T000000', '20260108T000000', '20260115T000000',
              '20260122T000000', '20260129T000000', '20260205T000000']
    for s in stamps:
        with open(p + '.' + s, 'w') as f:
            f.write(json.dumps({'ts': '2026-01-01T00:00:00', 'fn': 'x', 'hash': s}) + '\n')
    seed_active(p, 8, 'OLD')
    aw.log_removal('f', 'NEW')
    names = sorted(os.path.basename(x) for x in rotated(p))
    assert len(names) == 4, names
    kept_old = [n for n in names if n.split('.')[-1] in stamps]
    assert kept_old == ['removal-audit.jsonl.' + s for s in stamps[-3:]], kept_old
    assert [e['hash'] for e in lines(p)] == ['NEW'], lines(p)


TESTS = [
    test_appends_one_json_line_with_fields,
    test_never_raises_into_callers,
    test_old_active_file_rotates_weekly,
    test_recent_file_not_rotated_until_too_big,
    test_keeps_only_four_newest_rotated_files,
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
    print(f'{len(order)} tests, {failed} failed')
    sys.exit(1 if failed else 0)
