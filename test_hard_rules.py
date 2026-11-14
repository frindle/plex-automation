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


def test_reverse_spinoff_is_never_a_match():
    # The matched title extends the release's name: the release is the parent show, not the spin-off.
    for q, m in (('NCIS.S23E05.1080p.WEB.h264-ETHEL', 'NCIS: Origins'),
                 ('Chicago.S01E01.1080p.WEB.h264-GRP', 'Chicago Fire')):
        assert _spin(q, m) == ('unsure', 'spinoff_title_reverse'), q
    # Radarr titles often drop a subtitle ('Borat' for the long TMDB title); the year rule covers sequels there.
    assert _spin('Borat.2006.1080p.BluRay.x264-GRP', 'Borat: Cultural Learnings of America', 'Radarr') == ('match', None)
    assert ld.reverse_spinoff_words('NCIS.S23E05.1080p.WEB.h264-ETHEL', 'NCIS') == []
    assert ld.reverse_spinoff_words('The.Office.US.S01E01.720p', 'The Office') == []


def test_different_first_word_is_never_a_match():
    for q, m, svc in (('Chicago.Fire.S13E01.1080p.WEB.h264-GRP', 'Line of Fire', 'Sonarr'),
                      ('It.Chapter.Two.2019.1080p.BluRay.x264-GRP', 'John Wick: Chapter 3 - Parabellum', 'Radarr')):
        assert ld.apply_hard_rules('import_identity', 'match',
                                   {'service': svc, 'queue_title': q, 'matched_title': m}) == ('unsure', 'title_first_word'), q
    # Articles are skipped; a mismatch/unsure answer is never changed.
    assert not ld.first_word_differs('The.Bear.S03E01.1080p', 'Bear')
    assert ld.apply_hard_rules('import_identity', 'mismatch',
                               {'queue_title': 'Chicago.Fire.S13E01', 'matched_title': 'Line of Fire'}) == ('mismatch', None)


def test_title_guards_leave_real_matches_alone():
    for q, m in (('Chicago.P.D.S12E01.1080p.WEB.h264-GRP', 'Chicago P.D.'),
                 ('Law.and.Order.SVU.S26E01.1080p.WEB.h264-GRP', 'Law & Order: Special Victims Unit'),
                 ('Shogun.2024.S01E01.1080p.WEB.h264-GRP', 'Shōgun (2024)'),
                 ('The.Office.US.S01E01.720p.WEB.h264-GRP', 'The Office (US)')):
        assert ld.apply_hard_rules('import_identity', 'match',
                                   {'service': 'Sonarr', 'queue_title': q, 'matched_title': m}) == ('match', None), q


def test_titles_that_go_different_ways_are_never_a_match():
    # Same first word, then a different show (back-test false match dd305d160a56 was the first pair).
    for q, m in (('Chicago PD S14E06 1080p x265-ELiTE', 'Chicago Fire'),
                 ('Chicago.Med.S11E02.1080p.WEB.h264-GRP', 'Chicago P.D.'),
                 ('Star.Trek.Discovery.S05E01.2160p.WEB.H265-GRP', 'Star Trek: Strange New Worlds'),
                 ('FBI.International.S04E01.1080p.WEB.h264-GRP', 'FBI: Most Wanted')):
        assert _spin(q, m) == ('unsure', 'title_diverges'), q
    # Radarr titles and releases word sequels and subtitles differently; the year rule covers movies.
    assert _spin('Harry.Potter.and.the.Sorcerers.Stone.2001.1080p.BluRay.x264-GRP',
                 "Harry Potter and the Philosopher's Stone", 'Radarr') == ('match', None)


def test_spelling_and_initialisms_are_not_divergence():
    for q, m in (('Chicago.PD.S12E01.1080p.WEB.h264-GRP', 'Chicago P.D.'),
                 ('Law.Order.SVU.S26E01.1080p.WEB.h264-GRP', 'Law & Order: Special Victims Unit'),
                 ('NCIS.LA.S14E01.1080p.WEB.h264-GRP', 'NCIS: Los Angeles'),
                 ('NCIS.Hawaii.S03E01.1080p.WEB.h264-GRP', "NCIS: Hawai'i"),
                 ('Marvels.Agents.of.SHIELD.S07E01.1080p.WEB.h264-GRP', "Marvel's Agents of S.H.I.E.L.D."),
                 ('Its.Always.Sunny.in.Philadelphia.S17E01.1080p.WEB.h264-GRP', "It's Always Sunny in Philadelphia"),
                 ('Dancing.with.the.Stars.US.S34E01.1080p.WEB.h264-GRP', 'Dancing with the Stars'),
                 ('Hells.Kitchen.US.S24E01.1080p.WEB.h264-GRP', "Hell's Kitchen (US)")):
        assert _spin(q, m) == ('match', None), q
    assert not ld.title_diverges('Chicago.Fire.S13E01', 'Chicago Fire')
    assert ld.title_diverges('Chicago.Fire.S13E01', 'Chicago Med')


def test_country_tag_does_not_hide_a_spinoff():
    # Held-out back-test 11-14: all three were imported as matches by 805e51c.
    assert _spin('The.Traitors.US.S05E05.Wake.of.the.Living.Dead.1080p.WEB.h264-Kitsune',
                 'The Traitors: New Blood (2026)') == ('unsure', 'spinoff_title_reverse')
    assert _spin('The Traitors US S05E07 Its Like Fighting Two Pigs 1080p WEB h264-RAWR',
                 'The Traitors: New Blood (2026)') == ('unsure', 'spinoff_title_reverse')
    assert _spin('The.Traitors.New.Blood.S01E08.1080p.WEB.H264-HOTDOGWATER',
                 'The Traitors (US) (2023)') == ('unsure', 'spinoff_title')
    for q, m in (('The.Traitors.US.S05E05.1080p.WEB.h264-GRP', 'The Traitors (US)'),
                 ('The.Traitors.New.Blood.S01E01.1080p.WEB.H264-GRP', 'The Traitors: New Blood'),
                 ('The.Traitors.UK.S03E01.1080p.WEB.h264-GRP', 'The Traitors (UK)'),
                 ('The.Traitors.S03E01.1080p.WEB.h264-GRP', 'The Traitors (US)')):
        assert _spin(q, m) == ('match', None), q


def test_different_country_is_never_a_match():
    assert _spin('The.Traitors.UK.S03E01.1080p.WEB.h264-GRP', 'The Traitors (US)') == ('unsure', 'title_country')
    assert _spin('Ghosts.US.S04E05.1080p.WEB.h264-GRP', 'Ghosts (UK)') == ('unsure', 'title_country')
    assert _spin('This.Is.Us.S06E01.1080p.WEB.h264-GRP', 'This Is Us') == ('match', None)
    assert not ld.country_differs('Ghosts.S04E05.1080p', 'Ghosts (US)')


def test_possessive_brand_left_out_of_the_release_still_matches():
    assert ld.apply_hard_rules('import_identity', 'match', {
        'service': 'Radarr', 'queue_title': 'Without Remorse 2021 2160p AMZN WEB-DL DDP5 1 HDR H 265-GRP',
        'matched_title': "Tom Clancy's Without Remorse", 'year_matches': True}) == ('match', None)
    assert _spin('Jack.Ryan.S04E01.1080p.WEB.h264-GRP', "Tom Clancy's Jack Ryan") == ('match', None)
    assert _spin('Agents.of.S.H.I.E.L.D.S07E01.1080p.WEB.h264-GRP', "Marvel's Agents of S.H.I.E.L.D.") == ('match', None)
    # One word after the brand is too little to trust; a different title after it is still refused.
    assert _spin('Daredevil.S01E01.1080p.WEB.h264-GRP', "Marvel's Daredevil") == ('unsure', 'title_first_word')
    assert _spin('Anatomy.of.a.Scandal.S01E01.1080p.WEB.h264-GRP', "Grey's Anatomy") == ('unsure', 'title_first_word')
    # A real-world refusal that stays: the release adds a word in front, the brand is not possessive.
    assert _spin('Apollo.Has.Fallen.S02E04.1080p.WEB.h264-GRP', 'Has Fallen') == ('unsure', 'title_first_word')
