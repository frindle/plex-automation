import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    import llm_decide
    return llm_decide


GOOD = '{"action": "allow", "confidence": 0.9, "reason": "same group, better source"}'


def test_parses_plain_fenced_and_prose_wrapped_json():
    m = _load()
    want = {'action': 'allow', 'confidence': 0.9, 'reason': 'same group, better source'}
    assert m.parse_decision('upgrade_gate', GOOD) == want
    assert m.parse_decision('upgrade_gate', '```json\n' + GOOD + '\n```') == want
    assert m.parse_decision('upgrade_gate', 'Sure! Here you go: ' + GOOD + ' Hope that helps.') == want
    assert m.parse_decision('upgrade_gate', '{oops} then ' + GOOD) == want


def test_rejects_unknown_kind_and_action_outside_kind():
    m = _load()
    assert m.parse_decision('bogus', GOOD) is None
    assert m.parse_decision(None, GOOD) is None
    assert m.parse_decision('import_identity', GOOD) is None  # 'allow' belongs to upgrade_gate
    assert m.parse_decision('upgrade_gate', '{"confidence": 0.9, "reason": "x"}') is None
    assert m.parse_decision('dupe_keeper', '{"action": "keep_second", "confidence": 1, "reason": "r"}')['action'] == 'keep_second'


def test_rejects_bad_confidence_values():
    m = _load()
    tpl = '{"action": "allow", "confidence": %s, "reason": "r"}'
    for bad in ('1.5', '-0.1', 'true', '"0.9"', 'null'):
        assert m.parse_decision('upgrade_gate', tpl % bad) is None, bad
    assert m.parse_decision('upgrade_gate', '{"action": "allow", "reason": "r"}') is None
    assert m.parse_decision('upgrade_gate', tpl % '0')['confidence'] == 0.0
    assert m.parse_decision('upgrade_gate', tpl % '1')['confidence'] == 1.0


def test_reason_must_be_str_and_is_truncated_to_300():
    m = _load()
    assert m.parse_decision('upgrade_gate', '{"action": "allow", "confidence": 0.5}') is None
    assert m.parse_decision('upgrade_gate', '{"action": "allow", "confidence": 0.5, "reason": 7}') is None
    long = json.dumps({'action': 'reject', 'confidence': 0.8, 'reason': 'x' * 500})
    got = m.parse_decision('upgrade_gate', long)
    assert got is not None and len(got['reason']) == 300


def test_never_raises_on_junk_input():
    m = _load()
    for raw in (None, b'', '', 5, 3.2, [], {}, '[1, 2]', '"str"', 'no json here', '{', '{"action": ', b'\xff\xfe'):
        assert m.parse_decision('upgrade_gate', raw) is None, raw
    assert m.parse_decision('upgrade_gate', GOOD.encode())['action'] == 'allow'
