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
    f = {'first': {'path': LIB}, 'second': {'path': '/media/movies/A (2020)/b.mkv', 'seeding': True, 'seeded_days': 5}}
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


def test_path_prefix_and_traversal_tricks_blocked():
    for bad in ('/media/movies-evil/x.mkv', '/media/movies/../../etc/passwd', '/media/movies', '/media/movies/',
                'media/movies/x.mkv', '/media/tvshows/x', 5):
        f = {'first': {'path': LIB}, 'second': {'path': bad}}
        assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_both', 'not_library_path'), bad


def test_same_file_never_deleted():
    f = {'first': {'path': LIB}, 'second': {'path': '/media/movies/A (2020)/./a.mkv'}}
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_both', 'same_file')


def test_malformed_seed_facts_fail_closed():
    for days in ('5', 'abc', float('nan'), True, [1]):
        f = {'first': {'path': LIB}, 'second': {'path': '/media/tv/S/e.mkv', 'seeding': True, 'seeded_days': days}}
        assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f)[0] == 'keep_both', days
    f = {'first': {'path': LIB}, 'second': {'path': '/media/tv/S/e.mkv', 'seeding': 'true', 'seeded_days': 30},
         'seed_days': 'junk'}
    assert ld.apply_hard_rules('dupe_keeper', 'keep_first', f) == ('keep_first', None)


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
