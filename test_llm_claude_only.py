"""LLM_CLAUDE_ONLY switch: only Claude is built and asked, no pending wait, free-only kinds included, cap / shadow /
no-key fall back to rules, /llm-layers output. Synthetic, no network."""
import json

import pytest

import llm_advisor as la
from test_llm_chain import ENV, Clock, FakeClaude, FakeHttp, chat, run

ONLY = dict(ENV, LLM_CLAUDE_ONLY='1', UNRAID_OLLAMA_URL='http://unraid.invalid')


def routes():
    return FakeHttp({'local.invalid': chat('allow'), 'unraid.invalid': chat('allow'),
                     'api.darkbloom.dev': chat('allow')})


@pytest.fixture(autouse=True)
def fresh_once():
    la._ONCE.clear()
    yield
    la._ONCE.clear()


def test_off_keeps_layer_order(tmp_path):
    clock, http, claude = Clock(), FakeHttp({'local.invalid': ConnectionError('down')}), FakeClaude('reject', 0.9)
    out, info, rows = run(tmp_path, clock, http, claude=claude)
    assert info['pending'] is True and claude.calls == []        # still waits for the free layer
    for off in ('', '0', 'no', 'false', 'off', 'junk'):
        assert la.claude_only(dict(ENV, LLM_CLAUDE_ONLY=off)) is False
    assert list(la.build_layers('upgrade_gate', ENV, http=routes(), claude_client=claude)) == ['local', 'paid', 'claude']


@pytest.mark.parametrize('val', ['1', 'true', 'YES', 'On', ' True '])
def test_truthy_values(val):
    assert la.claude_only({'LLM_CLAUDE_ONLY': val}) is True


def test_only_claude_is_built_and_asked(tmp_path):
    http, claude = routes(), FakeClaude('reject', 0.9)
    assert list(la.build_layers('upgrade_gate', ONLY, http=http, claude_client=claude)) == ['claude']
    out, info, rows = run(tmp_path, Clock(), http, env=ONLY, claude=claude)
    assert out == 'reject' and info['layer'] == 'claude' and info['pending'] is False
    assert len(claude.calls) == 1 and http.calls == []
    assert rows[-1]['layer'] == 'claude'


def test_no_pending_wait_when_free_layers_down(tmp_path):
    http = FakeHttp({'local.invalid': ConnectionError('down')})
    claude = FakeClaude('reject', 0.9)
    out, info, rows = run(tmp_path, Clock(), http, env=ONLY, claude=claude)
    assert info['pending'] is False and out == 'reject' and http.calls == []
    assert not (tmp_path / 'llm_pending.json').exists() or json.loads((tmp_path / 'llm_pending.json').read_text()) == {}


@pytest.mark.parametrize('kind,action', [('dupe_confirm', 'same'), ('torrent_error', 'wait')])
def test_free_only_kinds_go_to_claude(tmp_path, kind, action):
    claude = FakeClaude(action, 0.9)
    out, info, rows = run(tmp_path, Clock(), routes(), env=ONLY, claude=claude, kind=kind,
                          mode='enforce', rules=action)
    assert len(claude.calls) == 1 and info['layer'] == 'claude'


def test_over_cap_falls_back_to_rules(tmp_path):
    clock, claude = Clock(), FakeClaude('reject', 0.9)
    env = dict(ONLY, LLM_CLAUDE_MONTHLY_USD='0.001', LLM_CLAUDE_SPEND_PATH=str(tmp_path / 'claude_spend.json'))
    la.get_claude_spend(env['LLM_CLAUDE_SPEND_PATH'], clock).add('claude-haiku-5-5', 100_000, 10_000)
    out, info, rows = run(tmp_path, clock, routes(), env=env, claude=claude)
    assert out == 'allow' and claude.calls == [] and info['pending'] is False
    assert rows[-1]['claude_skip'] == 'claude month cap'


def test_claude_error_falls_back_to_rules(tmp_path):
    class Boom(FakeClaude):
        def create(self, **kw):
            self.calls.append(kw)
            raise RuntimeError('boom')
    claude = Boom()
    out, info, rows = run(tmp_path, Clock(), routes(), env=ONLY, claude=claude)
    assert out == 'allow' and len(claude.calls) == 1 and info['pending'] is False


def test_low_confidence_still_applies(tmp_path):
    claude = FakeClaude('reject', 0.1)
    out, info, rows = run(tmp_path, Clock(), routes(), env=ONLY, claude=claude)
    assert out == 'allow' and len(claude.calls) == 1      # asked once, low confidence keeps the rules action


def test_answer_cache_still_applies(tmp_path):
    claude, clock = FakeClaude('match', 0.95), Clock()
    facts = {'queue_title': 'Some.Show.S01E01.1080p.WEB-GRP', 'matched_title': 'Some Show', 'seeders': 5}
    run(tmp_path, clock, routes(), env=ONLY, claude=claude, kind='import_identity', facts=dict(facts), rules='match')
    clock.minutes(60)
    out, info, rows = run(tmp_path, clock, routes(), env=ONLY, claude=claude, kind='import_identity',
                          facts=dict(facts), rules='match')
    assert len(claude.calls) == 1 and info['layer'] == 'cache'


def test_shadow_asks_nothing_and_logs_once(tmp_path, caplog):
    claude, http = FakeClaude('reject', 0.9), routes()
    with caplog.at_level('WARNING', logger='llm_advisor'):
        for facts in ({'title': 'a'}, {'title': 'b'}):
            out, info, rows = run(tmp_path, Clock(), http, env=ONLY, claude=claude, mode='shadow', facts=facts)
            assert out == 'allow'
    assert claude.calls == [] and http.calls == []
    assert sum('shadow mode asks no LLM' in r.getMessage() for r in caplog.records) == 1


def test_no_key_warns_once_and_uses_rules_only(tmp_path, caplog):
    env = {k: v for k, v in ONLY.items() if k != 'ANTHROPIC_API_KEY'}
    http = routes()
    with caplog.at_level('WARNING', logger='llm_advisor'):
        for facts in ({'title': 'a'}, {'title': 'b'}):
            out, info, rows = run(tmp_path, Clock(), http, env=env, claude=None, facts=facts)
            assert out == 'allow' and info['pending'] is False
    assert http.calls == []                                   # the free layers are not used
    assert la.build_layers('upgrade_gate', env, http=http) == {}
    assert sum('ANTHROPIC_API_KEY is not set' in r.getMessage() for r in caplog.records) == 1


def test_layer_status_reports_claude_only():
    env = dict(ONLY, LLM_DECISIONS='enforce')
    st = la.layer_status(env)
    assert st['claude_only'] is True
    assert all(v == ['claude'] for v in st['kinds'].values())
    assert st['unraid_model'] is None and st['local_model'] is None and st['paid_model'] is None
    assert st['claude_model']
    off = la.layer_status(dict(ENV, LLM_DECISIONS='enforce'))
    assert off['claude_only'] is False and off['kinds']['upgrade_gate'] == ['local', 'claude', 'paid']
    nokey = la.layer_status({'LLM_DECISIONS': 'enforce', 'LLM_CLAUDE_ONLY': 'on', 'DARKBLOOM_BASE_URL': 'http://x'})
    assert nokey['claude_only'] is True and all(v == [] for v in nokey['kinds'].values())


def test_llm_layers_route(monkeypatch):
    import test_llm_arr_health as t
    client = t.aw.app.test_client()
    for k, v in dict(ONLY, LLM_DECISIONS='enforce').items():
        monkeypatch.setenv(k, v)
    body = client.get('/llm-layers').get_json()
    assert body['ok'] is True and body['claude_only'] is True
    assert body['kinds']['dupe_confirm'] == ['claude']
