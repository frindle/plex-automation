"""Frozen tests: _promote_keeper (arr-webhook.py) may move a duplicate-pass keeper
to the top of its lane only for normal and SAME-year upgrade labels. Older-year
upgrade lanes (RADARR_UPG_LABEL / SONARR_UPG_LABEL) are throttled by design and
must not be promoted.

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


def test_older_year_radarr_upgrade_keeper_not_promoted():
    posts = promote('radarr-upgrade')
    assert posts == [], f'older-year radarr-upgrade keeper was reordered: {posts!r}'
    posts = promote(aw.RADARR_UPG_LABEL)
    assert not posts, f'RADARR_UPG_LABEL keeper was reordered: {posts!r}'


def test_older_year_sonarr_upgrade_keeper_not_promoted():
    posts = promote('sonarr-upgrade')
    assert posts == [], f'older-year sonarr-upgrade keeper was reordered: {posts!r}'
    posts = promote(aw.SONARR_UPG_LABEL)
    assert not posts, f'SONARR_UPG_LABEL keeper was reordered: {posts!r}'


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
