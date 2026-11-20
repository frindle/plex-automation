"""Claude (Haiku) layer between the free layers and paid Darkbloom: order, monthly cap, rollover, spend-limit skip,
cost math, /llm-layers fields, backtest flags. Synthetic, no network."""
import json
import time

import backtest_local as bl
import llm_advisor as la
from test_llm_chain import ENV, KEY, Clock, FakeClaude, FakeHttp, chat, run


def epoch(y, m, d, hh=0):
    import calendar
    return float(calendar.timegm((y, m, d, hh, 0, 0)))


class Err(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


class ErrClaude(FakeClaude):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def create(self, **kw):
        self.calls.append(kw)
        raise self.exc


def down_http():
    return FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('reject')})


def spend_env(tmp_path, **kw):
    return dict(ENV, LLM_CLAUDE_SPEND_PATH=str(tmp_path / 'claude_spend.json'), **kw)


def go_after_wait(tmp_path, clock, http, env, claude):
    run(tmp_path, clock, http, env=env, claude=claude)   # starts the pending clock
    clock.minutes(30)
    return run(tmp_path, clock, http, env=env, claude=claude)


def test_claude_answers_before_paid(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('reject', 0.9)
    out, info, rows = go_after_wait(tmp_path, clock, http, spend_env(tmp_path), claude)
    assert info['layer'] == 'claude' and out == 'reject'
    assert http.n('api.darkbloom.dev') == 0          # paid never asked
    assert len(claude.calls) == 1


def test_still_waits_for_free_layers_first(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('reject', 0.9)
    out, info, rows = run(tmp_path, clock, http, env=spend_env(tmp_path), claude=claude)
    assert info['pending'] is True and claude.calls == [] and http.n('api.darkbloom.dev') == 0


def test_free_only_kind_and_shadow_never_ask_claude(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('wait', 0.9)
    go_after_wait(tmp_path, clock, http, spend_env(tmp_path), claude)
    shadow_claude = FakeClaude('reject', 0.9)
    run(tmp_path, clock, http, env=spend_env(tmp_path), claude=shadow_claude, mode='shadow')
    clock.minutes(30)
    run(tmp_path, clock, http, env=spend_env(tmp_path), claude=shadow_claude, mode='shadow')
    assert shadow_claude.calls == []
    free = FakeClaude('wait', 0.9)
    run(tmp_path, clock, http, env=spend_env(tmp_path), claude=free, kind='torrent_error')
    clock.minutes(30)
    run(tmp_path, clock, http, env=spend_env(tmp_path), claude=free, kind='torrent_error')
    assert free.calls == []


def test_cap_reached_skips_claude_and_paid_answers(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('allow', 0.9)
    env = spend_env(tmp_path, LLM_CLAUDE_MONTHLY_USD='0.001')
    la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH'], clock).add('claude-haiku-5-5', 100_000, 10_000)   # $0.015
    out, info, rows = go_after_wait(tmp_path, clock, http, env, claude)
    assert claude.calls == [] and info['layer'] == 'paid' and out == 'reject'
    assert rows[-1]['claude_skip'] == 'claude month cap'


def test_calls_are_charged_and_cap_trips_mid_month(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('reject', 0.9)
    env = spend_env(tmp_path, LLM_CLAUDE_MONTHLY_USD='0.00005')      # one 200-in/20-out call costs $0.00003
    go_after_wait(tmp_path, clock, http, env, claude)
    snap = la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH'], clock).snapshot()
    assert snap['calls'] == 1 and abs(snap['usd'] - 0.00003) < 1e-9
    assert la.claude_month(env, clock)['capped'] is False
    la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH'], clock).add('claude-haiku-5-5', 200, 20)
    assert la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH'], clock).skip_reason(la.claude_monthly_cap(env)) == 'claude month cap'


def test_month_rollover_resets_cost(tmp_path):
    t = [epoch(2026, 11, 30, 23)]
    sp = la.ClaudeSpend(str(tmp_path / 's.json'), lambda: t[0])
    sp.add('claude-sonnet-5-5', 1_000_000, 0)
    assert sp.snapshot()['usd'] == 2.0 and sp.snapshot()['month'] == '2026-11'
    sp.mark_limit_hit()
    assert sp.skip_reason(1.0) == 'claude spend limit'
    t[0] = epoch(2026, 12, 1, 0)
    snap = sp.snapshot()
    assert snap == {'month': '2026-12', 'usd': 0.0, 'calls': 0, 'limit_hit': False}
    assert sp.skip_reason(1.0) is None
    # a fresh process reads the persisted current month, and drops a stale one
    again = la.ClaudeSpend(str(tmp_path / 's.json'), lambda: epoch(2027, 1, 2))
    assert again.snapshot()['usd'] == 0.0


def test_spend_persists_across_processes(tmp_path):
    p = str(tmp_path / 's.json')
    now = lambda: epoch(2026, 11, 5)  # noqa: E731
    la.ClaudeSpend(p, now).add('claude-haiku-5-5', 100_000, 100_000)
    again = la.ClaudeSpend(p, now).snapshot()
    assert abs(again['usd'] - 0.06) < 1e-9 and again['calls'] == 1


def test_spend_limit_error_skips_rest_of_month_and_logs_once(tmp_path, caplog):
    clock = Clock()
    exc = Err('Your credit balance is too low to access the Anthropic API', 400)
    claude = ErrClaude(exc)
    http = down_http()
    env = spend_env(tmp_path)
    with caplog.at_level('WARNING', logger='llm_advisor'):
        out, info, rows = go_after_wait(tmp_path, clock, http, env, claude)
        assert info['layer'] == 'paid' and out == 'reject' and len(claude.calls) == 1
        # a later, different decision this month: claude is not tried again
        out2, info2, rows2 = run(tmp_path, clock, http, env=env, claude=claude, facts={'title': 'T2'})
        clock.minutes(30)
        out2, info2, rows2 = run(tmp_path, clock, http, env=env, claude=claude, facts={'title': 'T2'})
    assert len(claude.calls) == 1 and info2['layer'] == 'paid'
    assert rows2[-1]['claude_skip'] == 'claude spend limit'
    assert sum('spend/credit limit' in r.getMessage() for r in caplog.records) == 1
    assert la.claude_month(env, clock)['api_limit_hit'] is True


def test_spend_limit_error_classification():
    assert la.claude_spend_limit_error(Err('You have reached your specified workspace API usage limits.', 400))
    assert la.claude_spend_limit_error(Err('monthly spend limit reached', 429))
    assert not la.claude_spend_limit_error(Err('rate limit exceeded, retry later', 429))
    assert not la.claude_spend_limit_error(Err('usage limit', 500))
    assert not la.claude_spend_limit_error(ValueError('credit balance'))


def test_timeout_and_5xx_back_off_then_fall_to_paid(tmp_path):
    clock = Clock()
    for exc in (type('APITimeoutError', (Exception,), {})('t'), Err('overloaded', 529), Err('boom', 500)):
        sp = la.ClaudeSpend(str(tmp_path / ('s%d.json' % id(exc))), clock)
        fn = la._claude_fn('upgrade_gate', 'claude-haiku-5-5', lambda: ErrClaude(exc), sp, {})
        try:
            fn('p')
        except Exception:  # noqa: BLE001
            pass
        assert sp.skip_reason(100) == 'backoff'
        clock.minutes(16)
        assert sp.skip_reason(100) is None            # 15 min, then it is tried again
        assert sp.snapshot()['limit_hit'] is False
    # 400 on request shape is neither a back-off nor a month skip
    sp = la.ClaudeSpend(str(tmp_path / 'x.json'), clock)
    try:
        la._claude_fn('upgrade_gate', 'claude-haiku-5-5', lambda: ErrClaude(Err('bad field', 400)), sp, {})('p')
    except Exception:  # noqa: BLE001
        pass
    assert sp.skip_reason(100) is None


def test_backoff_doubles_with_repeated_failures(tmp_path):
    t = [1000.0]
    sp = la.ClaudeSpend(str(tmp_path / 's.json'), lambda: t[0])
    f = la._claude_backoff_s({})
    sp.note_failure(f)
    assert sp.until - t[0] == 15 * 60
    sp.note_failure(f)
    assert sp.until - t[0] == 30 * 60
    sp.note_ok()
    assert sp.skip_reason(100) is None


def test_cost_math():
    assert la.CLAUDE_MODEL == 'claude-haiku-5-5'
    assert abs(la.claude_cost_usd('claude-haiku-5-5', 100_000, 100_000) - 0.06) < 1e-12
    assert abs(la.claude_cost_usd('claude-sonnet-5-5', 1_000_000, 1_000_000) - 12.0) < 1e-12
    assert abs(la.claude_cost_usd('claude-haiku-5-5', 2000, 300) - (2000 * 0.10 + 300 * 0.50) / 1e6) < 1e-12
    assert abs(la.claude_cost_usd('claude-haiku-5-5', 200_000, 0) - 0.1) < 1e-12        # long-prompt tier $0.50/M
    assert la.claude_cost_usd('some-override', 1_000_000, 0) == 2.0                      # unknown: dearest price
    assert la.claude_cost_usd('claude-haiku-5-5', None, None) == 0.0


def test_cap_env_parsing():
    assert la.claude_monthly_cap({}) == 100.0
    assert la.claude_monthly_cap({'LLM_CLAUDE_MONTHLY_USD': '25.5'}) == 25.5
    assert la.claude_monthly_cap({'LLM_CLAUDE_MONTHLY_USD': 'abc'}) == 100.0
    assert la.claude_monthly_cap({'LLM_CLAUDE_MONTHLY_USD': '-3'}) == 100.0


def test_llm_model_override_still_wins(tmp_path):
    clock, http, claude = Clock(), down_http(), FakeClaude('reject', 0.9)
    go_after_wait(tmp_path, clock, http, spend_env(tmp_path, LLM_CLAUDE_MODEL='claude-sonnet-5-5'), claude)
    assert claude.calls[0]['model'] == 'claude-sonnet-5-5'
    # priced at the Sonnet table rate: 200 in / 20 out
    assert abs(la.get_claude_spend(str(tmp_path / 'claude_spend.json'), clock).snapshot()['usd'] - 0.0006) < 1e-9


def test_layer_status_fields_and_order(tmp_path):
    env = dict(ENV, LLM_DECISIONS='enforce', LLM_CLAUDE_SPEND_PATH=str(tmp_path / 's.json'), LLM_CLAUDE_MONTHLY_USD='50')
    la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH']).add('claude-haiku-5-5', 100_000, 0)
    st = la.layer_status(env)
    assert st['kinds']['upgrade_gate'] == ['local', 'claude', 'paid']
    assert st['claude_model'] == 'claude-haiku-5-5'
    m = st['claude_month']
    assert m['usd'] == 0.01 and m['cap_usd'] == 50.0 and m['calls'] == 1 and m['capped'] is False
    assert m['api_limit_hit'] is False and m['month'] == time.strftime('%Y-%m', time.gmtime())
    blob = json.dumps(st)
    assert 'sk-fake' not in blob and KEY not in blob
    off = la.layer_status({'LLM_DECISIONS': 'enforce'})
    assert off['claude_model'] is None and off['claude_month'] is None


# ── backtest ────────────────────────────────────────────────────────────────

def reply(action, conf=0.9, tin=1000, tout=100):
    return la.Meta(json.dumps({'action': action, 'confidence': conf, 'reason': 'x'}),
                   {'input_tokens': tin, 'output_tokens': tout})


def facts_file(tmp_path, n=10, rules='allow'):
    p = tmp_path / 'f.jsonl'
    p.write_text('\n'.join(json.dumps({'facts': {'seeders': 50, 'quality': 'WEB-DL'}, 'rules_action': rules,
                                        'id': 'r%d' % i}) for i in range(n)))
    return str(p)


def test_backtest_claude_layer_reports_agreement_invalid_and_cost(tmp_path, capsys):
    outs = iter([reply('allow')] * 8 + [la.Meta('not json', {'input_tokens': 1000, 'output_tokens': 50}), reply('reject')])
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'claude', '--model', 'claude-haiku-5-5',
                    '--facts', facts_file(tmp_path)], env={'ANTHROPIC_API_KEY': 'sk-fake'}, transport=lambda p: next(outs))
    text = capsys.readouterr().out
    assert 'model=claude-haiku-5-5 layer=claude' in text
    assert 'answered=9/10' in text and 'rules_agree=8/9' in text
    assert 'invalid=1 errors=0' in text
    assert 'tokens in=10000 out=950 cost_usd=0.0015' in text      # 10000*0.10/M + 950*0.50/M
    assert code == 1


def test_backtest_claude_layer_needs_key_and_ignores_darkbloom(tmp_path, capsys):
    assert bl.main(['--kind', 'upgrade_gate', '--layer', 'claude', '--facts', facts_file(tmp_path)], env={}) == 2
    assert 'ANTHROPIC_API_KEY' in capsys.readouterr().out
    # no DARKBLOOM_BASE_URL needed, no GPU wait
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'claude', '--facts', facts_file(tmp_path)],
                   env={'ANTHROPIC_API_KEY': 'sk-fake'}, transport=lambda p: reply('allow'))
    assert code == 0


def test_backtest_max_usd_stops_the_run(tmp_path, capsys):
    calls = []

    def tr(p):
        calls.append(p)
        return reply('allow', tin=100_000, tout=0)         # $0.01 each
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'claude', '--model', 'claude-haiku-5-5', '--max-usd', '0.025',
                    '--facts', facts_file(tmp_path, n=20)], env={'ANTHROPIC_API_KEY': 'sk-fake'}, transport=tr)
    text = capsys.readouterr().out
    assert len(calls) == 3                               # $0.03 >= $0.025: stop before the 4th
    assert 'INCONCLUSIVE' in text and '--max-usd' in text and code == 1


def test_backtest_default_max_usd_is_two_dollars(tmp_path, capsys):
    n = []

    def tr(p):
        n.append(1)
        return reply('allow', tin=0, tout=2_000_000)       # $1.00 each at the Haiku output price
    bl.main(['--kind', 'upgrade_gate', '--layer', 'claude', '--facts', facts_file(tmp_path, n=10)],
            env={'ANTHROPIC_API_KEY': 'sk-fake'}, transport=tr)
    assert len(n) == 2


def test_backtest_budget_is_shared_across_kinds(tmp_path, capsys):
    p = tmp_path / 'u.jsonl'
    p.write_text('\n'.join(json.dumps({'facts': {'seeders': 50}, 'rules_action': 'allow'}) for _ in range(5)))
    real = bl.load_rows
    bl.load_rows = lambda path, limit=None: real(str(p), limit)
    try:
        calls = []

        def tr(prompt):
            calls.append(1)
            return reply('allow', tin=0, tout=200_000)         # $0.10 each
        bl.main(['--kind', 'upgrade_gate,dupe_confirm', '--layer', 'claude', '--max-usd', '0.3', '--limit', '2'],
                env={'ANTHROPIC_API_KEY': 'sk-fake'}, transport=tr)
    finally:
        bl.load_rows = real
    assert len(calls) == 3 and 'later kinds not run' in capsys.readouterr().out


def test_backtest_unknown_kind_refused(capsys):
    assert bl.main(['--kind', 'nope'], env={}) == 2
