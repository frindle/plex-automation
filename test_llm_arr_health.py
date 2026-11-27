"""arr_health kind: shadow-only model advice on arr health pushes. Synthetic facts only."""
import json
import os
import tempfile
import time

import pytest

import arr_health as ah
import backtest_arr_health as bt
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

K = 'arr_health'
T0 = 2_000_000_000.0
MIN = 60
NOTIFY = '{"action": "notify", "confidence": 0.9, "reason": "r"}'
HOLD = '{"action": "hold", "confidence": 0.9, "reason": "r"}'
SELF = '{"action": "self_heals", "confidence": 0.9, "reason": "r"}'


def facts(**kw):
    f = ah.advise_facts('recovered', 'Sonarr', 'IndexerStatusCheck', 35, 1)
    f.update(kw)
    return f


class AdviceIO:
    services = ['Sonarr']

    def __init__(self):
        self.pushes, self.advice, self.health = [], [], []
        self.up = True

    def get(self, who, path, params=None):
        return self.health

    def deluge_ok(self):
        return self.up

    def push(self, title, message):
        self.pushes.append(title)

    def record(self, category, summary):
        pass

    def advise(self, f):
        self.advice.append(f)

    def post(self, who, path, body):   # self-heal calls (arr_health._heal)
        pass

    def log(self, msg):
        pass


@pytest.fixture(autouse=True)
def inline_shadow(monkeypatch):
    monkeypatch.setattr(aw, '_run_llm_shadow', lambda name, fn: fn())


def test_kind_prompt_parse_hard_rules():
    assert ld.KINDS[K] == ('notify', 'hold', 'self_heals')
    p = ld.build_prompt(K, facts())
    assert 'self_heals' in p and '"event": "recovered"' in p and 'Policy:' in p
    assert ld.parse_decision(K, HOLD)['action'] == 'hold'
    assert ld.parse_decision(K, '{"action": "ask", "confidence": 0.9, "reason": "r"}') is None
    assert ld.apply_hard_rules(K, 'hold', facts()) == ('hold', None)
    assert ld.apply_hard_rules(K, 'self_heals', facts(event='removed', minutes_down=0)) == ('self_heals', None)
    assert ld.apply_hard_rules(K, 'bogus', facts()) == ('notify', 'invalid_action')


def test_outage_past_threshold_always_notifies_and_fails_closed():
    out = facts(event='outage', minutes_down=30)
    assert ld.apply_hard_rules(K, 'hold', out) == ('notify', 'outage_over_threshold')
    assert ld.apply_hard_rules(K, 'self_heals', facts(event='outage', minutes_down=240)) == ('notify', 'outage_over_threshold')
    assert ld.apply_hard_rules(K, 'hold', facts(event='outage', minutes_down=None)) == ('notify', 'outage_over_threshold')
    assert ld.apply_hard_rules(K, 'hold', facts(event='outage', minutes_down='45')) == ('notify', 'outage_over_threshold')
    assert ld.apply_hard_rules(K, 'hold', {'event': 'outage', 'minutes_down': 5}) == ('notify', 'outage_over_threshold')
    assert ld.apply_hard_rules(K, 'notify', out) == ('notify', None)
    # under the threshold the floor does not apply (such an outage is not pushed today)
    assert ld.apply_hard_rules(K, 'hold', facts(event='outage', minutes_down=5)) == ('hold', None)


def test_advisor_config():
    assert K in la.FREE_ONLY_KINDS and K in la.CACHE_KINDS and K in la.OPT_IN_KINDS and K in la.UNRAID_COLD_KINDS
    assert K in la.UNRAID_KINDS.split(',') and la.UNRAID_DECISIVE[K] == frozenset({'notify', 'hold', 'self_heals'})
    assert la.ANSWER_TTL_H[K] == 24.0 and la.PENDING_ACTION[K] == 'notify'
    assert ld._SAFE_ACTION[K] == 'notify'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow'}) == 'off'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow', 'LLM_MODE_ARR_HEALTH': 'shadow'}) == 'shadow'
    st = la.layer_status({'LLM_DECISIONS': 'enforce', 'LLM_MODE_ARR_HEALTH': 'enforce',
                          'DARKBLOOM_CLOUD_API_KEY': 'k', 'DARKBLOOM_BASE_URL': 'http://local.invalid'})
    assert 'paid' not in st['kinds'][K]


def test_listed_by_llm_layers_and_llm_stats():
    assert la.layer_status({'LLM_DECISIONS': 'shadow'})['modes'][K] == 'off'
    assert K in la.llm_stats([])
    client = aw.app.test_client()
    assert K in client.get('/llm-layers').get_json()['modes']
    assert K in client.get('/llm-stats').get_json()['kinds']


def test_shadow_keeps_rules_action_logs_and_caches():
    d = tempfile.mkdtemp(prefix='ah-')
    calls = []

    def run(f):
        def local(prompt):
            calls.append(1)
            return HOLD
        env = {'LLM_DECISIONS': 'shadow', 'LLM_MODE_ARR_HEALTH': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0',
               'LLM_PENDING_PATH': os.path.join(d, 'p.json'), 'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
        return la.advise(K, f, 'notify', log_path=os.path.join(d, 'log.jsonl'), env=env,
                         layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    assert run(facts()) == 'notify' and len(calls) == 1
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == K and row['mode'] == 'shadow'
    run(facts())
    assert len(calls) == 1


def test_outage_recovery_and_removed_pushes_hand_over_name_free_facts():
    io = AdviceIO()
    io.health = [{'source': 'IndexerStatusCheck', 'type': 'error', 'message': 'Indexer Synthetic.Host failing'}]
    state = {}
    ah.outage_tick(io, state, T0)
    ah.outage_tick(io, state, T0 + 15 * MIN)
    assert io.advice == []
    ah.outage_tick(io, state, T0 + 31 * MIN)
    (f,) = io.advice
    assert (f['event'], f['service'], f['condition'], f['minutes_down']) == ('outage', 'sonarr', 'IndexerStatusCheck', 31)
    assert f['outage_alert_min'] == 30 and f['rules_action'] == 'notify'
    assert len(io.pushes) == 1
    io.health = []
    ah.outage_tick(io, state, T0 + 40 * MIN)
    assert io.advice[1]['event'] == 'recovered' and io.advice[1]['minutes_down'] == 40
    assert len(io.pushes) == 2
    # removed: first read is a silent baseline, a newly named one is advised
    rm = [{'source': 'RemovedSeriesCheck', 'type': 'error', 'message': 'Series Synthetic Show (tvdbid 123) was removed from TheTVDB'}]
    ah.removed_tick(io, state, 'Sonarr', rm)
    n = len(io.advice)
    rm2 = rm + [{'source': 'RemovedSeriesCheck', 'type': 'error', 'message': 'Series Other Show (tvdbid 456) was removed from TheTVDB'}]
    ah.removed_tick(io, state, 'Sonarr', rm2)
    assert len(io.advice) == n + 1 and io.advice[-1]['event'] == 'removed' and io.advice[-1]['site'] == 'TheTVDB'
    blob = json.dumps(io.advice)
    for bad in ('Synthetic', 'Show', 'http', '123', '456'):
        assert bad not in blob


def test_advise_failure_never_blocks_the_push():
    io = AdviceIO()
    io.advise = lambda f: 1 / 0
    io.health = [{'source': 'RootFolderCheck', 'type': 'error', 'message': 'm'}]
    state = {}
    ah.outage_tick(io, state, T0)
    ah.outage_tick(io, state, T0 + 15 * MIN)
    ah.outage_tick(io, state, T0 + 31 * MIN)
    assert len(io.pushes) == 1


def test_tick_queues_off_thread_only_when_not_off(monkeypatch):
    advised = []
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append((kind, f, r)) or 'hold')
    monkeypatch.setattr(aw, '_write_json_atomic', lambda *a, **k: None)
    monkeypatch.setattr(aw, '_arr_health_load', lambda: {})

    class IO(AdviceIO):
        def __init__(self):
            super().__init__()
            self.advice = [facts(event='outage', minutes_down=40)]
    monkeypatch.setattr(aw.arr_health, 'outage_tick', lambda io, state, now: {})
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'off')
    aw.arr_health_tick(now=T0, io=IO())
    assert advised == []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    aw.arr_health_tick(now=T0, io=IO())
    (kind, f, rules), = advised
    assert kind == K and rules == 'notify' and f['event'] == 'outage'


def test_cap_and_errors(monkeypatch):
    advised = []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append(1) or 'notify')
    monkeypatch.setenv('ARR_HEALTH_LLM_MAX', '2')
    aw._arr_health_shadow([facts() for _ in range(5)])
    assert len(advised) == 2
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    aw._arr_health_shadow([facts()])


def test_backtest_counts_and_floor(tmp_path, capsys):
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in [facts(), {'facts': facts(), 'expected': 'hold'}]) + '\njunk\n')
    replies = iter([NOTIFY, HOLD])
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=2 disagree=0 no_answer=0' in capsys.readouterr().out
    assert bt.backtest([facts()], lambda prompt: 'junk')['no_answer'] == 1
    res = bt.backtest([facts(event='outage', minutes_down=90)], lambda prompt: SELF)
    assert res['agree'] == 1 and res['pair:notify->notify'] == 1
