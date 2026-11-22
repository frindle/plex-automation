"""Penn always keeps HEVC over any other codec: the upgrade gate rejects a non-HEVC candidate replacing an HEVC
file (hard rule), and /plex-dupe-fix keeps the HEVC copy of a duplicate even when another copy is bigger or
higher resolution."""
import llm_decide as ld

from test_plex_dupe_fix import _file, _run, world  # noqa: F401  (fixture)

BASE = {'quality': 'Bluray-2160p', 'title': 'Movie.2019.2160p.BluRay.x264-GRP', 'codec': 'x264',
        'existing_quality': 'WEBDL-1080p', 'existing_codec': 'x265', 'seeders': 50}


def gate(action='allow', **over):
    return ld.apply_hard_rules('upgrade_gate', action, dict(BASE, **over))


def test_non_hevc_never_replaces_hevc():
    assert gate() == ('reject', 'non_hevc_over_hevc')
    assert gate('throttle') == ('reject', 'non_hevc_over_hevc')
    assert gate(codec='AV1') == ('reject', 'non_hevc_over_hevc')
    assert gate(codec='VC-1') == ('reject', 'non_hevc_over_hevc')
    for old in ('HEVC', 'h265', 'x265', 'h.265'):
        assert gate(existing_codec=old) == ('reject', 'non_hevc_over_hevc')
    assert gate('reject') == ('reject', None)


def test_hevc_candidates_and_unknowns_pass():
    assert gate(codec='HEVC') == ('allow', None)
    assert gate(codec='h.265') == ('allow', None)
    assert gate(codec=None) == ('allow', None)
    assert gate(codec='') == ('allow', None)
    assert gate(existing_codec=None) == ('allow', None)
    assert gate(existing_codec='x264') == ('allow', None)
    facts = dict(BASE)
    del facts['existing_codec']
    assert ld.apply_hard_rules('upgrade_gate', 'allow', facts) == ('allow', None)


def test_comparison_and_policy_say_hevc():
    assert '(loses HEVC' in ld.upgrade_comparison(BASE)
    assert '(gains HEVC' in ld.upgrade_comparison(dict(BASE, codec='x265', existing_codec='x264'))
    assert 'HEVC' not in ld.upgrade_comparison(dict(BASE, codec='x265', existing_codec='x265'))
    pol = ld.POLICY['upgrade_gate']
    assert 'Penn always keeps HEVC' in pol and 'whatever its size' in pol
    assert 'unless it is x264 replacing x265' not in pol


def _m(path, res, size, mid, codec):
    return {'file': path, 'resolution': res, 'size_gb': size, 'media_id': mid, 'codec': codec}


def test_dupe_fix_keeps_hevc_over_bigger_or_higher_res(world):  # noqa: F811
    d = '/media/movies/Some Movie (2021)/'
    hevc = d + 'Some.Movie.2021.1080p.x265.mkv'
    avc = d + 'Some.Movie.2021.2160p.x264.mkv'
    _file(world, hevc), _file(world, avc)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie',
                         'media': [_m(avc, '4k', 60, 1, 'h264'), _m(hevc, '1080', 4, 2, 'hevc')]}]
    plan = _run()['plan']['quality_upgrade'][0]
    assert plan['keep']['file'] == 'Some.Movie.2021.1080p.x265.mkv' and plan['keep']['codec'] == 'hevc'
    assert [s['file'] for s in plan['supersede']] == [avc]


def test_dupe_fix_same_codec_still_ranks_resolution_then_size(world):  # noqa: F811
    d = '/media/movies/Some Movie (2021)/'
    a, b = d + 'Some.Movie.2021.2160p.x265.mkv', d + 'Some.Movie.2021.1080p.x265.mkv'
    _file(world, a), _file(world, b)
    world['entries'] = [{'plex_key': '1', 'title': 'Some Movie',
                         'media': [_m(b, '1080', 9, 1, 'hevc'), _m(a, '4k', 20, 2, 'hevc')]}]
    assert _run()['plan']['quality_upgrade'][0]['keep']['file'] == 'Some.Movie.2021.2160p.x265.mkv'
