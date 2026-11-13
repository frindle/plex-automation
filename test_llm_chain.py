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
    out = la.advise(kind, facts or FACTS, rules, mode=mode, log_path=str(log), env=dict(env or ENV, LLM_PENDING_PATH=str(tmp_path / 'llm_pending.json')), now=clock, http=http,
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
    assert run(tmp_path, Clock(), http, kind='dupe_keeper', rules='keep_both')[0] == 'keep_both'
    assert run(tmp_path, Clock(), http, kind='import_identity', rules='match', mode='shadow')[0] == 'match'


def test_after_wait_paid_is_called_and_logged_with_usage(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('throttle')})
    run(tmp_path, clock, http)
    clock.minutes(15)
    out, info, rows = run(tmp_path, clock, http)
    assert out == 'throttle' and info['layer'] == 'paid'
    row = rows[-1]
    assert row['layer'] == 'paid' and row['model'] == 'qwen3.5-27b-claude-opus-8bit'
    call = row['paid_calls'][0]
    assert call['prompt_tokens'] == 100 and call['completion_tokens'] == 10 and call['request_id'] == 'req-1'
    paid_post = [c for c in http.calls if 'api.darkbloom.dev' in c[1]][0]
    assert 'chat_template_kwargs' not in paid_post[2]['json']
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
    assert kw['model'] == 'claude-sonnet-5-5' and 'thinking' not in kw
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


def test_no_local_env_goes_straight_to_paid(tmp_path):
    http = FakeHttp({'api.darkbloom.dev': chat('reject')})
    out, info, _ = run(tmp_path, Clock(), http, env={'DARKBLOOM_CLOUD_API_KEY': KEY})
    assert out == 'reject' and info['layer'] == 'paid'


def test_restart_keeps_first_seen(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('reject')})
    run(tmp_path, clock, http)
    first = list(json.loads((tmp_path / 'llm_pending.json').read_text()).values())[0]['first_seen']
    la._STORES.clear()  # process restart
    clock.minutes(16)
    out, info, _ = run(tmp_path, clock, http)
    assert info['layer'] == 'paid' and first == 1_000_000.0


def _dupe(days_a, days_b, seeding=True):
    v = lambda d: {'path': '/m/a.mkv', 'size_gb': 20, 'res': '2160p', 'hdr': 'dv', 'seeding': seeding, 'seeded_days': d}
    return {'title': 'T', 'seed_days': 21, 'first': v(days_a), 'second': dict(v(days_b), path='/m/b.mkv')}


def test_seeded_days_tick_keeps_the_pending_clock(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('keep_both')})
    out, info, _ = run(tmp_path, clock, http, kind='dupe_keeper', rules='keep_first', facts=_dupe(3.0, 0.4))
    assert info['pending'] is True
    clock.minutes(10)
    out, info, _ = run(tmp_path, clock, http, kind='dupe_keeper', rules='keep_first', facts=_dupe(3.1, 0.5))
    assert info['pending'] is True and info['pending_min'] == 10.0
    clock.minutes(6)
    out, info, _ = run(tmp_path, clock, http, kind='dupe_keeper', rules='keep_first', facts=_dupe(3.1, 0.5))
    assert info['layer'] == 'paid' and http.n('api.darkbloom.dev') == 1
    assert len(json.loads((tmp_path / 'llm_pending.json').read_text())) == 0


def test_material_fact_change_is_a_new_pending_decision():
    a = la.pending_key('dupe_keeper', _dupe(3.0, 0.4))
    assert a == la.pending_key('dupe_keeper', _dupe(9.9, 7.7))
    assert a != la.pending_key('dupe_keeper', _dupe(3.0, 0.4, seeding=False))
    assert la.pending_key('upgrade_gate', {'title': 'T', 'seeders': 3}) == la.pending_key('upgrade_gate', {'title': 'T', 'seeders': 40})


def test_pending_entries_expire_after_24h(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down')})
    run(tmp_path, clock, http)
    clock.minutes(25 * 60)
    run(tmp_path, clock, http, env={k: v for k, v in ENV.items() if k not in ('DARKBLOOM_CLOUD_API_KEY', 'ANTHROPIC_API_KEY')})
    ent = list(json.loads((tmp_path / 'llm_pending.json').read_text()).values())
    assert len(ent) == 1 and ent[0]['attempts'] == 1


def test_model_discovery_first_id_cached_10_min(tmp_path):
    clock = Clock()
    env = {k: v for k, v in ENV.items() if k != 'LLM_LOCAL_MODEL'}
    http = FakeHttp({'/v1/models': Resp(200, {'data': [{'id': 'first'}, {'id': 'second'}]}),
                     '/v1/chat/completions': chat('allow')})
    run(tmp_path, clock, http, env=env)
    run(tmp_path, clock, http, env=env)
    assert http.n('/v1/models') == 1
    assert [c for c in http.calls if 'chat' in c[1]][0][2]['json']['model'] == 'first'
    clock.minutes(11)
    run(tmp_path, clock, http, env=env)
    assert http.n('/v1/models') == 2


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
