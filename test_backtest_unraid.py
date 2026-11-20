"""backtest_local --layer unraid: setup refusals, the gate wait, the keep rule and kept-only scoring (no network)."""
import json

import pytest

import backtest_local as bl
import llm_advisor

BASE = 'http://unraid.invalid:11434'
UP = {'seeders': 50, 'quality': 'WEB-DL'}
LOW = {'seeders': 2}


@pytest.fixture(autouse=True)
def _clean_health():
    llm_advisor._UNRAID_HEALTH.clear()
    yield
    llm_advisor._UNRAID_HEALTH.clear()


def reply(action, conf=0.9):
    return json.dumps({'action': action, 'confidence': conf, 'reason': 'x'})


class ReadTimeout(Exception):
    pass


class R:
    def __init__(self, body):
        self.body, self.status_code = body, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class Http:
    """Fake Unraid Ollama: /api/tags, /api/ps, a hold URL and /api/chat (answers popped from `chat`)."""

    def __init__(self, tags=('qwen3:14b',), hold=(), resident=(), chat=()):
        self.tags, self.hold, self.resident, self.chat = list(tags), list(hold), list(resident), list(chat)
        self.posts = 0
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        if url.endswith('/api/tags'):
            return R({'models': [{'name': t, 'model': t} for t in self.tags]})
        if url.endswith('/api/ps'):
            return R({'models': [{'name': t, 'model': t} for t in self.resident]})
        return R({'hold': self.hold.pop(0) if self.hold else False, 'cause': 'plex'})

    def post(self, url, **kw):
        self.posts += 1
        a = self.chat.pop(0) if self.chat else reply('allow')
        if isinstance(a, Exception):
            raise a
        return R({'message': {'content': a}, 'prompt_eval_count': 3, 'eval_count': 2})


def rows(n, facts=UP, rules='allow', **extra):
    return [dict({'facts': dict(facts), 'rules_action': rules, 'id': 'r%d' % i}, **extra) for i in range(n)]


def facts_file(tmp_path, data, name='f.jsonl'):
    p = tmp_path / name
    p.write_text('\n'.join(json.dumps(r) for r in data) + '\n')
    return str(p)


ENV = {'UNRAID_OLLAMA_URL': BASE}


def run(tmp_path, http, argv=(), env=None, data=None, sleep=None):
    naps = []
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'unraid', '--facts',
                    facts_file(tmp_path, data if data is not None else rows(10))] + list(argv),
                   env=dict(ENV, **(env or {})), http=http, sleep=sleep or naps.append)
    return code, naps


def score(data, answers, kind='upgrade_gate', gate=lambda: None, budget_s=1500.0, state=None):
    it = iter(answers)

    def chat(prompt):
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return a
    budget = {'left_s': budget_s, 'waited_s': 0.0}
    naps = []
    res = bl.score_unraid(kind, data, chat, gate, state if state is not None else {}, budget, naps.append)
    return res, budget, naps


def test_refuses_without_url_and_unlisted_model_sends_no_chat(tmp_path, capsys):
    assert run(tmp_path, Http(), env={'UNRAID_OLLAMA_URL': ''})[0] == 2
    assert 'UNRAID_OLLAMA_URL not set' in capsys.readouterr().out
    http = Http(tags=['other:1b'])
    assert run(tmp_path, http)[0] == 2
    assert 'model-not-listed' in capsys.readouterr().out and http.posts == 0
    http = Http(tags=['qwen3:14b'])
    assert run(tmp_path, http, argv=['--model', 'qwen3.5:9b'])[0] == 2
    assert http.posts == 0 and not any(u.endswith('/api/ps') for u in http.gets)


def test_model_choice_flag_then_env_then_default(tmp_path, capsys):
    http = Http(tags=['qwen3.5:9b'])
    assert run(tmp_path, http, argv=['--model', 'qwen3.5:9b'])[0] == 0
    assert 'model=qwen3.5:9b layer=unraid' in capsys.readouterr().out
    http = Http(tags=['m-env:1b'])
    assert run(tmp_path, http, env={'LLM_UNRAID_MODEL': 'm-env:1b'})[0] == 0
    assert 'model=m-env:1b' in capsys.readouterr().out
    http = Http(tags=['qwen3:14b'])
    assert run(tmp_path, http)[0] == 0
    assert 'model=%s' % llm_advisor.UNRAID_MODEL in capsys.readouterr().out


def test_hold_then_clears_polls_every_30s(tmp_path, capsys):
    http = Http(hold=[True, True, False])
    code, naps = run(tmp_path, http, env={'LLM_UNRAID_HOLD_URL': 'http://hold.invalid/h'})
    out = capsys.readouterr().out
    assert code == 0 and naps == [30.0, 30.0] and http.posts == 10
    assert 'verdict=PASS' in out and 'gpu_wait_min=1.0' in out


def test_other_model_resident_waits_too(tmp_path):
    http = Http(resident=['other:1b'])
    code, naps = run(tmp_path, http, argv=['--wait-min', '1'])
    assert code == 3 and naps == [30.0, 30.0] and http.posts == 0


def test_hold_never_clears_is_inconclusive_with_no_request(tmp_path, capsys):
    http = Http(hold=[True] * 500)
    code, naps = run(tmp_path, http, argv=['--wait-min', '2'], env={'LLM_UNRAID_HOLD_URL': 'http://hold.invalid/h'})
    out = capsys.readouterr().out
    assert code == 3 and http.posts == 0 and len(naps) == 4
    assert 'verdict=INCONCLUSIVE (GPU busy)' in out


def test_wait_budget_is_shared_across_rows():
    seq = iter(['hold', None, 'hold', 'hold', None])    # row 1 waits once, row 2 would wait twice
    res, budget, naps = score(rows(3), [reply('allow')] * 3, gate=lambda: next(seq), budget_s=60.0)
    assert res['stopped_gpu'] and res['total'] == 1 and len(naps) == 2 and budget['left_s'] == 0.0
    assert bl.verdict_unraid('upgrade_gate', res) == ('INCONCLUSIVE', ['GPU busy'])


def test_keep_rule():
    res, _, _ = score(rows(6), [reply('allow'),                  # decisive, confident: kept
                                reply('unsure'),                 # unsure: passed on
                                reply('allow', 0.5),             # weak: passed on
                                'not json',                      # invalid
                                ValueError('boom'),              # error
                                ReadTimeout('slow')])            # timeout
    assert res['kept'] == 1 and res['total'] == 6 and res['blocks'] == []
    assert res['passed'] == {'unsure': 1, 'weak': 1, 'invalid': 1, 'error': 1, 'timeout': 1}


def test_hard_rule_turning_answer_into_unsure_is_passed_on():
    data = rows(1, {'year_matches': False}, rules='unsure')
    res, _, _ = score(data, [reply('match')], kind='import_identity')
    assert res['kept'] == 0 and res['passed']['unsure'] == 1
    assert res['blocks'] == ['#1/r0:match->unsure:year_mismatch']


def test_hard_rule_to_decisive_answer_is_kept_as_the_ruled_action():
    res, _, _ = score(rows(1, LOW, rules='reject'), [reply('throttle')])
    assert res['kept'] == 1 and res['rules_agree'] == 1 and res['pairs'] == {'reject->reject': 1}


def test_agreement_counts_kept_answers_only():
    data = rows(10, human='allow')
    answers = [reply('allow')] * 8 + [reply('reject'), reply('unsure')]
    res, _, _ = score(data, answers)
    assert res['kept'] == 9 and res['rules_n'] == 9 and res['rules_agree'] == 8
    assert res['human_n'] == 9 and res['human_agree'] == 8 and res['disagree_ids'] == ['#9/r8']


def test_verdicts():
    ok, _, _ = score(rows(10), [reply('allow')] * 10)
    assert bl.verdict_unraid('upgrade_gate', ok) == ('PASS', [])
    few, _, _ = score(rows(9), [reply('allow')] * 9)
    assert bl.verdict_unraid('upgrade_gate', few)[0] == 'INCONCLUSIVE'
    none, _, _ = score(rows(10), [reply('unsure')] * 10)
    assert bl.verdict_unraid('upgrade_gate', none) == ('INCONCLUSIVE', ['nothing kept'])
    bad, _, _ = score(rows(10), [reply('allow')] * 9 + [reply('reject')])
    v, why = bl.verdict_unraid('upgrade_gate', bad)
    assert v == 'FAIL' and 'rules agreement 90.0%' in why[0]
    hum, _, _ = score(rows(10, human='reject'), [reply('allow')] * 10)
    assert 'human agreement' in bl.verdict_unraid('upgrade_gate', hum)[1][-1]
    # lots passed on is still a PASS: there is no answered-% bar
    mostly, _, _ = score(rows(20), [reply('allow')] * 10 + [reply('unsure')] * 10)
    assert bl.verdict_unraid('upgrade_gate', mostly)[0] == 'PASS'


def test_timeout_does_not_block_the_next_row(tmp_path, capsys):
    http = Http(chat=[ReadTimeout('slow')])
    code, naps = run(tmp_path, http)
    out = capsys.readouterr().out
    assert code == 0 and http.posts == 10 and naps == []
    assert 'kept=9/10' in out and 'timeout=1' in out


def test_report_has_required_lines_and_no_facts(tmp_path, capsys):
    data = rows(10)
    data[0]['title'] = 'Secret Title'
    http = Http(chat=[reply('allow', 0.5)])
    run(tmp_path, http, data=data)
    out = capsys.readouterr().out
    for want in ('kept=9/10 (90.0%)', 'passed_on=1', 'weak=1', 'rules_agree=9/9', 'human_agree=0/0', 'latency_ms p50=',
                 'cold_rows=10'):
        assert want in out
    assert 'Secret Title' not in out and BASE not in out


def test_non_unraid_kind_skipped(tmp_path, capsys):
    http = Http()
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'unraid', '--facts', facts_file(tmp_path, rows(10))],
                   env=dict(ENV, LLM_UNRAID_KINDS='import_identity'), http=http, sleep=lambda s: None)
    assert code == 2 and 'skipped: not an Unraid kind' in capsys.readouterr().out and http.posts == 0


def test_multi_kind_skips_missing_facts_file_and_goes_on(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = bl.load_rows

    def load(path, limit=None):
        if path.endswith('upgrade_gate.jsonl'):
            return rows(10)
        return real(path, limit)       # others: file not found
    monkeypatch.setattr(bl, 'load_rows', load)
    http = Http()
    code = bl.main(['--kind', 'upgrade_gate,import_identity,torrent_error', '--layer', 'unraid'],
                   env=dict(ENV, LLM_UNRAID_KINDS='upgrade_gate,import_identity'), http=http, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 0 and 'kind=import_identity skipped: no facts file' in out
    assert 'kind=torrent_error skipped: not an Unraid kind' in out and 'verdict=PASS' in out and http.posts == 10


def test_single_kind_missing_facts_file_still_refuses(tmp_path, capsys):
    code = bl.main(['--kind', 'upgrade_gate', '--layer', 'unraid', '--facts', str(tmp_path / 'none.jsonl')],
                   env=ENV, http=Http(), sleep=lambda s: None)
    assert code == 2 and 'facts file not readable' in capsys.readouterr().out


def test_multi_kind_missing_facts_skipped_for_local_layer_too(tmp_path, capsys, monkeypatch):
    import test_backtest_local as tl
    monkeypatch.setattr(bl, 'load_rows', lambda path, limit=None: rows(10) if 'upgrade_gate' in path else (_ for _ in ()).throw(OSError()))
    http = tl.Http(['m1'])
    code = bl.main(['--kind', 'upgrade_gate,dupe_confirm', '--layer', 'local'], env=tl.ENV, http=http,
                   transport=lambda p: reply('allow'), sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 0 and 'kind=dupe_confirm skipped: no facts file' in out and 'verdict=PASS' in out
