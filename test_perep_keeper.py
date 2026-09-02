"""Adversarial test for select_episode_dupe_losers + call-site wiring.

Property: for a re-imported episode, supersede everything EXCEPT the latest-import
keeper; never the keeper; fall back (return None) when the latest keeper is unknown
or not present among the matched torrents. The Reacher S04E03 case is the anchor.
"""
import ast

aw = __import__('arr-webhook')

CAKES = '57bcd1579864736e9fbeea230115ddaaf2acae31'   # older import
NEONOIR = '2a19e4e32a99a74461cd7c8e93a6ac12bb54b3d5' # latest import = keeper

def check(desc, got, want):
    assert got == want, f'FAIL: {desc}: got {got!r} want {want!r}'
    print(f'  ok: {desc} -> {got}')

def test_select_episode_dupe_losers():
    f = aw.select_episode_dupe_losers
    # THE REACHER CASE: two torrents, latest keeper is NeoNoir -> supersede CAKES only
    check('Reacher S04E03: supersede older, keep latest',
          f('S04E03', [CAKES, NEONOIR], {'S04E03': NEONOIR.lower()}), [CAKES])
    # keeper is never in the returned losers
    losers = f('S04E03', [CAKES, NEONOIR], {'S04E03': NEONOIR.lower()})
    assert NEONOIR not in losers, 'FAIL: keeper hash returned as a loser'
    print('  ok: keeper never returned as loser')
    # case-insensitive: hashes uppercased, map lowercased
    check('case-insensitive hash match',
          f('S04E03', [CAKES.upper(), NEONOIR.upper()], {'S04E03': NEONOIR.lower()}),
          [CAKES.upper()])
    # keeper present + two older dupes -> both older superseded
    OTHER = 'abcabcabcabcabcabcabcabcabcabcabcabcabca'
    check('keeper + two losers',
          sorted(f('S04E03', [CAKES, NEONOIR, OTHER], {'S04E03': NEONOIR.lower()})),
          sorted([CAKES, OTHER]))
    # no map entry for the episode -> None (fall back, don't guess)
    check('no latest-keeper entry -> fall back', f('S04E03', [CAKES, NEONOIR], {}), None)
    # latest keeper downloadId NOT present among these torrents -> None (don't guess)
    check('keeper not present -> fall back',
          f('S04E03', [CAKES, NEONOIR], {'S04E03': 'ffffffffffffffffffffffffffffffffffffffff'}), None)
    # None / empty map arg -> None, no crash
    check('None map -> fall back', f('S04E03', [CAKES, NEONOIR], None), None)

def test_call_site_wired():
    src = open('arr-webhook.py').read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert 'select_episode_dupe_losers' in names, 'FAIL: select_episode_dupe_losers not defined'
    assert '_sonarr_latest_keeper_by_episode_key' in names, 'FAIL: _sonarr_latest_keeper_by_episode_key not defined'
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'dedup_via_sonarr')
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'select_episode_dupe_losers' in called, 'FAIL: dedup_via_sonarr does not call select_episode_dupe_losers'
    assert '_sonarr_latest_keeper_by_episode_key' in called, 'FAIL: dedup_via_sonarr does not build the latest-keeper map'
    print('  ok: both helpers defined and called from dedup_via_sonarr')

if __name__ == '__main__':
    test_select_episode_dupe_losers()
    test_call_site_wired()
    print('test_perep_keeper: all assertions passed')
