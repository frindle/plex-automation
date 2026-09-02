"""Adversarial test for the blank-downloadId sourceTitle fallback.

Property: when an episode's keeper import has a blank downloadId, resolve the
keeper by matching the newest import's sourceTitle to a torrent's release name;
pick it iff EXACTLY ONE torrent matches; never guess. Anchor: MAFS UK S10E08.
The pure selector is what the verify drives; the I/O helper and wiring are
checked structurally.
"""
import ast

aw = __import__('arr-webhook')

# Two candidate torrents for S10E08; only one matches the kept sourceTitle.
KEPT = 'Married.at.First.Sight.UK.S10E08.1080p.HDTV.x264-GROUP'
OTHER = 'Married.at.First.Sight.UK.S10E08.720p.WEB.h264-OTHER'
H_KEEP = 'aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000'
H_OTHER = 'bbbb1111bbbb1111bbbb1111bbbb1111bbbb1111'
NAMES = {H_KEEP: KEPT + '.mkv', H_OTHER: OTHER + '.mkv'}


def test_selector():
    f = aw.select_episode_keeper_by_source_title

    # Exact match on the kept sourceTitle -> that hash is the keeper.
    assert f([H_KEEP, H_OTHER], NAMES, KEPT) == H_KEEP, 'FAIL: did not pick sourceTitle match'
    print('  ok: keeper picked by sourceTitle match')

    # Ext-stripping: torrent name has .mkv, sourceTitle does not -> still matches.
    assert f([H_KEEP], {H_KEEP: KEPT + '.mkv'}, KEPT) == H_KEEP, 'FAIL: ext-strip match failed'
    print('  ok: .mkv extension stripped before compare')

    # Case-insensitive.
    assert f([H_KEEP, H_OTHER], NAMES, KEPT.upper()) == H_KEEP, 'FAIL: not case-insensitive'
    print('  ok: case-insensitive match')

    # Zero matches -> None (fall back, do not guess).
    assert f([H_KEEP, H_OTHER], NAMES, 'Totally.Different.Release-XYZ') is None, \
        'FAIL: should be None when nothing matches'
    print('  ok: zero matches -> None')

    # More than one match -> None (ambiguous, do not guess).
    dupe_names = {H_KEEP: KEPT + '.mkv', H_OTHER: KEPT + '.mkv'}
    assert f([H_KEEP, H_OTHER], dupe_names, KEPT) is None, \
        'FAIL: two identical matches must be ambiguous -> None'
    print('  ok: >1 match -> None (no guessing)')

    # None / non-string / empty sourceTitle -> None, no raise.
    assert f([H_KEEP], NAMES, None) is None, 'FAIL: None source_title should be None'
    assert f([H_KEEP], NAMES, '') is None, 'FAIL: empty source_title should be None'
    assert f([H_KEEP], NAMES, 12345) is None, 'FAIL: non-string source_title should be None'
    print('  ok: None/empty/non-string sourceTitle tolerated -> None')

    # A missing name must not raise.
    assert f([H_KEEP, 'ccc'], {H_KEEP: KEPT + '.mkv'}, KEPT) == H_KEEP, \
        'FAIL: missing name entry raised or broke the match'
    print('  ok: missing name entry tolerated')


def test_io_helper_keeps_blank_did_rows():
    # BEHAVIOURAL relevance check: drive the real I/O helper with a mocked Sonarr
    # API whose kept import for S10E08 has a BLANK downloadId. A helper that
    # copied the blank-did skip would return {} for that episode; a correct one
    # returns its sourceTitle. This is the property, not a source-text match.
    class FakeResp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    HISTORY = [
        # The kept import: blank downloadId, real sourceTitle. Must NOT be dropped.
        {'episodeId': 808, 'date': '2026-02-01T00:00:00Z', 'downloadId': '',
         'sourceTitle': 'Married.at.First.Sight.UK.S10E08.1080p.HDTV.x264-GROUP'},
        # An older import for the same episode -> newest (by date) must win.
        {'episodeId': 808, 'date': '2026-01-01T00:00:00Z', 'downloadId': 'deadbeef',
         'sourceTitle': 'Married.at.First.Sight.UK.S10E08.720p.WEB.h264-OLD'},
    ]
    EPISODES = [{'id': 808, 'seasonNumber': 10, 'episodeNumber': 8}]

    def fake_get(url, **kw):
        return FakeResp(EPISODES if url.endswith('/episode') else HISTORY)

    orig = aw.requests.get
    aw.requests.get = fake_get
    try:
        out = aw._sonarr_latest_source_title_by_episode_key(1)
    finally:
        aw.requests.get = orig
    assert out.get('S10E08') == 'Married.at.First.Sight.UK.S10E08.1080p.HDTV.x264-GROUP', \
        f'FAIL: helper dropped the blank-downloadId keeper row or picked the wrong date: {out}'
    print('  ok: I/O helper keeps the blank-downloadId row and takes the newest by date')


def test_wiring():
    src = open('arr-webhook.py').read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert '_sonarr_latest_source_title_by_episode_key' in names, 'FAIL: I/O helper not defined'
    assert 'select_episode_keeper_by_source_title' in names, 'FAIL: selector not defined'

    # The selector must be called from dedup_via_sonarr.
    dedup = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == 'dedup_via_sonarr')
    called = {n.func.id for n in ast.walk(dedup)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'select_episode_keeper_by_source_title' in called, \
        'FAIL: dedup_via_sonarr does not call the sourceTitle selector'
    assert '_sonarr_latest_source_title_by_episode_key' in called, \
        'FAIL: dedup_via_sonarr does not build the sourceTitle map'
    print('  ok: selector + map builder both wired into dedup_via_sonarr')


if __name__ == '__main__':
    test_selector()
    test_io_helper_keeps_blank_did_rows()
    test_wiring()
    print('test_blank_did: all assertions passed')
