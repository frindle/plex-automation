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
