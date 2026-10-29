import llm_decide as ld

LIB = '/media/movies/A (2020)/a.mkv'


def test_dv_p5_web_rejected():
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'candidate_dv_web': True}) == ('reject', 'dv_p5_web')


def test_min_seeders():
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': 3}) == ('reject', 'min_seeders')
    assert ld.apply_hard_rules('upgrade_gate', 'allow', {'seeders': 10}) == ('allow', None)


def test_identity_year_mismatch_never_match():
    assert ld.apply_hard_rules('import_identity', 'match', {'year_matches': False}) == ('unsure', 'year_mismatch')


def test_dupe_never_deletes_seeding_inside_window():
    f = {'first': {'path': LIB}, 'second': {'path': LIB, 'seeding': True, 'seeded_days': 5}}
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_both', 'seed_window')
    f['second']['seeded_days'] = 30
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_first', None)


def test_dupe_only_library_paths_and_never_raises():
    f = {'first': {'path': LIB}, 'second': {'path': '/data/Downloads/Just4Seeding/x.mkv'}}
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_both', 'not_library_path')
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', None) == ('keep_both', 'not_library_path')
    assert ld.apply_hard_rules('nope', 'x', object()) == ('x', None)


def test_prompt_carries_policy():
    assert 'Policy:' in ld.build_prompt('dupe_keeper', {}) and 'seed' in ld.build_prompt('dupe_keeper', {})
