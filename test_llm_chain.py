import json
import types

import llm_advisor as la

GOOD = '{"action": "%s", "confidence": %s, "reason": "why"}'
FACTS = {'title': 'T', 'size_gb': 12}
KEY = 'eigeninference-fake'


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def minutes(self, m):
        self.t += m * 60


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._d = data
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('http %d' % self.status_code)

    def json(self):
        if self._d is None:
            raise ValueError('bad json')
        return self._d


def chat(action, conf=0.9, usage=True, rid='req-1'):
    d = {'choices': [{'message': {'content': GOOD % (action, conf)}}], 'id': rid}
    if usage:
        d['usage'] = {'prompt_tokens': 100, 'completion_tokens': 10}
    return Resp(200, d)


class FakeHttp:
    """requests stand-in: routes by URL; records calls (never headers' values in asserts)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for frag, handler in self.routes.items():
            if frag in url:
                out = handler() if callable(handler) else handler
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError('unrouted ' + url)

    def get(self, url, **kw):
        if url.endswith('/v1/models') and not any('/v1/models' in f for f in self.routes):
            # a pin must be listed: the default listing (not recorded in calls) holds the test pin
            return Resp(200, {'data': [{'id': 'm-local'}, {'id': 'gemma-4-26b'}]})
        return self._go('GET', url, **kw)

    def post(self, url, **kw):
        return self._go('POST', url, **kw)

    def n(self, frag):
        return sum(1 for _, u, _ in self.calls if frag in u)


class FakeClaude:
    def __init__(self, action='allow', conf=0.95, stop='end_turn'):
        self.calls = []
        self.out = (action, conf, stop)
        self.messages = types.SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        a, c, stop = self.out
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type='text', text=GOOD % (a, c))], stop_reason=stop,
            usage=types.SimpleNamespace(input_tokens=200, output_tokens=20), id='msg_1', _request_id='req_c1')


ENV = {'DARKBLOOM_BASE_URL': 'http://local.invalid', 'DARKBLOOM_API_KEY': 'k', 'LLM_LOCAL_MODEL': 'm-local',
       'DARKBLOOM_CLOUD_API_KEY': KEY, 'ANTHROPIC_API_KEY': 'sk-fake'}


def run(tmp_path, clock, http, env=None, claude=None, kind='upgrade_gate', rules='allow', mode='enforce', facts=None, **kw):
    log = tmp_path / 'l.jsonl'
    info = {}
    env = dict(env or ENV, LLM_PENDING_PATH=str(tmp_path / 'llm_pending.json'),
               LLM_CLAUDE_SPEND_PATH=str(tmp_path / 'claude_spend.json'))
    if claude is None:
        env.pop('ANTHROPIC_API_KEY', None)  # claude now precedes paid: never let a test build the real client
    out = la.advise(kind, facts or FACTS, rules, mode=mode, log_path=str(log), env=env, now=clock, http=http,
                    hold_http=http, claude_client=claude, info=info, **kw)
    rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return out, info, rows


def test_local_down_under_wait_no_paid_call_pending_recorded(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'darkbloom.dev': chat('reject')})
    claude = FakeClaude()
    out, info, rows = run(tmp_path, clock, http, claude=claude)
    assert out == 'allow' and info['pending'] is True
    assert http.n('darkbloom.dev') == 0 and claude.calls == []
    assert rows[0]['layer'] == 'pending'
    pend = json.loads((tmp_path / 'llm_pending.json').read_text())
    assert len(pend) == 1 and list(pend.values())[0]['attempts'] == 1


def test_pending_seam_actions_by_kind_and_mode(tmp_path):
    http = FakeHttp({'local.invalid': ConnectionError('down')})
    assert run(tmp_path, Clock(), http, kind='import_identity', rules='match')[0] == 'unsure'
    assert run(tmp_path, Clock(), http)[0] == 'allow'
    assert run(tmp_path, Clock(), http, kind='import_identity', rules='match', mode='shadow')[0] == 'match'


def test_after_wait_paid_is_called_and_logged_with_usage(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('throttle')})
    run(tmp_path, clock, http)
    clock.minutes(15)
    out, info, rows = run(tmp_path, clock, http)
    assert out == 'throttle' and info['layer'] == 'paid'
    row = rows[-1]
    assert row['layer'] == 'paid' and row['model'] == 'qwen3.5-35b-a3b'
    call = row['paid_calls'][0]
    assert call['prompt_tokens'] == 100 and call['completion_tokens'] == 10 and call['request_id'] == 'req-1'
    paid_post = [c for c in http.calls if 'api.darkbloom.dev' in c[1]][0]
    # No thinking: with it on, the paid model spent the whole budget reasoning and never wrote the JSON.
    assert paid_post[2]['json']['chat_template_kwargs'] == {'enable_thinking': False}
    assert paid_post[2]['json']['max_tokens'] == 2048
    assert paid_post[2]['headers']['Authorization'] == 'Bearer ' + KEY
    assert KEY not in json.dumps(rows)
    assert json.loads((tmp_path / 'llm_pending.json').read_text()) == {}


def test_local_back_at_minute_5_answers_locally_and_clears_entry(tmp_path):
    clock = Clock()
    state = {'up': False}
    http = FakeHttp({'local.invalid': lambda: chat('reject') if state['up'] else ConnectionError('x'),
                     'api.darkbloom.dev': chat('allow')})
    run(tmp_path, clock, http)
    clock.minutes(5)
    state['up'] = True
    out, info, rows = run(tmp_path, clock, http)
    assert out == 'reject' and info['layer'] == 'local' and info['pending'] is False
    assert http.n('api.darkbloom.dev') == 0
    assert json.loads((tmp_path / 'llm_pending.json').read_text()) == {}


def test_low_confidence_local_goes_to_claude_immediately(tmp_path):
    http = FakeHttp({'local.invalid': chat('allow', 0.4)})
    claude = FakeClaude('reject', 0.9)
    out, info, rows = run(tmp_path, Clock(), http, claude=claude)
    assert out == 'reject' and info['layer'] == 'claude'
    assert http.n('darkbloom.dev') == 0
    kw = claude.calls[0]
    assert kw['model'] == 'claude-haiku-5-5' and 'thinking' not in kw
    assert kw['output_config']['format']['schema']['additionalProperties'] is False
    assert kw['output_config']['effort'] == 'low'
    assert rows[0]['paid_calls'][0]['input_tokens'] == 200 and rows[0]['paid_calls'][0]['request_id'] == 'req_c1'


def test_claude_refusal_is_failure_rules_stand(tmp_path):
    http = FakeHttp({'local.invalid': chat('allow', 0.4)})
    out, info, rows = run(tmp_path, Clock(), http, claude=FakeClaude('reject', 0.9, stop='refusal'))
    assert out == 'allow' and rows[0]['layer'] is None or rows[0]['layer'] == 'local'


def test_exhausted_chain_returns_rules(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('x'), 'api.darkbloom.dev': ConnectionError('y')})
    run(tmp_path, clock, http, env={k: v for k, v in ENV.items() if k != 'ANTHROPIC_API_KEY'})
    clock.minutes(16)
    out, info, rows = run(tmp_path, clock, http, env={k: v for k, v in ENV.items() if k != 'ANTHROPIC_API_KEY'})
    assert out == 'allow' and info['layer'] is None


def test_no_free_layer_never_pays(tmp_path):
    # Paid is only a backup for the free layers (Penn, 2026-11-16): with none configured the rules stand.
    http = FakeHttp({'api.darkbloom.dev': chat('reject')})
    claude = FakeClaude('reject')
    out, info, rows = run(tmp_path, Clock(), http, env={'DARKBLOOM_CLOUD_API_KEY': KEY, 'ANTHROPIC_API_KEY': 'sk-fake'},
                          claude=claude)
    assert out == 'allow' and info['layer'] is None and info['pending'] is False
    assert http.n('api.darkbloom.dev') == 0 and claude.calls == []
    assert rows[-1]['error'] == 'no free layer configured' and 'paid_calls' not in rows[-1]


def test_paid_retries_without_template_kwargs_when_rejected(tmp_path):
    clock = Clock()
    bodies = []

    def paid():
        body = http.calls[-1][2]['json']
        bodies.append(dict(body))
        return Resp(400, {'error': 'unknown field'}) if 'chat_template_kwargs' in body else chat('reject')

    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': paid})
    run(tmp_path, clock, http)
    clock.minutes(16)
    out, info, _ = run(tmp_path, clock, http)
    assert out == 'reject' and info['layer'] == 'paid'
    assert len(bodies) == 2 and 'chat_template_kwargs' not in bodies[1] and bodies[1]['max_tokens'] == 2048


def test_paid_server_error_is_not_retried(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': Resp(500, {})})
    env = {k: v for k, v in ENV.items() if k != 'ANTHROPIC_API_KEY'}
    run(tmp_path, clock, http, env=env)
    clock.minutes(16)
    out, info, _ = run(tmp_path, clock, http, env=env)
    assert out == 'allow' and info['layer'] is None and http.n('api.darkbloom.dev') == 1


def test_restart_keeps_first_seen(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('reject')})
    run(tmp_path, clock, http)
    first = list(json.loads((tmp_path / 'llm_pending.json').read_text()).values())[0]['first_seen']
    la._STORES.clear()  # process restart
    clock.minutes(16)
    out, info, _ = run(tmp_path, clock, http)
    assert info['layer'] == 'paid' and first == 1_000_000.0


def _gate(seeders, freeleech='full'):
    return {'title': 'T', 'release': 'T.2021.2160p.BluRay-GRP', 'existing_quality': 'WEBDL-1080p',
            'freeleech': freeleech, 'seeders': seeders}


def test_seeders_tick_keeps_the_pending_clock(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('reject')})
    out, info, _ = run(tmp_path, clock, http, facts=_gate(12))
    assert info['pending'] is True
    clock.minutes(10)
    out, info, _ = run(tmp_path, clock, http, facts=_gate(15))
    assert info['pending'] is True and info['pending_min'] == 10.0
    clock.minutes(6)
    out, info, _ = run(tmp_path, clock, http, facts=_gate(15))
    assert info['layer'] == 'paid' and http.n('api.darkbloom.dev') == 1
    assert len(json.loads((tmp_path / 'llm_pending.json').read_text())) == 0


def test_material_fact_change_is_a_new_pending_decision():
    a = la.pending_key('upgrade_gate', _gate(12))
    assert a == la.pending_key('upgrade_gate', _gate(40))
    assert a != la.pending_key('upgrade_gate', _gate(12, freeleech='no'))
    assert la.pending_key('upgrade_gate', {'title': 'T', 'seeders': 3}) == la.pending_key('upgrade_gate', {'title': 'T', 'seeders': 40})


def test_pending_entries_expire_after_24h(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down')})
    run(tmp_path, clock, http)
    clock.minutes(25 * 60)
    run(tmp_path, clock, http, env={k: v for k, v in ENV.items() if k not in ('DARKBLOOM_CLOUD_API_KEY', 'ANTHROPIC_API_KEY')})
    ent = list(json.loads((tmp_path / 'llm_pending.json').read_text()).values())
    assert len(ent) == 1 and ent[0]['attempts'] == 1


def test_model_discovery_first_id_cached_15_s(tmp_path):
    clock = Clock()
    env = dict({k: v for k, v in ENV.items() if k != 'LLM_LOCAL_MODEL'}, LLM_LOCAL_APPROVED_UPGRADE_GATE='first')
    http = FakeHttp({'/v1/models': Resp(200, {'data': [{'id': 'first'}, {'id': 'second'}]}),
                     '/v1/chat/completions': chat('allow')})
    run(tmp_path, clock, http, env=env)
    run(tmp_path, clock, http, env=env)
    assert http.n('/v1/models') == 1
    assert [c for c in http.calls if 'chat' in c[1]][0][2]['json']['model'] == 'first'
    clock.t += 16
    run(tmp_path, clock, http, env=env)
    assert http.n('/v1/models') == 2


def _loaded(model_id):
    return FakeHttp({'/v1/models': Resp(200, {'data': [{'id': model_id}]}), '/v1/chat/completions': chat('allow', 0.9)})


AUTO = {k: v for k, v in ENV.items() if k not in ('LLM_LOCAL_MODEL', 'DARKBLOOM_CLOUD_API_KEY', 'ANTHROPIC_API_KEY')}


def test_unapproved_loaded_model_skips_local_never_swaps(tmp_path):
    la._MODELS_CACHE.clear()
    http = _loaded('gemma-4-26b')
    out, info, rows = run(tmp_path, Clock(), http, env=AUTO)
    assert http.n('/v1/chat/completions') == 0 and info['pending'] is True and out == 'allow'
    assert rows[-1]['local_skip'] == 'model-not-approved' and rows[-1]['layer'] == 'pending'


def test_approved_loaded_model_is_used_case_and_org_prefix_ignored(tmp_path):
    la._MODELS_CACHE.clear()
    http = _loaded('mlx-community/Qwen3.5-35B-A3B')
    out, info, rows = run(tmp_path, Clock(), http, env=AUTO, rules='reject')
    assert out == 'allow' and info['layer'] == 'local' and 'local_skip' not in rows[-1]


def test_approved_list_env_override_and_wildcard(tmp_path):
    for i, (approved, asked) in enumerate((('gemma-4-26b, other', 1), ('*', 1), ('qwen3.5-35b-a3b', 0))):
        la._MODELS_CACHE.clear()
        http = _loaded('gemma-4-26b')
        d = tmp_path / str(i)
        d.mkdir()
        run(d, Clock(), http, env=dict(AUTO, LLM_LOCAL_APPROVED_UPGRADE_GATE=approved))
        assert http.n('/v1/chat/completions') == asked, approved


def test_approved_list_is_per_kind(tmp_path):
    la._MODELS_CACHE.clear()
    http = FakeHttp({'/v1/models': Resp(200, {'data': [{'id': 'gemma-4-26b'}]}), '/v1/chat/completions': chat('match', 0.9)})
    run(tmp_path, Clock(), http, env=dict(AUTO, LLM_LOCAL_APPROVED_UPGRADE_GATE='gemma-4-26b'), kind='import_identity',
        rules='match', facts={'service': 'Sonarr', 'queue_title': 'Some.Show.S01E01', 'matched_title': 'Some Show'})
    assert http.n('/v1/chat/completions') == 0


def test_pinned_model_bypasses_the_approved_list_but_must_be_listed(tmp_path):
    la._MODELS_CACHE.clear()
    http = FakeHttp({'local.invalid': chat('allow')})
    out, info, _ = run(tmp_path, Clock(), http, env=dict(ENV, LLM_LOCAL_MODEL='gemma-4-26b'), rules='reject')
    assert out == 'allow' and info['layer'] == 'local' and http.n('/v1/chat/completions') == 1
    la._MODELS_CACHE.clear()
    http = FakeHttp({'local.invalid': chat('allow')})
    out, info, rows = run(tmp_path, Clock(), http, env=dict(ENV, LLM_LOCAL_MODEL='not-loaded'), rules='reject')
    assert http.n('/v1/chat/completions') == 0 and rows[-1]['local_skip'] == 'model-not-loaded'


def test_models_endpoint_down_is_model_unknown(tmp_path):
    la._MODELS_CACHE.clear()
    http = FakeHttp({'/v1/models': ConnectionError('down'), '/v1/chat/completions': chat('allow')})
    _, info, rows = run(tmp_path, Clock(), http, env=AUTO)
    assert http.n('/v1/chat/completions') == 0 and info['pending'] is True
    assert rows[-1]['local_skip'] == 'model-unknown'


def test_local_timeouts_connect_2s_total_configurable(tmp_path):
    http = FakeHttp({'local.invalid': chat('allow')})
    run(tmp_path, Clock(), http, env=dict(ENV, LLM_DECIDE_TIMEOUT='7'))
    assert [c for c in http.calls if 'chat' in c[1]][0][2]['timeout'] == (2.0, 7.0)


def test_legacy_llm_decide_url_alias(tmp_path):
    http = FakeHttp({'legacy.invalid': chat('reject')})
    env = {'LLM_DECIDE_URL': 'http://legacy.invalid/v1/chat/completions', 'LLM_DECIDE_MODEL': 'old', 'LLM_DECIDE_KEY': 'k'}
    out, info, _ = run(tmp_path, Clock(), http, env=env)
    assert out == 'reject' and info['layer'] == 'local'


# ── hold ─────────────────────────────────────────────────────────────────────

HOLD = dict(ENV, LLM_LOCAL_HOLD_URL='http://hold.invalid/hold', LLM_LOCAL_HOLD_TOKEN='tok-secret')


def test_hold_true_skips_local_and_logs_cause_not_token(tmp_path):
    http = FakeHttp({'hold.invalid': Resp(200, {'hold': True, 'cause': 'own-job'}), 'local.invalid': chat('reject')})
    out, info, rows = run(tmp_path, Clock(), http, env=HOLD)
    assert info['pending'] is True and http.n('local.invalid') == 0
    assert rows[0]['hold_cause'] == 'own-job' and 'tok-secret' not in json.dumps(rows)
    hold_call = [c for c in http.calls if 'hold.invalid' in c[1]][0]
    assert hold_call[2]['headers']['Authorization'] == 'Bearer tok-secret'


def test_hold_failures_fail_open(tmp_path):
    for resp in (Resp(401, {'error': 'unauthorized', 'status': 401}), Resp(404, {'error': 'x', 'status': 404}),
                 Resp(500, {'error': 'x', 'status': 500}), Resp(200, None), Resp(200, {'hold': 'true'}),
                 Resp(200, {'active': True}), TimeoutError('slow')):
        http = FakeHttp({'hold.invalid': resp, 'local.invalid': chat('reject')})
        out, info, _ = run(tmp_path, Clock(), http, env=HOLD)
        assert out == 'reject' and info['layer'] == 'local', resp


def test_hold_401_is_not_a_hold(tmp_path):
    held, cause = la.hold_active(HOLD, FakeHttp({'hold.invalid': Resp(401, {'error': 'unauthorized', 'status': 401})}))
    assert held is False and cause is None


# ── spend route ──────────────────────────────────────────────────────────────

def test_spend_summary_sums_month_per_layer(tmp_path):
    p = tmp_path / 'l.jsonl'
    rows = [
        {'ts': '2026-11-02T10:00:00+00:00', 'paid_calls': [{'layer': 'paid', 'prompt_tokens': 100, 'completion_tokens': 10}]},
        {'ts': '2026-11-20T10:00:00+00:00', 'paid_calls': [{'layer': 'paid', 'prompt_tokens': 50, 'completion_tokens': 5},
                                                          {'layer': 'claude', 'input_tokens': 200, 'output_tokens': 20}]},
        {'ts': '2026-10-31T23:00:00+00:00', 'paid_calls': [{'layer': 'paid', 'prompt_tokens': 999, 'completion_tokens': 9}]},
        {'ts': '2026-11-05T00:00:00+00:00', 'layer': 'local'},
    ]
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\nnot json\n')
    got = la.spend_summary(str(p), '2026-11')
    assert got == {'paid': {'calls': 2, 'input_tokens': 150, 'output_tokens': 15},
                   'claude': {'calls': 1, 'input_tokens': 200, 'output_tokens': 20}}
    assert la.spend_summary(str(tmp_path / 'none'), '2026-11') == {}


def test_layer_status_lists_free_first_and_hides_secrets():
    env = dict(ENV, LLM_DECISIONS='enforce', UNRAID_OLLAMA_URL='http://unraid.invalid:11434')
    st = la.layer_status(env)
    assert st['mode'] == 'enforce'
    assert st['kinds']['upgrade_gate'] == ['unraid', 'local', 'claude', 'paid']
    assert st['kinds']['dupe_confirm'] == ['unraid', 'local']
    assert KEY not in json.dumps(st) and 'unraid.invalid' not in json.dumps(st) and 'sk-fake' not in json.dumps(st)


def test_layer_status_without_free_layers_shows_no_paid():
    st = la.layer_status({'LLM_DECISIONS': 'enforce', 'DARKBLOOM_CLOUD_API_KEY': KEY})
    assert all(v == [] for v in st['kinds'].values()) and st['paid_model'] == 'qwen3.5-35b-a3b'
