"""Frozen tests: cleanup_radarr_queue_dupes writes an audit entry whenever it asks Radarr
to delete a duplicate FROM THE DOWNLOAD CLIENT (removeFromClient=True) and in no other case.
Run: python3 test_removal_wiring_radarr.py"""
import json
import os
import sys
import tempfile

import requests

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
os.environ['RADARR_URL'] = 'http://radarr.test'
os.environ['RADARR_API_KEY'] = 'testkey'
aw = __import__('arr-webhook')   # alias assigned only here

def calls():
    """Audit entries written so far, read back from the audit file."""
    try:
        with open(os.environ['REMOVAL_LOG']) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return [{'fn_name': r.pop('fn'), 'hash': r.pop('hash'), 'name': r.pop('name'),
             'reason': r.pop('reason'), 'extra': r} for r in rows]
DELETES = []     # (queue id, removeFromClient) per Radarr DELETE
STATE = {'queue': [], 'torrents': {}, 'fail_ids': set(), 'history': {}}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_session_post(self, url, json=None, timeout=None, **kw):
    if json['method'] == 'core.get_torrents_status':
        return Resp({'result': STATE['torrents']})
    return Resp({'result': True})


def fake_get(url, headers=None, params=None, timeout=None, **kw):
    if url.endswith('/api/v3/queue'):
        return Resp({'records': STATE['queue']})
    if url.endswith('/api/v3/history'):
        return Resp({'records': STATE['history'].get((params or {}).get('downloadId'), [])})
    return Resp({'records': []})


def fake_delete(url, headers=None, params=None, timeout=None, **kw):
    qid = int(url.rsplit('/', 1)[1])
    if qid in STATE['fail_ids']:
        raise requests.ConnectionError('queue record vanished')
    DELETES.append((qid, params['removeFromClient']))
    return Resp({})


requests.Session.post = fake_session_post
requests.get = fake_get
requests.delete = fake_delete


def rec(qid, movie_id, dl, title, score):
    return {'id': qid, 'movieId': movie_id, 'downloadId': dl, 'title': title,
            'customFormatScore': score, 'size': 100, 'sizeleft': 100}


def setup(queue, torrents=None, fail_ids=(), history=None):
    if os.path.exists(os.environ['REMOVAL_LOG']):
        os.remove(os.environ['REMOVAL_LOG'])
    del DELETES[:]
    STATE['queue'] = queue
    STATE['torrents'] = torrents or {}
    STATE['fail_ids'] = set(fail_ids)
    STATE['history'] = history or {}


def test_client_delete_is_logged_with_identifiers():
    setup([rec(7, 9, 'CCC', 'Movie.Good', 100), rec(8, 9, 'DDD', 'Movie.Worse', 20)])
    aw.cleanup_radarr_queue_dupes()
    assert DELETES == [(8, True)], DELETES
    assert len(calls()) == 1, calls()
    c = calls()[0]
    assert c['fn_name'] == 'cleanup_radarr_queue_dupes', c
    assert c['hash'] == 'ddd' and c['name'] == 'Movie.Worse', c
    assert c['extra'].get('radarr_movie_id') == 9 and c['extra'].get('queue_id') == 8, c
    assert isinstance(c['reason'], str) and c['reason'], c


def test_every_removed_duplicate_gets_its_own_entry():
    setup([rec(1, 4, 'M1', 'A.Keep', 100), rec(2, 4, 'M2', 'A.Drop', 10),
           rec(3, 5, 'M3', 'B.Keep', 100), rec(4, 5, 'M4', 'B.Drop', 10)])
    aw.cleanup_radarr_queue_dupes()
    assert DELETES == [(2, True), (4, True)], DELETES
    assert [(c['hash'], c['extra'].get('radarr_movie_id')) for c in calls()] == [('m2', 4), ('m4', 5)], calls()


def test_record_only_drop_for_a_torrent_already_in_deluge_is_not_a_removal():
    setup([rec(7, 9, 'CCC', 'Movie.Good', 100), rec(8, 9, 'DDD', 'Movie.Worse', 20)],
          torrents={'ddd': {'label': 'radarr', 'progress': 100.0, 'state': 'Seeding', 'name': 'Movie.Worse'}},
          history={'CCC': [{'eventType': 'downloadFolderImported', 'downloadId': 'CCC', 'movieId': 9}]})
    aw.cleanup_radarr_queue_dupes()
    assert DELETES == [(8, False)], DELETES
    assert calls() == [], calls()


def test_dry_run_logs_nothing():
    setup([rec(7, 9, 'CCC', 'Movie.Good', 100), rec(8, 9, 'DDD', 'Movie.Worse', 20)])
    aw.cleanup_radarr_queue_dupes(dry_run=True)
    assert DELETES == [], DELETES
    assert calls() == [], calls()


def test_failed_delete_is_not_logged_and_does_not_stop_the_pass():
    setup([rec(1, 4, 'M1', 'A.Keep', 100), rec(2, 4, 'M2', 'A.Drop', 10),
           rec(3, 5, 'M3', 'B.Keep', 100), rec(4, 5, 'M4', 'B.Drop', 10)], fail_ids=[2])
    aw.cleanup_radarr_queue_dupes()
    assert DELETES == [(4, True)], DELETES
    assert [c['hash'] for c in calls()] == ['m4'], calls()


TESTS = [
    test_client_delete_is_logged_with_identifiers,
    test_every_removed_duplicate_gets_its_own_entry,
    test_record_only_drop_for_a_torrent_already_in_deluge_is_not_a_removal,
    test_dry_run_logs_nothing,
    test_failed_delete_is_not_logged_and_does_not_stop_the_pass,
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
