import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_decide as ld

HEAD = 'Comparison (computed from the facts; trust it over your own reading): '


def _f(service, qt, mt, my, ym=None):
    return {'service': service, 'queue_title': qt, 'matched_title': mt, 'matched_year': my, 'year_matches': ym}


def test_year_conflict_is_stated():
    c = ld.identity_comparison(_f('Radarr', 'Dune.2021.1080p.WEB-DL.x265-GRP', 'Dune', 1984, False))
    assert c.startswith(HEAD) and 'release year 2021 is not the matched year 1984' in c


def test_exact_name_with_agreeing_or_one_off_year():
    agree = ld.identity_comparison(_f('Radarr', 'Dune.2021.1080p.WEB-DL.x265-GRP', 'Dune', 2021, True))
    assert 'the release names exactly the matched title' in agree and 'year 2021 agrees' in agree
    off = ld.identity_comparison(_f('Radarr', 'Dune.2022.1080p.WEB-DL.x265-GRP', 'Dune', 2021, False))
    assert 'one off the matched year 2021: not a year mismatch' in off and 'is not the matched year' not in off


def test_sonarr_exact_name_without_a_year():
    c = ld.identity_comparison(_f('Sonarr', 'Severance.S02E01.1080p.WEB.h264-GRP', 'Severance', 2022))
    assert 'names exactly the matched title' in c and 'carries no year (normal for Sonarr' in c


def test_partial_agreement_says_nothing():
    # Same year or a shared word is not agreement: the model judges these alone and the hard rules still run.
    assert ld.identity_comparison(_f('Sonarr', 'Fear.the.Walking.Dead.S01E01.1080p.WEB.h264-GRP',
                                     'The Walking Dead', 2010)) == ''
    assert ld.identity_comparison(_f('Radarr', 'Dune.Part.Two.2024.1080p.WEB-DL.x265-GRP', 'Dune', 2024,
                                     True)) == ''


def test_country_tags():
    differ = ld.identity_comparison(_f('Sonarr', 'The.Office.UK.S01E01.1080p.WEB.h264-GRP', 'The Office (US)', 2005))
    assert 'country tags differ (release UK, matched title US)' in differ
    rel_only = ld.identity_comparison(_f('Sonarr', 'The.Diplomat.US.S04E01.1080p.WEB.h264-GRP', 'The Diplomat', 2023))
    assert 'apart from the country tag US, which only the release carries' in rel_only
    mt_only = ld.identity_comparison(_f('Sonarr', 'Ghosts.S05E01.1080p.WEB.h264-GRP', 'Ghosts (US)', 2021))
    assert 'apart from the country tag US, which only the matched title carries' in mt_only


def test_bad_input_says_nothing():
    assert ld.identity_comparison(None) == ''
    assert ld.identity_comparison({'queue_title': 5, 'matched_title': 'x'}) == ''


def test_identity_prompt_carries_the_line_and_the_policy_explains_its_absence():
    p = ld.build_prompt('import_identity', _f('Radarr', 'Dune.2021.1080p.WEB-DL.x265-GRP', 'Dune', 2021, True))
    assert p.index('Context:') < p.index(HEAD.strip()) < p.index('Respond ONLY')
    assert 'no Comparison line means nothing in it was certain' in ld.POLICY['import_identity']


def test_same_name_spaced_differently_is_not_a_different_first_word():
    q = 'SpiderMan.Brand.New.Day.2026.1080p.WEB-DL.x265-GRP'
    assert not ld.first_word_differs(q, 'Spider-Man: Brand New Day')
    assert ld.apply_hard_rules('import_identity', 'match', {
        'service': 'Radarr', 'queue_title': q, 'matched_title': 'Spider-Man: Brand New Day', 'matched_year': 2026,
        'year_matches': True, 'files': 1}) == ('match', None)
    assert ld.first_word_differs('SpiderWoman.2026.1080p.WEB-DL.x265-GRP', 'Spider-Man')


def test_one_dropped_letter_in_the_release_name_passes_only_with_a_matching_year():
    q = 'ioness.2023.S03.1080p.WEBRip.10Bit.DDP5.1.H265-d3g'  # real release, the group left the L off
    assert not ld.first_word_differs(q, 'Lioness', allow_tail=True)
    assert ld.first_word_differs(q, 'Lioness')  # no matching year: still a different first word
    assert ld.first_word_differs('Ozar.2023.S01.1080p.WEB.h264-GRP', 'Ozark', allow_tail=True) is False
    assert ld.first_word_differs('Lion.2023.S01.1080p.WEB.h264-GRP', 'Lioness', allow_tail=True)  # 3 letters short
    assert ld.first_word_differs('Ioness.Rising.2023.S01.1080p.WEB.h264-GRP', 'Lioness', allow_tail=True)
