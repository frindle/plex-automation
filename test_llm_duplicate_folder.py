"""duplicate_folder: shadow-only model advice on duplicate media folder findings. Synthetic facts only."""
import json
import os
import tempfile
import time

import pytest

import backtest_duplicate_folder as bt
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

K = 'duplicate_folder'
LEAVE = '{"action": "leave", "confidence": 0.9, "reason": "r"}'
MERGE = '{"action": "merge", "confidence": 0.9, "reason": "r"}'
ASK = '{"action": "ask", "confidence": 0.9, "reason": "r"}'


def facts(**kw):
    f = {'media': 'movies', 'level': 'library_root', 'folder_count': 2, 'suffixes': ['none', 'paren_number'],
         'sizes_gb': [4.2, 0.0], 'empty_folders': 1, 'rules_action': 'ask'}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def inline_shadow(monkeypatch):
    monkeypatch.setattr(aw, '_run_llm_shadow', lambda name, fn: fn())


def test_kind_prompt_parse_hard_rules():
    assert ld.KINDS[K] == ('leave', 'merge', 'ask')
    p = ld.build_prompt(K, facts())
    assert 'merge' in p and '"folder_count": 2' in p and 'Policy:' in p
    assert ld.parse_decision(K, MERGE)['action'] == 'merge'
    assert ld.parse_decision(K, '{"action": "delete", "confidence": 0.9, "reason": "r"}') is None
    assert ld.apply_hard_rules(K, 'merge', facts()) == ('merge', None)
    assert ld.apply_hard_rules(K, 'leave', facts()) == ('leave', None)
    assert ld.apply_hard_rules(K, 'bogus', facts()) == ('ask', 'invalid_action')
    assert ld.apply_hard_rules(K, 'merge', facts(empty_folders=2)) == ('ask', 'merge_needs_content')
    assert ld.apply_hard_rules(K, 'merge', facts(folder_count=1)) == ('ask', 'merge_needs_content')
    assert ld.apply_hard_rules(K, 'merge', {}) == ('ask', 'merge_needs_content')


def test_advisor_config():
    assert K in la.FREE_ONLY_KINDS and K in la.CACHE_KINDS and K in la.OPT_IN_KINDS
    assert K in la.UNRAID_KINDS.split(',') and la.UNRAID_DECISIVE[K] == frozenset({'leave', 'merge'})
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow'}) == 'off'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow', 'LLM_MODE_DUPLICATE_FOLDER': 'shadow'}) == 'shadow'


def test_shadow_keeps_rules_action_logs_and_caches():
    d = tempfile.mkdtemp(prefix='df-')
    calls = []

    def run(f):
        def local(prompt):
            calls.append(1)
            return MERGE
        env = {'LLM_DECISIONS': 'shadow', 'LLM_MODE_DUPLICATE_FOLDER': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0',
               'LLM_PENDING_PATH': os.path.join(d, 'p.json'), 'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
        return la.advise(K, f, 'ask', log_path=os.path.join(d, 'log.jsonl'), env=env,
                         layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    assert run(facts()) == 'ask' and len(calls) == 1
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == K and row['mode'] == 'shadow'
    run(facts())
    assert len(calls) == 1


def finding(parent='/lib/movies', names=('Synthetic Movie (2001)', 'Synthetic Movie (2001) (1)'), sizes=(4.2, 0.0)):
    return {'key': 'k', 'parent': parent,
            'folders': [{'path': f'{parent}/{n}', 'size_gb': s} for n, s in zip(names, sizes)]}


def test_facts_have_no_names(monkeypatch):
    monkeypatch.setattr(aw, 'MEDIA_ROOTS', ['/lib/movies', '/lib/tv'])
    f = aw._duplicate_folder_facts(finding())
    assert f == {'media': 'movies', 'level': 'library_root', 'folder_count': 2,
                 'suffixes': ['none', 'paren_number'], 'sizes_gb': [4.2, 0.0], 'empty_folders': 1,
                 'rules_action': 'ask'}
    assert 'Synthetic' not in json.dumps(f) and '/lib' not in json.dumps(f)
    t = aw._duplicate_folder_facts(finding(parent='/lib/tv/Show', names=('Season 01', 'Season 01_2'), sizes=(1, 2)))
    assert t['media'] == 'tv' and t['level'] == 'nested' and t['suffixes'] == ['none', 'underscore_number']
    assert aw._dup_suffix_class('Movie - copy') == 'copy'


def _scan(monkeypatch, mode, findings):
    advised, acts = [], []
    monkeypatch.setattr(aw, 'MEDIA_ROOTS', ['/lib/movies'])
    monkeypatch.setattr(aw, 'find_duplicate_media_folders', lambda: findings)
    monkeypatch.setattr(aw, '_reported_dupe_folders', set())
    monkeypatch.setattr(aw, 'record_activity', lambda cat, s: acts.append((cat, s)))
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: mode)
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append((kind, f, r)) or 'merge')
    aw.duplicate_folder_scan()
    aw.duplicate_folder_scan()   # already reported: nothing new, no second question
    return advised, acts


def test_scan_off_never_asks(monkeypatch):
    advised, acts = _scan(monkeypatch, 'off', [finding()])
    assert advised == [] and len(acts) == 1


def test_scan_shadow_asks_new_findings_once_and_digest_unchanged(monkeypatch):
    advised, acts = _scan(monkeypatch, 'shadow', [finding()])
    assert len(acts) == 1 and acts[0][0] == 'dupe-folder'
    (kind, f, rules), = advised
    assert kind == K and rules == 'ask' and f['folder_count'] == 2


def test_cap_and_errors(monkeypatch):
    advised = []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append(1) or 'ask')
    monkeypatch.setenv('DUPLICATE_FOLDER_LLM_MAX', '2')
    aw._duplicate_folder_shadow([finding() for _ in range(5)])
    assert len(advised) == 2
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    aw._duplicate_folder_shadow([finding()])


def test_backtest_counts(tmp_path, capsys):
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in [facts(), {'facts': facts(), 'expected': 'leave'}]) + '\njunk\n')
    replies = iter([ASK, LEAVE])
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=2 disagree=0 no_answer=0' in capsys.readouterr().out
    assert bt.backtest([facts()], lambda prompt: 'junk')['no_answer'] == 1
