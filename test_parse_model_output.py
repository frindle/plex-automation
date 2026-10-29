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
