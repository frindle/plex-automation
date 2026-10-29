import llm_decide as ld


def _r(a='reject'):
    return '{"action": "%s", "confidence": 0.9, "reason": "r"}' % a


def test_thinking_braces_before_answer():
    raw = 'Let me weigh {"action": "allow"} maybe... {x} \n' + _r()
    assert ld.parse_decision('upgrade_gate', raw)['action'] == 'reject'


def test_think_block_stripped():
    raw = '<think>{"action": "allow", "confidence": 1, "reason": "x"}</think>' + _r('throttle')
    assert ld.parse_decision('upgrade_gate', raw)['action'] == 'throttle'


def test_unclosed_think_still_parses_tail():
    assert ld.parse_decision('upgrade_gate', 'hmm {not json} ' + _r('allow'))['action'] == 'allow'


def test_nan_and_infinite_confidence_rejected():
    for c in ('NaN', 'Infinity', '-Infinity', '1e999'):
        raw = '{"action": "allow", "confidence": %s, "reason": "r"}' % c
        assert ld.parse_decision('upgrade_gate', raw) is None, c


def test_trailing_invalid_object_does_not_hide_answer():
    assert ld.parse_decision('upgrade_gate', _r() + ' e.g. {"action": "maybe"}')['action'] == 'reject'


def test_stray_close_think_prefers_tail_and_truncated_think_is_ignored():
    raw = 'thinking {"action": "allow", "confidence": 1, "reason": "x"}</think>\n```json\n' + _r() + '\n```'
    assert ld.parse_decision('upgrade_gate', raw)['action'] == 'reject'
    assert ld.parse_decision('upgrade_gate', '<think>maybe ' + _r('allow')) is None


def test_resolve_action_rejects_nan_and_never_raises():
    nan = {'action': 'allow', 'confidence': float('nan'), 'reason': ''}
    assert ld.resolve_action('upgrade_gate', 'reject', nan, 'enforce') == 'reject'
    assert ld.resolve_action(['x'], 'a', {}, 'enforce') == 'a'
    assert ld.resolve_action('upgrade_gate', 'a', {'action': ['allow'], 'confidence': 1}, 'enforce') == 'a'


def test_log_decision_never_raises(tmp_path):
    p = str(tmp_path / 'd.jsonl')
    assert ld.log_decision(p, 'k', {}, 'a', ['x'], 'm') is True
    circ = {}
    circ['self'] = circ
    assert ld.log_decision(p, 'k', circ, 'a', None, 'm') is True
    assert ld.log_decision(p, 'k', {(1, 2): 'tuple key'}, 'a', None, 'm') is True
    assert ld.log_decision(True, 'k', {}, 'a', None, 'm') is False
    assert ld.log_decision(1, 'k', {}, 'a', None, 'm') is False
