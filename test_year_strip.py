"""Adversarial test for add_year_stripped_variants + its wiring into
get_sonarr_series_titles. Behavioural: imports the real module and drives the
real matcher. The anchor case is Very Important People (2023) S03E15.

Property: a series title carrying a disambiguation year must still match a
release that lacks the year token, WITHOUT stripping years from titles that ARE
a year (1923), stripping non-year parentheticals (US), or dropping originals.
"""
import ast

aw = __import__('arr-webhook')


def check(desc, got, want):
    assert got == want, f'FAIL: {desc}: got {got!r} want {want!r}'
    print(f'  ok: {desc}')


def test_add_year_stripped_variants():
    f = aw.add_year_stripped_variants

    # Parenthesized year: stripped form is added, original kept (ADDITIVE).
    out = f({'very important people (2023)'})
    assert 'very important people' in out, f'FAIL: parenthesized year not stripped: {out}'
    assert 'very important people (2023)' in out, f'FAIL: original dropped: {out}'
    print('  ok: parenthesized year stripped, original kept')

    # Bare trailing year with preceding words.
    out = f({'foo bar 2019'})
    assert 'foo bar' in out and 'foo bar 2019' in out, f'FAIL: bare trailing year: {out}'
    print('  ok: bare trailing year stripped, original kept')

    # A show that IS a year must not be reduced to empty / must keep itself.
    out = f({'1923'})
    assert out == {'1923'}, f'FAIL: bare-year title should be unchanged: {out}'
    assert '' not in out, 'FAIL: empty string produced'
    print('  ok: bare-year title (1923) unchanged, no empty variant')

    # Non-year parenthetical must not be stripped.
    out = f({'the office (us)'})
    assert out == {'the office (us)'}, f'FAIL: non-year paren stripped: {out}'
    print('  ok: non-year parenthetical (us) not stripped')

    # No year present -> unchanged.
    out = f({"blake's 7"})
    assert out == {"blake's 7"}, f'FAIL: no-year title changed: {out}'
    print("  ok: no-year title unchanged")

    # No variant whose longest word < 3 chars may be added.
    out = f({'ab 2020'})
    assert 'ab' not in out, f'FAIL: added a <3-char variant: {out}'
    print('  ok: does not add a degenerate <3-char variant')

    # Robustness: empty input, non-string entry.
    assert f(set()) == set(), 'FAIL: empty input not empty set'
    out = f({'ncis (2003)', 12345})  # non-string skipped, not raised
    assert 'ncis' in out
    print('  ok: empty input and non-string entry tolerated')


def test_matcher_behaviour():
    # The end-to-end property: the real matcher, fed the year-augmented variants,
    # matches the no-year release. Baseline (no stripped variant) must NOT match.
    variants = aw.add_year_stripped_variants({'very important people (2023)'})
    torrent = 'Very.Important.People.S03E15.1080p.WEB.h264-GROUP'
    assert aw.torrent_matches_any_title(torrent, variants) is True, \
        'FAIL: no-year release still does not match after year-strip'
    print('  ok: no-year S03E15 release matches the year-augmented variants')

    # Control: an unrelated release must still NOT match (no over-broadening).
    other = 'The.Bear.S03E01.1080p.WEB.h264-GROUP'
    assert aw.torrent_matches_any_title(other, variants) is False, \
        'FAIL: year-strip over-broadened and matched an unrelated release'
    print('  ok: unrelated release still does not match (no over-broadening)')


def test_wired_into_get_sonarr_series_titles():
    src = open('arr-webhook.py').read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert 'add_year_stripped_variants' in names, 'FAIL: helper not defined'
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'get_sonarr_series_titles')
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'add_year_stripped_variants' in called, \
        'FAIL: get_sonarr_series_titles does not call add_year_stripped_variants'
    print('  ok: helper defined and called from get_sonarr_series_titles')


if __name__ == '__main__':
    test_add_year_stripped_variants()
    test_matcher_behaviour()
    test_wired_into_get_sonarr_series_titles()
    print('test_year_strip: all assertions passed')
