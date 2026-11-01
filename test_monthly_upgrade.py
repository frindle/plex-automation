"""Frozen test: monthly_upgrade Step 1 purge must respect the seed floor.

The script runs at import time, so it is executed here with Session, requests
and time.sleep faked; time.sleep (Step 3, after the purge) stops it.
"""
import importlib.util
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
MB = 1024 * 1024
POSTS = []


class _Stop(Exception):
    pass


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


TORRENTS = {}


def _fake_post(self, url, json=None, **kw):
    POSTS.append(json)
    if json['method'] == 'core.get_torrents_status':
        fields = json['params'][1]
        return _Resp({'result': {h: {k: v for k, v in t.items() if k in fields}
                                 for h, t in TORRENTS.items()}})
    return _Resp({'result': []})


def _fake_get(url, **kw):
    return _Resp([])


def _stop(*a, **k):
    raise _Stop()


def _t(label='radarr-upgrade', total_done=MB, **kw):
    d = {'name': 'n', 'label': label, 'total_done': total_done}
    d.update(kw)
    return d


def _run_script(torrents):
    POSTS.clear()
    TORRENTS.clear()
    TORRENTS.update(torrents)
    os.environ['DELUGE_URL'] = 'http://deluge.invalid'
    os.environ['RADARR_URL'] = 'http://radarr.invalid'
    os.environ['RADARR_API_KEY'] = 'x'
    saved = (requests.Session.post, requests.get, requests.post, time.sleep)
    requests.Session.post = _fake_post
    requests.get = _fake_get
    requests.post = _fake_get
    time.sleep = _stop
    try:
        spec = importlib.util.spec_from_file_location('mu_target', os.path.join(_HERE, 'monthly_upgrade.py'))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except _Stop:
            pass
    finally:
        requests.Session.post, requests.get, requests.post, time.sleep = saved
    removed = []
    for c in POSTS:
        if c['method'] == 'core.remove_torrents':
            removed.extend(c['params'][0])
        elif c['method'] == 'core.remove_torrent':
            removed.append(c['params'][0])
    return removed


NEVER = dict(seeding_time=0, total_uploaded=0, is_finished=False, progress=3.0)
SEEDED = dict(seeding_time=86400, total_uploaded=500, is_finished=True, progress=100.0)


def test_only_never_seeded_is_removed():
    removed = _run_script({'aaa': _t(**NEVER), 'bbb': _t(**SEEDED), 'ccc': _t()})
    assert removed == ['aaa'], f'removed: {removed!r}'


def test_unknown_seed_time_is_kept():
    removed = _run_script({'aaa': _t(seeding_time=None, total_uploaded=0, is_finished=False, progress=3.0)})
    assert removed == [], f'removed: {removed!r}'


def test_uploaded_or_finished_is_kept():
    removed = _run_script({
        'aaa': _t(seeding_time=0, total_uploaded=10, is_finished=False, progress=3.0),
        'bbb': _t(seeding_time=0, total_uploaded=0, is_finished=True, progress=100.0),
        'ccc': _t(seeding_time=0, total_uploaded=0, is_finished=False, progress=100.0),
    })
    assert removed == [], f'removed: {removed!r}'


def test_requests_seed_fields_from_deluge():
    _run_script({})
    fields = [c['params'][1] for c in POSTS if c['method'] == 'core.get_torrents_status']
    assert fields, f'no status call: {POSTS!r}'
    for need in ('seeding_time', 'total_uploaded', 'is_finished', 'progress', 'total_done'):
        assert need in fields[0], f'{need} missing from {fields[0]!r}'


def test_big_and_other_label_untouched():
    removed = _run_script({'aaa': _t(total_done=50 * MB, **NEVER), 'bbb': _t(label='radarr', **NEVER)})
    assert removed == [], f'removed: {removed!r}'


TESTS = [test_only_never_seeded_is_removed, test_unknown_seed_time_is_kept,
         test_uploaded_or_finished_is_kept, test_requests_seed_fields_from_deluge,
         test_big_and_other_label_untouched]

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
    sys.exit(1 if failed else 0)
