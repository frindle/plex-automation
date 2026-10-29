import json

import llm_advisor as la

GOOD = '{"action": "%s", "confidence": %s, "reason": "why"}'
LIB = '/media/movies/A (2020)/a.mkv'


def _t(action, conf=0.9, calls=None):
    def t(prompt):
        if calls is not None:
            calls.append(prompt)
        return GOOD % (action, conf)
    return t


def _lines(p):
    return [json.loads(x) for x in open(p).read().splitlines()]


def test_off_by_default_no_transport_no_log(tmp_path):
    calls, log = [], tmp_path / 'l.jsonl'
    out = la.advise('upgrade_gate', {}, 'allow', transport=_t('reject', calls=calls), log_path=str(log), env={})
    assert out == 'allow' and calls == [] and not log.exists()


def test_shadow_keeps_rules_action_and_logs_disagreement(tmp_path):
    log = tmp_path / 'l.jsonl'
    out = la.advise('upgrade_gate', {'title': 'T'}, 'allow', mode='shadow', transport=_t('reject'), log_path=str(log))
    assert out == 'allow'
    rec = _lines(log)[0]
    assert rec['kind'] == 'upgrade_gate' and rec['llm_action'] == 'reject' and rec['agree'] is False and rec['mode'] == 'shadow'


def test_enforce_follows_confident_model_not_weak_one(tmp_path):
    log = str(tmp_path / 'l.jsonl')
    assert la.advise('upgrade_gate', {}, 'allow', mode='enforce', transport=_t('throttle', 0.9), log_path=log) == 'throttle'
    assert la.advise('upgrade_gate', {}, 'allow', mode='enforce', transport=_t('throttle', 0.5), log_path=log) == 'allow'


def test_enforce_hard_rule_vetoes_model_and_is_logged(tmp_path):
    log = tmp_path / 'l.jsonl'
    facts = {'first': {'path': LIB}, 'second': {'path': LIB.replace('a.mkv', 'b.mkv'), 'seeding': True, 'seeded_days': 5}}
    out = la.advise('dupe_keeper', facts, 'keep_both', mode='enforce', transport=_t('keep_first'), log_path=str(log))
    assert out == 'keep_both'
    assert 'seed_window' in _lines(log)[0]['reason']


def test_failures_never_propagate(tmp_path):
    def boom(prompt):
        raise RuntimeError('down')

    def bad_record(*a, **k):
        raise RuntimeError('x')
    assert la.advise('upgrade_gate', {}, 'allow', mode='enforce', transport=boom, log_path=str(tmp_path / 'l'), record=bad_record) == 'allow'
    got = []
    la.advise('upgrade_gate', {}, 'allow', mode='shadow', transport=_t('allow'), log_path=str(tmp_path / 'l'), record=lambda *a: got.append(a))
    assert len(got) == 1 and got[0][0] == 'llm-decision'
