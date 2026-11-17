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
    assert url == 'http://unraid.invalid:11434/api/chat' and kw['timeout'] == (2.0, 120.0)  # nothing loaded: cold
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
    import llm_advisor as la
    for answer, reason in cases:
        la._UNRAID_HEALTH.clear()  # a timeout starts the back-off; each case starts clean
        http = world(answer, chat('match', 0.9))
        out, info, rows = go(fresh(tmp_path, reason), http)
        assert out == 'match' and info['layer'] == 'local' and rows[-1]['unraid']['pass'] == reason


def test_only_configured_kinds_use_unraid(tmp_path):
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, info, _ = run(fresh(tmp_path, 'a'), Clock(), http, env=dict(UENV, LLM_UNRAID_KINDS='import_identity'))
    assert http.n('unraid.invalid') == 0 and info['layer'] == 'local'
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, info, _ = run(fresh(tmp_path, 'b'), Clock(), http, env=dict(UENV, LLM_UNRAID_KINDS='import_identity, upgrade_gate'))
    assert info['layer'] == 'unraid'


def test_upgrade_gate_uses_unraid_by_default(tmp_path):
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, info, _ = run(tmp_path, Clock(), http, env=UENV)
    assert info['layer'] == 'unraid' and http.n('local.invalid') == 0


UONLY = {'UNRAID_OLLAMA_URL': 'http://unraid.invalid:11434', 'DARKBLOOM_CLOUD_API_KEY': 'k-paid'}


def test_unraid_only_failure_waits_before_paid(tmp_path):
    clock = Clock()
    http = world(Timeout('slow'))
    kw = dict(env=UONLY, kind='import_identity', rules='match', facts=ID)
    out, info, rows = run(tmp_path, clock, http, **kw)
    assert info['pending'] is True and out == 'unsure' and http.n('api.darkbloom.dev') == 0
    assert rows[-1]['unraid']['pass'] == 'timeout'
    clock.minutes(10)
    out, info, _ = run(tmp_path, clock, http, **kw)
    assert info['pending'] is True and http.n('api.darkbloom.dev') == 0
    clock.minutes(6)
    out, info, _ = run(tmp_path, clock, http, **kw)
    assert info['layer'] == 'paid' and http.n('api.darkbloom.dev') == 1


def test_unraid_only_answer_after_a_miss_clears_the_clock(tmp_path):
    clock = Clock()
    kw = dict(env=dict(UONLY, LLM_UNRAID_BACKOFF_MIN='4'), kind='import_identity', rules='match', facts=ID)
    run(tmp_path, clock, world(Timeout('slow')), **kw)
    clock.minutes(5)  # past the timeout's back-off
    out, info, _ = run(tmp_path, clock, world(ollama('mismatch', 0.95)), **kw)
    assert info['layer'] == 'unraid' and out == 'mismatch'
    assert json.loads((tmp_path / 'llm_pending.json').read_text()) == {}


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


def _chat_timeout(http):
    (_, _, kw), = [c for c in http.calls if c[1].endswith('/api/chat')]
    return kw['timeout']


def ask(tmp_path, name, clock, http, env=UENV):
    return run(fresh(tmp_path, name), clock, http, env=env, kind='import_identity', rules='match', facts=ID)


def test_cold_load_gets_the_long_timeout_warm_keeps_30s(tmp_path):
    clock = Clock()
    http = world(ollama('match', 0.95))
    ask(tmp_path, 'cold', clock, http)
    assert _chat_timeout(http) == (2.0, 120.0)
    # Listed and answered a minute ago: warm.
    clock.minutes(1)
    http = world(ollama('match', 0.95), loaded=('qwen3:14b',))
    ask(tmp_path, 'warm', clock, http)
    assert _chat_timeout(http) == (2.0, 30.0)
    # Listed, but its last answer is past Ollama's keep-alive: the listing may be a load in progress.
    clock.minutes(6)
    http = world(ollama('match', 0.95), loaded=('qwen3:14b',))
    ask(tmp_path, 'stale', clock, http)
    assert _chat_timeout(http) == (2.0, 120.0)
    http = world(ollama('match', 0.95))
    go(fresh(tmp_path, 'env'), http, env=dict(UENV, LLM_UNRAID_COLD_TIMEOUT='200'))
    assert _chat_timeout(http) == (2.0, 200.0)


def test_listed_but_never_answered_is_cold(tmp_path):
    # 11-16/17: a timed-out load stays listed in /api/ps; the next ask got 30 s and aborted the load again.
    http = world(ollama('match', 0.95), loaded=('qwen3:14b',))
    go(tmp_path, http)
    assert _chat_timeout(http) == (2.0, 120.0)


def test_timeout_backs_off_doubling_until_an_answer(tmp_path):
    clock = Clock()
    for i, wait_min in enumerate((15, 30, 60, 60)):
        http = world(Timeout('slow'), chat('match', 0.9), loaded=('qwen3:14b',))
        _, _, rows = ask(tmp_path, 'to%d' % i, clock, http)
        assert rows[-1]['unraid']['pass'] == 'timeout'
        # Inside the back-off: no /api/ps, no /api/chat (no model load started), passed on to local.
        clock.minutes(wait_min - 1)
        http = world(ollama('match', 0.95), chat('match', 0.9))
        _, info, rows = ask(tmp_path, 'in%d' % i, clock, http)
        assert http.n('unraid.invalid') == 0 and info['layer'] == 'local' and rows[-1]['unraid']['pass'] == 'backoff'
        clock.minutes(1)
    # Back-off over: asked again; the answer clears the count, so the next timeout backs off 15 min again.
    http = world(ollama('match', 0.95))
    _, info, _ = ask(tmp_path, 'ok', clock, http)
    assert info['layer'] == 'unraid'
    ask(tmp_path, 'to-again', clock, world(Timeout('slow'), chat('match', 0.9)))
    clock.minutes(15)
    http = world(ollama('match', 0.95))
    _, info, _ = ask(tmp_path, 'after', clock, http)
    assert info['layer'] == 'unraid'


def test_backoff_is_shared_by_kinds_and_can_be_turned_off(tmp_path):
    clock = Clock()
    env = dict(UENV, LLM_UNRAID_KINDS='import_identity,upgrade_gate')
    ask(tmp_path, 'to', clock, world(Timeout('slow'), chat('match', 0.9)), env=env)
    http = world(ollama('allow', 0.95), chat('allow', 0.9))
    _, _, rows = run(fresh(tmp_path, 'ug'), clock, http, env=env, kind='upgrade_gate', rules='allow',
                     facts={'title': 'T', 'seeders': 50})
    assert http.n('unraid.invalid') == 0 and rows[-1]['unraid']['pass'] == 'backoff'
    import llm_advisor as la
    la._UNRAID_HEALTH.clear()
    off = dict(UENV, LLM_UNRAID_BACKOFF_MIN='0')
    ask(tmp_path, 'to-off', clock, world(Timeout('slow'), chat('match', 0.9)), env=off)
    http = world(ollama('match', 0.95))
    _, info, _ = ask(tmp_path, 'off', clock, http, env=off)
    assert info['layer'] == 'unraid'


def test_grab_time_kind_keeps_30s_even_cold(tmp_path):
    http = world(ollama('allow', 0.95), extra={'unraid.invalid:11434/api/chat': ollama('allow', 0.95)})
    run(tmp_path, Clock(), http, env=dict(UENV, LLM_UNRAID_KINDS='import_identity,upgrade_gate'), kind='upgrade_gate',
        rules='allow', facts={'title': 'T', 'seeders': 50})
    assert _chat_timeout(http) == (2.0, 30.0)
