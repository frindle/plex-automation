"""Frozen tests: a duplicate-pass loser may be relabeled 'superseded' only after
Radarr history shows an import of the KEEPER's downloadId (dedup_via_radarr,
cleanup_radarr_queue_dupes, _keeper_import_confirmed in arr-webhook.py).

Radarr and Deluge are faked at the HTTP boundary (requests.get / requests.delete /
requests.Session.post), so the real supersede_torrent runs; a relabel shows up as
a Deluge `label.set_torrent` call carrying the 'superseded' label.

Run: python3 test_supersede_after_import.py
"""
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

IMPORTED = {'eventType': 'downloadFolderImported', 'date': '2026-10-24T00:00:00Z'}
STATE = {}
DELETES = []      # (queue id, removeFromClient)
SUPERSEDED = []   # hashes that got the 'superseded' Deluge label
DELUGE_FIELDS = {'name': '', 'label': '', 'save_path': '/downloads/radarr', 'seeding_time': 0,
                 'progress': 0.0, 'state': 'Downloading', 'total_uploaded': 0, 'total_seeds': 0,
                 'tracker_status': ''}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_session_post(self, url, json=None, timeout=None, **kw):
    method, params = json['method'], json.get('params') or []
    if method == 'auth.login':
        return Resp({'result': True})
    if method == 'label.get_labels':
        return Resp({'result': ['superseded', 'radarr', 'radarr-upgrade']})
    if method == 'core.get_torrents_status':
        flt = params[0] if params else {}
        wanted = flt.get('id') if isinstance(flt, dict) else None
        out = {h: dict(DELUGE_FIELDS, **t) for h, t in STATE['torrents'].items()
               if wanted is None or h in wanted}
        return Resp({'result': out})
    if method == 'label.set_torrent' and params[1] == 'superseded':
        SUPERSEDED.append(params[0])
    return Resp({'result': True})


def fake_get(url, headers=None, params=None, timeout=None, **kw):
    if url.endswith('/api/v3/queue'):
        return Resp({'records': STATE['queue']})
    if url.endswith('/api/v3/movie'):
        return Resp(STATE['movies'])
    if '/api/v3/movie/' in url:
        return Resp({'title': 'Spectre', 'originalTitle': 'Spectre'})
    if url.endswith('/api/v3/history'):
        dl = (params or {}).get('downloadId', '').lower()
        if dl in STATE['history_error']:
            raise requests.ConnectionError('radarr history down')
        return Resp({'records': STATE['history'].get(dl, [])})
    raise AssertionError(f'unexpected GET {url}')


def fake_delete(url, headers=None, params=None, timeout=None, **kw):
    DELETES.append((int(url.rsplit('/', 1)[1]), (params or {}).get('removeFromClient')))
    return Resp({})


requests.Session.post = fake_session_post
requests.get = fake_get
requests.delete = fake_delete


def setup(queue=None, torrents=None, history=None, movies=None, history_error=()):
    del DELETES[:]
    del SUPERSEDED[:]
    STATE.update(queue=queue or [], torrents=torrents or {}, history=history or {},
                 movies=movies or [], history_error=set(history_error))


def grab(movie_id, score, date='2026-10-20T00:00:00Z'):
    return {'eventType': 'grabbed', 'date': date, 'movieId': movie_id,
            'data': {'customFormatScore': score}}


def lane_setup(imported, history_error=()):
    """Movie 42: the keeper 'keephash' is in Radarr's queue (score 100, still downloading);
    'losehash' is a lower-scoring duplicate sitting in the throttled radarr-upgrade lane."""
    queue = [{'id': 7, 'movieId': 42, 'downloadId': 'KEEPHASH', 'title': 'Spectre.2015.2160p.4K4U',
              'customFormatScore': 100, 'size': 100, 'sizeleft': 90}]
    torrents = {
        'keephash': {'name': 'Spectre.2015.2160p.WEB-DL.4K4U.mkv', 'label': 'radarr',
                     'progress': 10.0, 'state': 'Downloading'},
        'losehash': {'name': 'Spectre.2015.1080p.AMZN.WEB-DL.mkv', 'label': 'radarr-upgrade',
                     'progress': 5.0, 'state': 'Downloading'},
    }
    history = {'keephash': [IMPORTED] if imported else [], 'losehash': [grab(42, 50)]}
    setup(queue=queue, torrents=torrents, history=history, history_error=history_error)


def dedup_setup(imported, history_error=()):
    """Movie 7 (Spectre 2015) has a tracked file; two finished torrents match title+year.
    'keephash' matches the tracked file name, 'losehash' does not."""
    tracked = 'Spectre.2015.2160p.WEB-DL.4K4U.mkv'
    movies = [{'id': 7, 'title': 'Spectre', 'year': 2015, 'hasFile': True,
               'movieFile': {'relativePath': tracked}}]
    torrents = {
        'keephash': {'name': tracked, 'label': 'radarr', 'progress': 100.0, 'state': 'Seeding'},
        'losehash': {'name': 'Spectre.2015.1080p.AMZN.WEB-DL.mkv', 'label': 'radarr',
                     'progress': 100.0, 'state': 'Seeding'},
    }
    setup(torrents=torrents, movies=movies, history={'keephash': [IMPORTED] if imported else []},
          history_error=history_error)


def queue_item(qid, movie_id, dl, score):
    return {'id': qid, 'movieId': movie_id, 'downloadId': dl.upper(), 'title': f'Movie.{dl}',
            'customFormatScore': score, 'size': 100, 'sizeleft': 50}


def test_queue_pass_defers_loser_when_keeper_not_imported():
    lane_setup(imported=False)
    removed = aw.cleanup_radarr_queue_dupes()
    assert SUPERSEDED == [], f'loser relabeled superseded without a keeper import: {SUPERSEDED!r}'
    assert DELETES == [], f'queue record deleted: {DELETES!r}'
    assert removed == 0, f'deferred loser was counted as removed: {removed!r}'
    # An import lookup that fails is treated as "not imported" (fail-closed).
    lane_setup(imported=True, history_error=['keephash'])
    removed = aw.cleanup_radarr_queue_dupes()
    assert SUPERSEDED == [], f'superseded on lookup failure: {SUPERSEDED!r}'
    assert DELETES == [] and not removed, f'deleted/removed on lookup failure: {DELETES!r} {removed!r}'


def test_queue_pass_stuck_record_kept_but_queue_only_loser_still_removed():
    # Movie 9: loser 'll' is already a Deluge torrent (stuck queue record) and keeper 'kk'
    # has no import -> nothing may be touched. Movie 10: loser 'l2' is unknown to Deluge
    # (queue-only) -> its record is still removed from the client, exactly as before.
    setup(queue=[queue_item(7, 9, 'kk', 100), queue_item(8, 9, 'll', 20),
                 queue_item(11, 10, 'k2', 100), queue_item(12, 10, 'l2', 20)],
          torrents={'kk': {'name': 'Movie.kk', 'label': 'radarr', 'progress': 50.0, 'state': 'Downloading'},
                    'll': {'name': 'Movie.ll', 'label': 'radarr', 'progress': 100.0, 'state': 'Seeding'}})
    removed = aw.cleanup_radarr_queue_dupes()
    assert SUPERSEDED == [], f'superseded: {SUPERSEDED!r}'
    assert DELETES == [(12, True)], f'only the queue-only loser may be removed: {DELETES!r}'
    assert removed == 1, f'removed: {removed!r}'


def test_dedup_pass_defers_loser_when_keeper_not_imported():
    dedup_setup(imported=False)
    aw.dedup_via_radarr()
    assert SUPERSEDED == [], f'loser relabeled superseded without a keeper import: {SUPERSEDED!r}'
    assert DELETES == [], f'unexpected deletes: {DELETES!r}'
    dedup_setup(imported=True, history_error=['keephash'])
    aw.dedup_via_radarr()
    assert SUPERSEDED == [], f'superseded on lookup failure: {SUPERSEDED!r}'
    assert DELETES == [], f'unexpected deletes: {DELETES!r}'


def test_keeper_import_confirmed_helper():
    setup(history={'aaa': [grab(1, 10), IMPORTED], 'bbb': [grab(1, 10)]}, history_error=['ccc'])
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', 'AAA') is True
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', 'aaa') is True
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', 'bbb') is False
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', 'zzz') is False
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', 'ccc') is False
    assert aw._keeper_import_confirmed('http://radarr.test', 'k', '') is False


def test_losers_still_superseded_once_keeper_import_is_confirmed():
    # Unchanged real-import behaviour (green at baseline too): with the keeper's import in
    # Radarr history every pass supersedes the loser exactly as before.
    lane_setup(imported=True)
    removed = aw.cleanup_radarr_queue_dupes()
    assert SUPERSEDED == ['losehash'], f'lane loser: {SUPERSEDED!r}'
    assert DELETES == [] and removed == 1, f'lane pass: {DELETES!r} {removed!r}'
    setup(queue=[queue_item(7, 9, 'kk', 100), queue_item(8, 9, 'll', 20)],
          torrents={'kk': {'name': 'Movie.kk', 'label': 'radarr', 'progress': 50.0, 'state': 'Downloading'},
                    'll': {'name': 'Movie.ll', 'label': 'radarr', 'progress': 100.0, 'state': 'Seeding'}},
          history={'kk': [IMPORTED]})
    removed = aw.cleanup_radarr_queue_dupes()
    assert DELETES == [(8, False)] and SUPERSEDED == ['ll'], f'stuck record: {DELETES!r} {SUPERSEDED!r}'
    assert removed == 1, f'removed: {removed!r}'
    dedup_setup(imported=True)
    aw.dedup_via_radarr()
    assert SUPERSEDED == ['losehash'], f'dedup loser: {SUPERSEDED!r}'
    assert 'keephash' not in SUPERSEDED, 'the keeper must never be superseded'


TESTS = [
    test_queue_pass_defers_loser_when_keeper_not_imported,
    test_queue_pass_stuck_record_kept_but_queue_only_loser_still_removed,
    test_dedup_pass_defers_loser_when_keeper_not_imported,
    test_keeper_import_confirmed_helper,
    test_losers_still_superseded_once_keeper_import_is_confirmed,
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
