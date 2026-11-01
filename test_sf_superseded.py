"""Frozen tests: cleanup_superseded and purge_queued_superseded must not count a removal, or record a removal activity, when the seed floor defers the removal (the fresh per-torrent status shows a young torrent).
Run: python3 test_sf_superseded.py"""
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


def _sup(seeding_time, progress=100.0, **kw):
    return dict(full(seeding_time, progress=progress), label=aw.SUPERSEDED_LABEL, state='Seeding', **dict({'total_done': 1}, **kw))


def test_cleanup_deferred():
    setup()
    STATE['all'] = {'a': _sup(30 * DAY), 'b': _sup(30 * DAY), 'orph': _sup(0, progress=0.0, total_done=0)}
    STATE['one'] = {'a': young(), 'b': full(30 * DAY), 'orph': full(3600, progress=0.0)}
    aw.cleanup_superseded()
    assert removed_hashes() == ['b'], RPC
    assert any('Removed 1 superseded' in s for s in summaries()), summaries()
    assert not any('orphan' in s for s in summaries()), summaries()


def test_cleanup_orphan_deferred():
    setup()
    STATE['all'] = {'b': _sup(30 * DAY), 'orph': _sup(0, progress=0.0, total_done=0)}
    STATE['one'] = {'b': full(30 * DAY), 'orph': full(60, progress=0.0)}
    aw.cleanup_superseded()
    assert removed_hashes() == ['b'], RPC
    assert any('Removed 1 superseded' in s for s in summaries()), summaries()
    assert not any('Removed superseded orphan' in s for s in summaries()), summaries()


def test_purge_queued_counts():
    setup()
    STATE['one'] = {'h0': never(), 'h1': young(), 'h2': never()}
    targets = [{'hash': f'h{i}', 'name': f'n{i}', 'seeding_time': 0} for i in range(3)]
    removed, failed = aw.purge_queued_superseded(targets)
    assert removed == 2 and failed == [], (removed, failed)
    assert removed_hashes() == ['h0', 'h2'], RPC


TESTS = [
    test_cleanup_deferred,
    test_cleanup_orphan_deferred,
    test_purge_queued_counts,
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
