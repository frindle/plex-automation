"""Backtest fact capture in advise() and the default facts file in backtest_local (synthetic, no network)."""
import json
import os
import stat

import backtest_local as bl
import llm_advisor as la

GOOD = '{"action": "allow", "confidence": 0.9, "reason": "r"}'


def env_for(tmp_path, **kw):
    e = {'LLM_BACKTEST_DIR': str(tmp_path / 'bt'), 'LLM_PENDING_PATH': str(tmp_path / 'p.json'),
         'LLM_DECISIONS_LOG': str(tmp_path / 'log.jsonl'), 'LLM_LOCAL_WAIT_MIN': '0'}
    e.update(kw)
    return e


def run(kind, facts, env, tmp_path):
    layers = {'local': la.Layer('local', lambda p: GOOD, 'm-local')}
    return la.advise(kind, facts, 'allow', mode='shadow', env=env, layers=layers, log_path=env['LLM_DECISIONS_LOG'])


def rows_of(tmp_path, kind):
    with open(tmp_path / 'bt' / ('%s-live.jsonl' % kind), encoding='utf-8') as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_capture_kind_written_other_kind_not(tmp_path):
    env = env_for(tmp_path)
    run('upgrade_gate', {'size_gb': 12, 'q': 'x' * 900}, env, tmp_path)
    run('dupe_confirm', {'size_gb': 3}, env, tmp_path)
    r = rows_of(tmp_path, 'upgrade_gate')
    assert len(r) == 1 and r[0]['facts']['q'] == 'x' * 900 and r[0]['rules_action'] == 'allow'
    assert len(r[0]['id']) == 12 and isinstance(r[0]['ts'], int)
    assert not (tmp_path / 'bt' / 'dupe_confirm-live.jsonl').exists()


def test_duplicate_id_not_written_twice(tmp_path):
    env = env_for(tmp_path)
    for _ in range(3):
        assert la.capture_facts('upgrade_gate', {'a': 1}, 'allow', env) in (True, False)
    assert len(rows_of(tmp_path, 'upgrade_gate')) == 1


def test_cap_drops_oldest(tmp_path):
    env = env_for(tmp_path, LLM_CAPTURE_MAX='3')
    for i in range(5):
        la.capture_facts('import_identity', {'n': i}, 'allow', env)
    assert [r['facts']['n'] for r in rows_of(tmp_path, 'import_identity')] == [2, 3, 4]


def test_modes(tmp_path):
    env = env_for(tmp_path)
    la.capture_facts('upgrade_gate', {'a': 1}, 'allow', env)
    d = tmp_path / 'bt'
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / 'upgrade_gate-live.jsonl').st_mode) == 0o600
    assert not [n for n in os.listdir(d) if '.tmp' in n]


def test_write_error_does_not_break_advise(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    env = env_for(tmp_path, LLM_BACKTEST_DIR=str(blocker / 'sub'))
    assert run('upgrade_gate', {'a': 1}, env, tmp_path) == 'allow'
    assert la.capture_facts('upgrade_gate', {'a': 1}, 'allow', env) is False


def test_empty_kinds_writes_nothing(tmp_path):
    env = env_for(tmp_path, LLM_CAPTURE_KINDS='')
    run('upgrade_gate', {'a': 1}, env, tmp_path)
    assert not (tmp_path / 'bt').exists()


def test_default_facts_order(tmp_path):
    d = tmp_path / 'bt'
    d.mkdir()
    env = {'LLM_BACKTEST_DIR': str(d)}
    assert bl.default_facts('upgrade_gate', env)[1] == 'plain'
    (d / 'upgrade_gate-20250101.jsonl').write_text('{}\n')
    (d / 'upgrade_gate-20250301.jsonl').write_text('{}\n')
    p, w = bl.default_facts('upgrade_gate', env)
    assert w == 'stamped' and p.endswith('-20250301.jsonl')
    (d / 'upgrade_gate-live.jsonl').write_text('{}\n')
    assert bl.default_facts('upgrade_gate', env)[1] == 'live'
    (d / 'upgrade_gate.jsonl').write_text('{}\n')
    assert bl.default_facts('upgrade_gate', env)[1] == 'plain'


def test_main_prints_label_not_path(tmp_path, capsys):
    d = tmp_path / 'bt'
    d.mkdir()
    (d / 'upgrade_gate-live.jsonl').write_text(json.dumps({'facts': {'a': 1}, 'id': 'r0'}) + '\n')
    bl.main(['--kind', 'upgrade_gate', '--layer', 'claude'], env={'LLM_BACKTEST_DIR': str(d)}, http=None)
    out = capsys.readouterr().out
    print_ok = True
    assert 'facts: live' in out and str(d) not in out
