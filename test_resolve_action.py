import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    import llm_decide
    return llm_decide


def _dec(action='reject', conf=0.99):
    return {'action': action, 'confidence': conf, 'reason': 'r'}


def test_shadow_and_off_always_keep_the_rules_action():
    m = _load()
    assert m.resolve_action('upgrade_gate', 'allow', _dec(), 'shadow') == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', _dec()) == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', _dec(), 'off') == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', _dec(), 'bogus') == 'allow'


def test_enforce_follows_model_only_at_or_above_min_confidence():
    m = _load()
    assert m.resolve_action('upgrade_gate', 'allow', _dec('reject', 0.75), 'enforce') == 'reject'
    assert m.resolve_action('upgrade_gate', 'allow', _dec('reject', 0.74), 'enforce') == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', _dec('throttle', 1.0), 'enforce') == 'throttle'
    assert m.resolve_action('dupe_keeper', 'keep_first', _dec('keep_second', 0.8), 'enforce') == 'keep_second'


def test_enforce_with_missing_or_malformed_decision_keeps_rules():
    m = _load()
    for bad in (None, {}, [], 'reject', 5, {'action': 'reject'}, {'confidence': 0.99}):
        assert m.resolve_action('upgrade_gate', 'allow', bad, 'enforce') == 'allow', bad


def test_enforce_ignores_action_outside_kind_and_bool_confidence():
    m = _load()
    assert m.resolve_action('upgrade_gate', 'allow', _dec('keep_first', 0.99), 'enforce') == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', {'action': 'reject', 'confidence': True, 'reason': ''}, 'enforce') == 'allow'
    assert m.resolve_action('upgrade_gate', 'allow', {'action': 'reject', 'confidence': '0.99', 'reason': ''}, 'enforce') == 'allow'


def test_unknown_kind_keeps_rules_action_and_never_raises():
    m = _load()
    assert m.resolve_action('bogus', 'x', _dec(), 'enforce') == 'x'
    assert m.resolve_action(None, 'x', _dec(), 'enforce') == 'x'
    assert m.resolve_action('upgrade_gate', None, None, 'enforce') is None
