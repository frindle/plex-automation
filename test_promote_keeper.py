"""Frozen tests: _promote_keeper (arr-webhook.py) may move a duplicate-pass keeper
to the top of the queue only for normal grabs (same-year upgrades lead their
own lane as before). Older-year upgrade keepers (RADARR_UPG_LABEL /
SONARR_UPG_LABEL) are throttled by design: they only lead their own bottom
lane -- never queue_top, and no other lane is moved.

Deluge is faked at the HTTP boundary (requests.Session.post); every
queue_top / queue_bottom call is recorded in POSTS as (method, hashes).

Run: python3 test_promote_keeper.py
"""
import os
import sys
import tempfile

import requests

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
aw = __import__('arr-webhook')   # alias assigned only here

POSTS = []


class Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {'result': True}


def fake_session_post(self, url, json=None, timeout=None, **kw):
    if json['method'] in ('core.queue_top', 'core.queue_bottom'):
        POSTS.append((json['method'], list(json['params'][0])))
    return Resp()


requests.Session.post = fake_session_post


def promote(label, progress=50.0, h='keephash'):
    """Keeper `h` plus one other unfinished torrent in the same lane and one finished torrent."""
    del POSTS[:]
    torrents = {
        h: {'label': label, 'progress': progress, 'name': 'keeper'},
        'otherhash': {'label': label, 'progress': 20.0, 'name': 'other'},
        'donehash': {'label': label, 'progress': 100.0, 'name': 'done'},
    }
    aw._promote_keeper(h, torrents)
    return list(POSTS)


def promote_mixed(label):
    """Older-year keeper plus a same-lane peer, a normal grab and a same-year upgrade."""
    del POSTS[:]
    torrents = {
        'keephash': {'label': label, 'progress': 50.0, 'name': 'keeper'},
        'otherhash': {'label': label, 'progress': 20.0, 'name': 'other'},
        'donehash': {'label': label, 'progress': 100.0, 'name': 'done'},
        'normhash': {'label': 'radarr', 'progress': 5.0, 'name': 'normal'},
        'recenthash': {'label': aw.RADARR_UPG_PRIORITY_LABEL, 'progress': 5.0, 'name': 'recent'},
    }
    aw._promote_keeper('keephash', torrents)
    return list(POSTS)


def _assert_lane_head_only(label):
    for posts in (promote(label), promote_mixed(label)):
        assert posts == [('core.queue_bottom', ['keephash']), ('core.queue_bottom', ['otherhash'])], \
            f'{label}: older-year keeper must lead only its own lane: {posts!r}'
        assert not any(m == 'core.queue_top' for m, _ in posts), f'{label}: global top used: {posts!r}'


def test_older_year_radarr_upgrade_keeper_not_promoted():
    """Older-year: head of its own (bottom) lane only -- never queue_top, no other lane moved."""
    _assert_lane_head_only('radarr-upgrade')
    _assert_lane_head_only(aw.RADARR_UPG_LABEL)


def test_older_year_sonarr_upgrade_keeper_not_promoted():
    _assert_lane_head_only('sonarr-upgrade')
    _assert_lane_head_only(aw.SONARR_UPG_LABEL)


def test_same_year_upgrade_keeper_still_promoted():
    for label in (aw.RADARR_UPG_PRIORITY_LABEL, aw.SONARR_UPG_PRIORITY_LABEL):
        posts = promote(label)
        assert posts and posts[0] == ('core.queue_bottom', ['keephash']), f'{label}: keeper must lead its lane: {posts!r}'
        assert ('core.queue_bottom', ['otherhash']) in posts, f'{label}: rest of lane follows: {posts!r}'
        assert all('donehash' not in hs for _, hs in posts), f'{label}: finished torrent moved: {posts!r}'


def test_normal_keeper_still_promoted():
    for label in ('radarr', 'sonarr'):
        posts = promote(label)
        assert posts == [('core.queue_top', ['otherhash']), ('core.queue_top', ['keephash'])], \
            f'{label}: keeper must end on top: {posts!r}'


def test_unchanged_guards_nearly_done_missing_empty():
    assert promote('radarr', progress=99.5) == [], 'keeper at 99.5% must not be reordered'
    del POSTS[:]
    aw._promote_keeper('nothere', {'otherhash': {'label': 'radarr', 'progress': 5.0}})
    assert POSTS == [], f'missing keeper reordered: {POSTS!r}'
    del POSTS[:]
    aw._promote_keeper('', {'': {'label': 'radarr', 'progress': 5.0}})
    assert POSTS == [], f'empty hash reordered: {POSTS!r}'


TESTS = [
    test_older_year_radarr_upgrade_keeper_not_promoted,
    test_older_year_sonarr_upgrade_keeper_not_promoted,
    test_same_year_upgrade_keeper_still_promoted,
    test_normal_keeper_still_promoted,
    test_unchanged_guards_nearly_done_missing_empty,
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
