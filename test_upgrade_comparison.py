import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_decide as ld


def test_same_codec_much_bigger_pack_is_called_a_higher_bitrate_encode():
    c = ld.upgrade_comparison({'quality': 'Bluray-1080p', 'size_gb': 70.12, 'codec': 'hevc', 'group': 'd3g',
                               'existing_quality': 'Bluray-1080p', 'existing_size_gb': 10.76,
                               'existing_codec': 'x265', 'existing_group': 'iVy'})
    assert 'resolution same (1080p vs 1080p)' in c
    assert 'source tier same' in c
    assert 'codec x265 -> x265' in c
    assert 'size 6.52 times' in c and 'higher-bitrate' in c
    assert 'release group different' in c


def test_bluray_over_web_dv_is_a_better_source_tier():
    c = ld.upgrade_comparison({'quality': 'Bluray-2160p', 'size_gb': 35.55, 'codec': 'x265', 'hdr': True,
                               'existing_quality': 'WEBDL-2160p', 'existing_size_gb': 27.73,
                               'existing_codec': 'h265', 'existing_hdr': 'DV'})
    assert 'source tier better' in c
    assert 'a little bigger' in c
    assert 'HDR' not in c  # both sides have HDR of some kind


def test_web_tiers_are_one_tier_and_codecs_normalise():
    c = ld.upgrade_comparison({'quality': 'HDTV-1080p', 'codec': 'h.265', 'existing_quality': 'WEBDL-1080p',
                               'existing_codec': 'AVC'})
    assert 'source tier same (WEB/HDTV vs WEB/HDTV)' in c
    assert 'codec x264 -> x265' in c
    assert 'size' not in c  # no sizes, no size claim


def test_smaller_and_same_size_wording():
    base = {'quality': 'WEBDL-1080p', 'existing_quality': 'WEBDL-1080p'}
    assert 'smaller: saves space' in ld.upgrade_comparison({**base, 'size_gb': 2.81, 'existing_size_gb': 4.44})
    assert 'about the same size' in ld.upgrade_comparison({**base, 'size_gb': 4.67, 'existing_size_gb': 4.67})


def test_partial_pack_compares_per_episode():
    # 10-episode pack, only 5 episodes have a file: 20 GB / 10 vs 5 GB / 5 = 2.0 per episode, not 4.0.
    c = ld.upgrade_comparison({'quality': 'WEBRip-1080p', 'existing_quality': 'WEBRip-1080p', 'size_gb': 20,
                               'existing_size_gb': 5, 'episodes': 10, 'episodes_with_file': 5})
    assert 'size 2.00 times' in c


def test_unknown_or_bad_values_say_nothing():
    assert ld.upgrade_comparison({}) == ''
    assert ld.upgrade_comparison(None) == ''
    assert ld.upgrade_comparison({'size_gb': None, 'existing_size_gb': 3}) == ''
    assert ld.upgrade_comparison({'size_gb': 'big', 'existing_size_gb': 3}) == ''
    assert ld.upgrade_comparison({'size_gb': 0, 'existing_size_gb': 3}) == ''
    assert ld.upgrade_comparison({'quality': 'Unknown', 'existing_quality': 'WEBDL-1080p'}) == ''


def test_hdr_gain_and_loss_and_av1_and_repack():
    gain = ld.upgrade_comparison({'hdr': True, 'existing_hdr': None, 'quality': 'WEBDL-2160p',
                                  'existing_quality': 'WEBDL-2160p'})
    assert 'HDR gained' in gain
    loss = ld.upgrade_comparison({'hdr': False, 'existing_hdr': 'HDR10', 'quality': 'WEBDL-2160p',
                                  'existing_quality': 'WEBDL-2160p'})
    assert 'HDR lost (existing file has HDR10)' in loss
    assert 'codec AV1 -> x265' in ld.upgrade_comparison({'codec': 'x265', 'existing_codec': 'AV1'})
    assert 'PROPER/REPACK' in ld.upgrade_comparison({'title': 'Show.S01E01.REPACK.1080p.WEB.h264-GRP'})
    assert ld.upgrade_comparison({'title': 'Show.Repackaged.S01E01.1080p'}) == ''


def test_prompt_carries_comparison_only_for_upgrade_gate():
    ctx = {'quality': 'Bluray-1080p', 'existing_quality': 'WEBDL-1080p'}
    p = ld.build_prompt('upgrade_gate', ctx)
    assert 'Comparison (computed from the facts' in p and 'source tier better' in p
    assert p.index('Context:') < p.index('Comparison (computed') < p.index('Respond ONLY')
    assert 'Comparison' not in ld.build_prompt('import_identity', ctx)
    assert 'Comparison (computed' not in ld.build_prompt('upgrade_gate', {'title': 'x'})


def test_policy_scopes_rule_one_to_the_candidate_and_throttles_group_changes():
    pol = ld.POLICY['upgrade_gate']
    assert 'Only the candidate counts' in pol
    assert '6. Throttle' in pol and 'different release group' in pol
    assert 'AV1' in pol and 'REPACK' in pol
