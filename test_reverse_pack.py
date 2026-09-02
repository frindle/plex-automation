"""Adversarial test for the reverse-pack fix: supersede a redundant season PACK
when the singles are the current keepers. Anchor: Make That Movie S01.

The safety case is the load-bearing one: a pack in a season with NO single-keeper
evidence must NEVER be selected -- it could be the only copy.
"""
import ast

aw = __import__('arr-webhook')

PACK_S01 = 'Make.That.Movie.S01.1080p.WEB.h264-GROUP'          # redundant pack
PACK_S02 = 'Make.That.Movie.S02.1080p.WEB.h264-GROUP'          # pack in an unknown season
SINGLE_S01E03 = 'Make.That.Movie.S01E03.1080p.WEB.h264-GROUP'  # an individual episode
H_PACK1 = '1111111111111111111111111111111111111111'
H_PACK2 = '2222222222222222222222222222222222222222'
H_SINGLE = '3333333333333333333333333333333333333333'


def test_selector():
    f = aw.select_pack_superseded_by_singles

    names = {H_PACK1: PACK_S01, H_SINGLE: SINGLE_S01E03}
    # Redundant S01 pack, S01 has single keepers, pack is not itself a keeper -> selected.
    out = f(names, keeper_ids=set(), keeper_single_seasons={1})
    assert out == [H_PACK1], f'FAIL: redundant pack not selected: {out}'
    print('  ok: redundant S01 pack selected when singles are the keepers')

    # The individual-episode torrent is NEVER returned.
    assert H_SINGLE not in f(names, set(), {1}), 'FAIL: an individual episode was returned'
    print('  ok: individual-episode torrent never returned')

    # SAFETY: a pack in a season with NO single-keeper evidence is NOT selected.
    out = f({H_PACK2: PACK_S02}, keeper_ids=set(), keeper_single_seasons={1})
    assert out == [], f'FAIL: pack in unknown season selected -- may be the only copy: {out}'
    print('  ok: pack in a non-single-keeper season NOT selected (safety)')

    # A pack that is ITSELF the current keeper is spared.
    out = f({H_PACK1: PACK_S01}, keeper_ids={H_PACK1.lower()}, keeper_single_seasons={1})
    assert out == [], f'FAIL: a pack that is itself the keeper was selected: {out}'
    print('  ok: pack that is itself the keeper is spared')

    # Empty keeper_single_seasons -> nothing selected.
    assert f(names, set(), set()) == [], 'FAIL: empty single-seasons should select nothing'
    print('  ok: empty keeper_single_seasons -> []')

    # A torrent with no season token at all is ignored, not crashed on.
    assert f({'x': 'Some.Random.Thing.1080p-GRP', H_PACK1: PACK_S01}, set(), {1}) == [H_PACK1], \
        'FAIL: no-season-token torrent mishandled'
    print('  ok: torrent with no season token ignored')

    # Case-insensitive keeper match: pack keeper given uppercase downloadId.
    out = f({H_PACK1: PACK_S01}, keeper_ids={H_PACK1.upper()}, keeper_single_seasons={1})
    assert out == [], 'FAIL: keeper match must be case-insensitive'
    print('  ok: keeper-id match is case-insensitive')


def test_io_helper_single_vs_pack():
    # BEHAVIOURAL: a season kept by a SINGLE (one episode per keeper did) appears;
    # a season kept by a PACK (one did across >=2 episodes) does NOT.
    class FakeResp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    # Season 1: two episodes, each with its OWN downloadId -> singles.
    # Season 2: two episodes sharing ONE downloadId -> a pack.
    HISTORY = [
        {'episodeId': 101, 'date': '2026-01-01', 'downloadId': 's1e1'},
        {'episodeId': 102, 'date': '2026-01-01', 'downloadId': 's1e2'},
        {'episodeId': 201, 'date': '2026-01-01', 'downloadId': 'packdid'},
        {'episodeId': 202, 'date': '2026-01-01', 'downloadId': 'packdid'},
    ]
    EPISODES = [
        {'id': 101, 'seasonNumber': 1, 'episodeNumber': 1},
        {'id': 102, 'seasonNumber': 1, 'episodeNumber': 2},
        {'id': 201, 'seasonNumber': 2, 'episodeNumber': 1},
        {'id': 202, 'seasonNumber': 2, 'episodeNumber': 2},
    ]

    def fake_get(url, **kw):
        return FakeResp(EPISODES if url.endswith('/episode') else HISTORY)

    orig = aw.requests.get
    aw.requests.get = fake_get
    try:
        seasons = aw._sonarr_keeper_single_seasons(1)
    finally:
        aw.requests.get = orig
    assert 1 in seasons, f'FAIL: season 1 (single keepers) missing: {seasons}'
    assert 2 not in seasons, f'FAIL: season 2 (pack keeper) wrongly reported as single-kept: {seasons}'
    print('  ok: I/O helper reports single-kept season, excludes pack-kept season')


def test_wiring():
    src = open('arr-webhook.py').read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert '_sonarr_keeper_single_seasons' in names, 'FAIL: I/O helper not defined'
    assert 'select_pack_superseded_by_singles' in names, 'FAIL: selector not defined'
    dedup = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == 'dedup_via_sonarr')
    called = {n.func.id for n in ast.walk(dedup)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'select_pack_superseded_by_singles' in called, 'FAIL: selector not called from dedup_via_sonarr'
    assert '_sonarr_keeper_single_seasons' in called, 'FAIL: single-seasons helper not called from dedup_via_sonarr'
    print('  ok: selector + helper both wired into dedup_via_sonarr')


if __name__ == '__main__':
    test_selector()
    test_io_helper_single_vs_pack()
    test_wiring()
    print('test_reverse_pack: all assertions passed')
