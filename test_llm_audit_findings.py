"""audit_findings: shadow-only model advice on library/downloads audit findings. Synthetic facts only."""
import json
import os
import tempfile
import time

import pytest

import backtest_audit_findings as bt
import downloads_audit as da
import library_audit as la_
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

K = 'audit_findings'
FIX = '{"action": "fix", "confidence": 0.9, "reason": "r"}'
IGNORE = '{"action": "ignore", "confidence": 0.9, "reason": "r"}'
ASK = '{"action": "ask", "confidence": 0.9, "reason": "r"}'


def facts(**kw):
    f = {'source': 'downloads_audit', 'finding': 'unlabeled', 'label_class': 'none', 'files': 0, 'size_gb': 0.0,
         'human_only': False, 'data_at_risk': False, 'rules_action': 'ask'}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def inline_shadow(monkeypatch):
    monkeypatch.setattr(aw, '_run_llm_shadow', lambda name, fn: fn())


def test_kind_prompt_parse_hard_rules():
    assert ld.KINDS[K] == ('fix', 'ignore', 'ask')
    p = ld.build_prompt(K, facts())
    assert 'human_only' in p and '"finding": "unlabeled"' in p and 'Policy:' in p
    assert ld.parse_decision(K, FIX)['action'] == 'fix'
    assert ld.parse_decision(K, '{"action": "delete", "confidence": 0.9, "reason": "r"}') is None
    assert ld.apply_hard_rules(K, 'fix', facts()) == ('fix', None)
    assert ld.apply_hard_rules(K, 'ignore', facts()) == ('ignore', None)
    assert ld.apply_hard_rules(K, 'bogus', facts()) == ('ask', 'invalid_action')
    assert ld.apply_hard_rules(K, 'ask', facts()) == ('ask', None)


def test_human_only_stays_human_only_and_floors_fail_closed():
    assert ld.apply_hard_rules(K, 'fix', facts(human_only=True)) == ('ask', 'human_only')
    assert ld.apply_hard_rules(K, 'fix', facts(human_only='false')) == ('ask', 'human_only')
    assert ld.apply_hard_rules(K, 'fix', {}) == ('ask', 'human_only')
    assert ld.apply_hard_rules(K, 'ignore', facts(data_at_risk=True)) == ('ask', 'data_at_risk')
    assert ld.apply_hard_rules(K, 'ignore', {}) == ('ask', 'data_at_risk')
    # ignoring a human-only finding is allowed (the notice was still sent); fixing a data-risk one is not blocked by the risk flag
    assert ld.apply_hard_rules(K, 'ignore', facts(human_only=True)) == ('ignore', None)


def test_advisor_config():
    assert K in la.FREE_ONLY_KINDS and K in la.CACHE_KINDS and K in la.OPT_IN_KINDS and K in la.UNRAID_COLD_KINDS
    assert K in la.UNRAID_KINDS.split(',') and la.UNRAID_DECISIVE[K] == frozenset({'fix', 'ignore'})
    assert la.ANSWER_TTL_H[K] == 24.0 and la.PENDING_ACTION[K] == 'ask'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow'}) == 'off'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow', 'LLM_MODE_AUDIT_FINDINGS': 'shadow'}) == 'shadow'
    # never a paid layer, whatever the mode
    layers = la.layer_status({'LLM_DECISIONS': 'enforce', 'LLM_MODE_AUDIT_FINDINGS': 'enforce',
                              'DARKBLOOM_CLOUD_API_KEY': 'k', 'DARKBLOOM_BASE_URL': 'http://local.invalid'})
    assert 'paid' not in layers['kinds'][K]


def test_shadow_keeps_rules_action_logs_and_caches():
    d = tempfile.mkdtemp(prefix='af-')
    calls = []

    def run(f):
        def local(prompt):
            calls.append(1)
            return FIX
        env = {'LLM_DECISIONS': 'shadow', 'LLM_MODE_AUDIT_FINDINGS': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0',
               'LLM_PENDING_PATH': os.path.join(d, 'p.json'), 'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
        return la.advise(K, f, 'ask', log_path=os.path.join(d, 'log.jsonl'), env=env,
                         layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    assert run(facts()) == 'ask' and len(calls) == 1
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == K and row['mode'] == 'shadow'
    run(facts())
    assert len(calls) == 1


def test_library_audit_facts_have_no_names():
    entry = {'hash': 'abc', 'name': 'Synthetic.Release.1080p', 'label': 'radarr-upgrade',
             'save_path': '/data/Media/Movies/Synthetic', 'state': 'Error',
             'reasons': ['error', 'arr-label-in-library']}
    rows = la_.finding_facts(entry, 'Missing or invalid torrent data!')
    assert [r['finding'] for r in rows] == ['missing_data', 'arr_label_in_library']
    assert all(r['label_class'] == 'radarr' and r['rules_action'] == 'ask' for r in rows)
    assert rows[0]['human_only'] is False and rows[0]['data_at_risk'] is True
    assert rows[1]['human_only'] is True
    blob = json.dumps(rows)
    assert 'Synthetic' not in blob and '/data' not in blob and 'abc' not in blob
    plain = la_.finding_facts({'hash': 'h', 'name': 'n', 'label': '', 'reasons': ['error']}, 'x')
    assert plain[0]['finding'] == 'error' and plain[0]['data_at_risk'] is False and plain[0]['label_class'] == 'none'


def test_library_audit_result_carries_facts_only_for_new_notified():
    torrents = {'h1': {'name': 'A', 'state': 'Error', 'message': '', 'save_path': '/x', 'label': ''}}
    rep = set()
    r1 = la_.library_seed_audit(torrents, lambda t, m: None, rep)
    r2 = la_.library_seed_audit(torrents, lambda t, m: None, rep)
    assert len(r1['advice_facts']) == 1 and r2['advice_facts'] == []
    assert la_.library_seed_audit(torrents, lambda t, m: None, set(), dry_run=True)['advice_facts'] == []


def test_downloads_audit_facts_have_no_names():
    o = da.finding_facts('o', {'path': '/data/Downloads/Synthetic/file.mkv', 'size': 3_500_000_000, 'group': 'Synthetic'})
    s = da.finding_facts('s', {'hash': 'h', 'name': 'Synthetic', 'label': 'sonarr-tv', 'files': 3, 'size': 9e9})
    u = da.finding_facts('u', {'hash': 'h', 'name': 'Synthetic', 'save_path': '/data/Downloads'})
    assert (o['finding'], o['human_only'], o['size_gb']) == ('orphan_file', True, 3.5)
    assert (s['finding'], s['label_class'], s['files'], s['human_only']) == ('single_link', 'sonarr', 3, True)
    assert (u['finding'], u['human_only']) == ('unlabeled', False)
    assert 'Synthetic' not in json.dumps([o, s, u]) and '/data' not in json.dumps([o, s, u])


def test_queue_off_never_asks_and_shadow_asks_each_row(monkeypatch):
    advised = []
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append((kind, f, r)) or 'fix')
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'off')
    aw._audit_findings_queue({'advice_facts': [facts()]})
    assert advised == []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    aw._audit_findings_queue({'advice_facts': [facts(), facts(finding='orphan_file', human_only=True)]})
    aw._audit_findings_queue({'advice_facts': []})
    aw._audit_findings_queue(None)
    assert [a[0] for a in advised] == [K, K] and all(a[2] == 'ask' for a in advised)


def test_cap_and_errors(monkeypatch):
    advised = []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append(1) or 'ask')
    monkeypatch.setenv('AUDIT_FINDINGS_LLM_MAX', '2')
    aw._audit_findings_shadow([facts() for _ in range(5)])
    assert len(advised) == 2
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    aw._audit_findings_shadow([facts()])


def test_backtest_counts_and_pass_bar(tmp_path, capsys):
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in [facts(), {'facts': facts(), 'expected': 'fix'}]) + '\njunk\n')
    replies = iter([ASK, FIX])
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=2 disagree=0 no_answer=0' in capsys.readouterr().out
    assert bt.backtest([facts()], lambda prompt: 'junk')['no_answer'] == 1
    # a model that says fix on a human-only row is held to ask by the same hard rules
    res = bt.backtest([facts(human_only=True)], lambda prompt: FIX)
    assert res['agree'] == 1 and res['pair:ask->ask'] == 1


def test_listed_by_llm_layers_and_llm_stats():
    assert la.layer_status({'LLM_DECISIONS': 'shadow'})['modes'][K] == 'off'
    assert K in la.llm_stats([])
    client = aw.app.test_client()
    assert K in client.get('/llm-layers').get_json()['modes']
    assert K in client.get('/llm-stats').get_json()['kinds']
