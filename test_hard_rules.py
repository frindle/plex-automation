import llm_decide as ld

def test_dv_p5_web_rejected():
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'candidate_dv_web': True}) == ('reject', 'dv_p5_web')


def test_min_seeders():
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': 3}) == ('reject', 'min_seeders')
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': 10}) == ('allow', None)


def test_identity_year_mismatch_never_match():
    assert ld.apply_hard_rules('import_identity', 'match', {'year_matches': False}) == ('unsure', 'year_mismatch')


def test_unknown_kinds_pass_through_and_never_raise():
    assert ld.apply_hard_rules('nope', 'x', object()) == ('x', None)
    assert ld.apply_hard_rules('upgrade_gate', 'allow', None) == ('allow', None)


def test_dupe_keeper_is_no_longer_a_model_decision():
    assert 'dupe_keeper' not in ld.KINDS
    try:
        ld.build_prompt('dupe_keeper', {})
    except ValueError:
        pass
    else:
        raise AssertionError('dupe_keeper prompt should be refused')


def test_prompt_carries_policy():
    p = ld.build_prompt('upgrade_gate', {})
    assert 'Policy:' in p and 'seeders' in p and 'existing_' in p


def test_upgrade_vetoes_cover_throttle_and_odd_types():
    assert ld.apply_hard_rules('upgrade_gate', 'throttle', {'candidate_dv_web': True}) == ('reject', 'dv_p5_web')
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'candidate_dv_web': 'yes'}) == ('reject', 'dv_p5_web')
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'candidate_dv_web': 'false'}) == ('allow', None)
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': '3'}) == ('reject', 'min_seeders')
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': float('nan')})[0] == 'reject'
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': 'many'})[0] == 'reject'
    assert ld.apply_hard_rules('import_identity', 'match', {'year_matches': 'false'}) == ('unsure', 'year_mismatch')
    assert ld.apply_hard_rules('import_identity', 'match', {'year_matches': 0}) == ('unsure', 'year_mismatch')
    assert ld.apply_hard_rules('import_identity', 'match', {}) == ('match', None)


def _spin(q, m, service='Sonarr'):
    return ld.apply_hard_rules('import_identity', 'match', {'service': service, 'queue_title': q, 'matched_title': m})


def test_spinoff_release_is_never_a_match():
    for q, m in [('NCIS.Origins.S03E05.1080p.WEB.H264-CAKES', 'NCIS'),
                 ('9-1-1 - Nashville (2025) S02E05 Ding-Dong-Ditch (1080p)', '9-1-1'),
                 ('Law.and.Order.Organized.Crime.S04E01.1080p.WEB.h264-ETHEL', 'Law & Order'),
                 ('The.Walking.Dead.Dead.City.S02E01.1080p.WEB.H264-GRP', 'The Walking Dead'),
                 ('90.Day.Fiance.Before.the.90.Days.S07E05.1080p.WEB.h264-GRP', '90 Day Fiancé'),
                 ('House.of.the.Dragon.S02E08.2160p.WEB.H265-GRP', 'House')]:
        assert _spin(q, m) == ('unsure', 'spinoff_title'), q


def test_same_show_spelled_differently_still_matches():
    for q, m in [('NCIS.Origins.S02E05.1080p.WEB.h264-ETHEL', 'NCIS: Origins'),
                 ('Law.and.Order.S25E03.1080p.WEB.h264-ETHEL', 'Law & Order'),
                 ('The.Office.US.S09E01.1080p.BluRay.x264-GRP', 'The Office'),
                 ('Ghosts (US) S04E05 1080p WEB h264-ETHEL', 'Ghosts'),
                 ('Greys.Anatomy.S22E01.1080p.WEB.h264-ETHEL', "Grey's Anatomy"),
                 ('Shogun.2024.S01E10.2160p.WEB.H265-GRP', 'Shōgun'),
                 ('Doctor.Who.2023.S01E01.2160p.WEB.H265-GRP', 'Doctor Who'),
                 ('Jimmy.Kimmel.Live.2025.11.12.720p.WEB.h264-GRP', 'Jimmy Kimmel Live'),
                 ('The.100.S07E16.1080p.BluRay.x264-GRP', 'The 100'),
                 ('Slow.Horses.Season.4.Complete.2160p.WEB.H265-GRP', 'Slow Horses'),
                 ('[SubsPlease] Dandadan - 17 (1080p) [ABCD1234].mkv', 'Dandadan'),
                 ('[Erai-raws] Dandadan 2nd Season - 05 [1080p][Multiple Subtitle].mkv', 'Dandadan'),
                 ('Chernobyl.Part.1.1080p.WEB.H264-GRP', 'Chernobyl')]:
        assert _spin(q, m) == ('match', None), q


def test_spinoff_guard_skips_radarr_and_missing_titles():
    assert _spin('Mad.Max.2.The.Road.Warrior.1981.1080p.BluRay.x264-GRP', 'Mad Max 2', 'Radarr') == ('match', None)
    assert ld.apply_hard_rules('import_identity', 'match', {'queue_title': 'NCIS.Origins.S01E01'}) == ('match', None)
    assert ld.apply_hard_rules('import_identity', 'mismatch', {'service': 'Sonarr', 'queue_title': 'NCIS.Origins.S01E01',
                                                               'matched_title': 'NCIS'}) == ('mismatch', None)
    assert ld.release_show_name('NCIS.Origins.S03E05.1080p.WEB.H264-CAKES') == 'ncis origins'
    assert ld.spinoff_words('NCIS.S23E05.1080p.WEB.h264-ETHEL', 'NCIS: Origins') == []
