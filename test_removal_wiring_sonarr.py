"""Frozen tests: cleanup_sonarr_queue_dupes writes an audit entry whenever it asks Sonarr
to delete a duplicate FROM THE DOWNLOAD CLIENT (removeFromClient=True) and in no other case.
Run: python3 test_removal_wiring_sonarr.py"""
import os
import sys
import tempfile

import requests

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ['SONARR_URL'] = 'http://sonarr.test'
os.environ['SONARR_API_KEY'] = 'testkey'
aw = __import__('arr-webhook')   # alias assigned only here

CALLS = []       # audit entries recorded through log_removal
DELETES = []     # (sorted queue ids, removeFromClient) per Sonarr bulk DELETE
STATE = {'queue': [], 'torrents': {}, 'fail_ids': set()}


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
    return Resp({'records': []})


def fake_delete(url, headers=None, params=None, timeout=None, json=None, **kw):
    ids = sorted(json['ids'])
    if set(ids) & STATE['fail_ids']:
        raise requests.ConnectionError('queue record vanished')
    DELETES.append((ids, params['removeFromClient']))
    return Resp({})


def fake_log_removal(fn_name, torrent_hash, name=None, reason=None, extra=None):
    CALLS.append({'fn_name': fn_name, 'hash': torrent_hash, 'name': name, 'reason': reason, 'extra': extra or {}})


requests.Session.post = fake_session_post
requests.get = fake_get
requests.delete = fake_delete
aw.log_removal = fake_log_removal
aw.supersede_torrent = lambda h: None
aw.set_torrent_label = lambda h, label: None


def rec(qid, series_id, episode_id, dl, title, score):
    return {'id': qid, 'seriesId': series_id, 'episodeId': episode_id, 'downloadId': dl,
            'title': title, 'customFormatScore': score, 'size': 100, 'sizeleft': 100}


# Season pack EEE (3 episode records) keeps; the single episode FFF inside it is the loser.
PACK_AND_SINGLE = [
    rec(10, 7, 301, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
    rec(11, 7, 302, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
    rec(12, 7, 303, 'EEE', 'The.Show.S03.WEB-DL-RUDR', 100),
    rec(13, 7, 302, 'FFF', 'The.Show.S03E02.WEB-DL-RUDR.mkv', 500),
]


def setup(queue, torrents=None, fail_ids=()):
    del CALLS[:]
    del DELETES[:]
    STATE['queue'] = queue
    STATE['torrents'] = torrents or {}
    STATE['fail_ids'] = set(fail_ids)


def test_client_delete_is_logged_with_identifiers():
    setup(PACK_AND_SINGLE)
    aw.cleanup_sonarr_queue_dupes()
    assert DELETES == [([13], True)], DELETES
    assert len(CALLS) == 1, CALLS
    c = CALLS[0]
    assert c['fn_name'] == 'cleanup_sonarr_queue_dupes', c
    assert c['hash'] == 'fff' and c['name'] == 'The.Show.S03E02.WEB-DL-RUDR.mkv', c
    assert c['extra'].get('sonarr_series_id') == 7, c
    assert c['extra'].get('queue_ids') == [13], c
    assert isinstance(c['reason'], str) and c['reason'], c


def test_every_removed_duplicate_gets_its_own_entry():
    setup([rec(1, 21, 701, 'M1', 'A.Keep', 100), rec(2, 21, 701, 'M2', 'A.Drop', 10),
           rec(3, 22, 801, 'M3', 'B.Keep', 100), rec(4, 22, 801, 'M4', 'B.Drop', 10)])
    aw.cleanup_sonarr_queue_dupes()
    assert DELETES == [([2], True), ([4], True)], DELETES
    assert [(c['hash'], c['extra'].get('sonarr_series_id')) for c in CALLS] == [('m2', 21), ('m4', 22)], CALLS


def test_record_only_drop_for_a_torrent_already_in_deluge_is_not_a_removal():
    setup(PACK_AND_SINGLE,
          torrents={'fff': {'label': 'sonarr', 'progress': 100.0, 'state': 'Seeding', 'name': 'x'}})
    aw.cleanup_sonarr_queue_dupes()
    assert DELETES == [([13], False)], DELETES
    assert CALLS == [], CALLS


def test_dry_run_logs_nothing():
    setup(PACK_AND_SINGLE)
    aw.cleanup_sonarr_queue_dupes(dry_run=True)
    assert DELETES == [], DELETES
    assert CALLS == [], CALLS


def test_failed_delete_is_not_logged_and_does_not_stop_the_pass():
    setup([rec(1, 21, 701, 'M1', 'A.Keep', 100), rec(2, 21, 701, 'M2', 'A.Drop', 10),
           rec(3, 22, 801, 'M3', 'B.Keep', 100), rec(4, 22, 801, 'M4', 'B.Drop', 10)], fail_ids=[2])
    aw.cleanup_sonarr_queue_dupes()
    assert DELETES == [([4], True)], DELETES
    assert [c['hash'] for c in CALLS] == ['m4'], CALLS


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
