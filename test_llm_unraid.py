"""Unraid Ollama layer: tried first for its kinds, only when the GPU is free; only a decisive, confident answer
(after hard rules) settles; everything else passes on to local Darkbloom without touching the pending clock."""
import json

from test_llm_chain import ENV, GOOD, Clock, FakeClaude, FakeHttp, Resp, chat, run

UENV = dict(ENV, UNRAID_OLLAMA_URL='http://unraid.invalid:11434')
ID = {'service': 'Sonarr', 'queue_title': 'Some.Show.S01E01.1080p.WEB-GRP', 'matched_title': 'Some Show', 'seeders': 5}
SPIN = {'service': 'Sonarr', 'queue_title': 'NCIS.Origins.S03E05.1080p.WEB.H264-CAKES', 'matched_title': 'NCIS'}


def ollama(action, conf=0.9):
    return Resp(200, {'message': {'content': GOOD % (action, conf)}, 'prompt_eval_count': 50, 'eval_count': 9})


def ps(*names):
    return Resp(200, {'models': [{'name': n, 'model': n} for n in names]})


class Timeout(Exception):
    pass


Timeout.__name__ = 'ReadTimeout'


def world(unraid_answer, local_answer=None, loaded=(), extra=None):
    routes = {'unraid.invalid:11434/api/ps': ps(*loaded), 'unraid.invalid:11434/api/chat': unraid_answer,
              'local.invalid': local_answer if local_answer is not None else ConnectionError('down'),
              'api.darkbloom.dev': chat('match')}
    routes.update(extra or {})
    return FakeHttp(routes)


def fresh(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def go(tmp_path, http, env=UENV, facts=ID, claude=None, **kw):
    return run(tmp_path, Clock(), http, env=env, claude=claude, kind='import_identity', rules='match', facts=facts, **kw)


def test_confident_match_settles_on_unraid_with_the_tested_request_shape(tmp_path):
    http = world(ollama('match', 0.95))
    out, info, rows = go(tmp_path, http)
    assert out == 'match' and info['layer'] == 'unraid' and info['pending'] is False
    assert http.n('local.invalid') == 0
    (_, url, kw), = [c for c in http.calls if c[1].endswith('/api/chat')]
    assert url == 'http://unraid.invalid:11434/api/chat' and kw['timeout'] == (2.0, 30.0)
    body = kw['json']
    assert body['model'] == 'qwen3:14b' and body['stream'] is False and body['think'] is False
    assert body['options'] == {'temperature': 0.2, 'num_predict': 1024}
    assert rows[-1]['layer'] == 'unraid' and rows[-1]['model'] == 'qwen3:14b'
    assert rows[-1]['unraid']['pass'] is None and isinstance(rows[-1]['unraid']['latency_ms'], int)
    assert not (tmp_path / 'llm_pending.json').exists() or json.loads((tmp_path / 'llm_pending.json').read_text()) == {}


def test_confident_mismatch_settles_on_unraid(tmp_path):
    out, info, _ = go(tmp_path, world(ollama('mismatch', 0.9)))
    assert out == 'mismatch' and info['layer'] == 'unraid'


def test_unsure_passes_on_to_local(tmp_path):
    http = world(ollama('unsure', 0.9), chat('match', 0.95))
    out, info, rows = go(tmp_path, http)
    assert out == 'match' and info['layer'] == 'local' and http.n('local.invalid') == 1
    assert rows[-1]['unraid']['pass'] == 'unsure'


def test_hard_rule_unsure_passes_on_to_local(tmp_path):
    http = world(ollama('match', 0.97), chat('mismatch', 0.9))
    out, info, rows = go(tmp_path, http, facts=SPIN)
    assert out == 'mismatch' and info['layer'] == 'local' and rows[-1]['unraid']['pass'] == 'unsure'


def test_weak_answer_goes_to_local_not_claude(tmp_path):
    claude = FakeClaude('match')
    http = world(ollama('match', 0.6), chat('match', 0.9))
    out, info, rows = go(tmp_path, http, claude=claude)
    assert out == 'match' and info['layer'] == 'local' and claude.calls == []
    assert rows[-1]['unraid']['pass'] == 'weak'


def test_weak_answer_with_local_down_waits_for_local(tmp_path):
    claude = FakeClaude('match')
    http = world(ollama('match', 0.6))
    out, info, rows = go(tmp_path, http, claude=claude)
    assert out == 'unsure' and info['pending'] is True
    assert claude.calls == [] and http.n('api.darkbloom.dev') == 0
    assert rows[-1]['layer'] == 'pending' and rows[-1]['unraid']['pass'] == 'weak'


def test_unraid_answer_settles_while_local_is_down_and_starts_no_clock(tmp_path):
    out, info, _ = go(tmp_path, world(ollama('match', 0.9)))
    assert out == 'match' and info['pending'] is False
    p = tmp_path / 'llm_pending.json'
    assert not p.exists() or json.loads(p.read_text()) == {}


def test_other_resident_model_skips_without_asking(tmp_path):
    http = world(ollama('match', 0.95), chat('match', 0.9), loaded=('qwen3:8b',))
    out, info, rows = go(tmp_path, http)
    assert http.n('/api/chat') == 0 and info['layer'] == 'local'
    assert rows[-1]['unraid']['pass'] == 'other-model-resident'


def test_own_model_resident_is_asked(tmp_path):
    for i, loaded in enumerate((('qwen3:14b',), ('qwen3:14b', 'nomic-embed-text:latest'))):
        http = world(ollama('match', 0.95), loaded=loaded)
        _, info, _ = go(fresh(tmp_path, str(i)), http)
        assert info['layer'] == 'unraid' and http.n('/api/chat') == 1


def test_untagged_model_name_matches_latest():
    import llm_advisor as la
    assert la._model_tag('qwen3') == la._model_tag('qwen3:latest') == 'qwen3:latest'
    assert la._model_tag('hf.co/org/model:Q4') == 'hf.co/org/model:Q4'


def test_hold_skips_unraid(tmp_path):
    env = dict(UENV, LLM_UNRAID_HOLD_URL='http://hold.invalid/u', LLM_UNRAID_HOLD_TOKEN='tok')
    http = world(ollama('match', 0.95), chat('match', 0.9),
                 extra={'hold.invalid/u': Resp(200, {'hold': True, 'cause': 'gpu job'})})
    _, info, rows = go(tmp_path, http, env=env)
    assert http.n('/api/chat') == 0 and http.n('/api/ps') == 0 and info['layer'] == 'local'
    assert rows[-1]['unraid']['pass'] == 'hold' and 'tok' not in json.dumps(rows)


def test_hold_errors_fail_open(tmp_path):
    env = dict(UENV, LLM_UNRAID_HOLD_URL='http://hold.invalid/u')
    http = world(ollama('match', 0.95), extra={'hold.invalid/u': ConnectionError('x')})
    _, info, _ = go(tmp_path, http, env=env)
    assert info['layer'] == 'unraid'


def test_ps_failure_and_timeout_skip(tmp_path):
    for err, reason in ((ConnectionError('refused'), 'error'), (Timeout('slow'), 'timeout')):
        http = world(ollama('match', 0.95), chat('match', 0.9), extra={'unraid.invalid:11434/api/ps': err})
        _, info, rows = go(fresh(tmp_path, reason), http)
        assert http.n('/api/chat') == 0 and info['layer'] == 'local' and rows[-1]['unraid']['pass'] == reason


def test_chat_timeout_error_and_garbage_pass_on(tmp_path):
    cases = ((Timeout('slow'), 'timeout'), (ConnectionError('reset'), 'error'),
             (Resp(200, {'message': {'content': 'no json here'}}), 'unparseable'))
    for answer, reason in cases:
        http = world(answer, chat('match', 0.9))
        out, info, rows = go(fresh(tmp_path, reason), http)
        assert out == 'match' and info['layer'] == 'local' and rows[-1]['unraid']['pass'] == reason


def test_only_configured_kinds_use_unraid(tmp_path):
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, info, _ = run(fresh(tmp_path, 'a'), Clock(), http, env=UENV)
    assert http.n('unraid.invalid') == 0 and info['layer'] == 'local'
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, info, _ = run(fresh(tmp_path, 'b'), Clock(), http, env=dict(UENV, LLM_UNRAID_KINDS='import_identity, upgrade_gate'))
    assert info['layer'] == 'unraid'


def test_model_is_fixed_default_14b_and_8b_only_when_set(tmp_path):
    http = world(ollama('match', 0.95))
    go(fresh(tmp_path, 'a'), http)
    assert [c[2]['json']['model'] for c in http.calls if c[1].endswith('/api/chat')] == ['qwen3:14b']
    assert http.n('/api/tags') == 0
    http = world(ollama('match', 0.95))
    go(fresh(tmp_path, 'b'), http, env=dict(UENV, LLM_UNRAID_MODEL='qwen3:8b'))
    assert [c[2]['json']['model'] for c in http.calls if c[1].endswith('/api/chat')] == ['qwen3:8b']


def test_shadow_asks_unraid_and_local_never_paid_or_claude(tmp_path):
    claude = FakeClaude('match')
    http = world(ollama('unsure', 0.9), chat('match', 0.4))
    run(tmp_path, Clock(), http, env=UENV, claude=claude, kind='import_identity', rules='match', facts=ID, mode='shadow')
    assert http.n('/api/chat') == 1 and http.n('local.invalid') == 1
    assert http.n('api.darkbloom.dev') == 0 and claude.calls == []


def test_unraid_answer_is_cached(tmp_path):
    clock = Clock()
    http = world(ollama('match', 0.95))
    kw = dict(env=UENV, kind='import_identity', rules='match', facts=ID)
    run(tmp_path, clock, http, **kw)
    clock.minutes(30)
    out, info, _ = run(tmp_path, clock, http, **kw)
    assert out == 'match' and info['layer'] == 'cache' and http.n('/api/chat') == 1
    cache = json.loads((tmp_path / 'llm_answers.json').read_text())
    assert [v['layer'] for v in cache.values()] == ['unraid']


def test_skips_raise_no_escalation_records(tmp_path):
    seen = []
    http = world(ollama('match', 0.95), chat('match', 0.9), loaded=('llama3:70b',))
    go(tmp_path, http, record=lambda name, data: seen.append(name))
    assert seen == ['llm-decision']


def test_without_unraid_url_chain_is_unchanged(tmp_path):
    http = world(ollama('match', 0.95), chat('match', 0.9))
    _, info, rows = go(tmp_path, http, env=ENV)
    assert http.n('unraid.invalid') == 0 and info['layer'] == 'local' and 'unraid' not in rows[-1]
