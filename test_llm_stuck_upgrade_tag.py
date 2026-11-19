"""stuck_upgrade_tag: shadow-only model advice when an upgrade import can't flip its label. Synthetic facts only."""
import json
import os
import tempfile
import time

import pytest

import backtest_stuck_upgrade_tag as bt
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

K = 'stuck_upgrade_tag'
WAIT = '{"action": "wait", "confidence": 0.9, "reason": "r"}'
SWEEP = '{"action": "run_sweep", "confidence": 0.9, "reason": "r"}'
ASK = '{"action": "ask", "confidence": 0.9, "reason": "r"}'


def facts(**kw):
    f = {'service': 'Radarr', 'season_pack': False, 'has_download_id': True, 'proper_repack': False,
         'flip_result': 'not_found', 'upgrade_labeled': 2, 'torrents_known': 40, 'rules_action': 'ask'}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def inline_shadow(monkeypatch):
    monkeypatch.setattr(aw, '_run_llm_shadow', lambda name, fn: fn())


def test_kind_prompt_parse_hard_rules():
    assert ld.KINDS[K] == ('wait', 'run_sweep', 'ask')
    p = ld.build_prompt(K, facts())
    assert 'run_sweep' in p and '"upgrade_labeled": 2' in p and 'Policy:' in p
    assert ld.parse_decision(K, SWEEP)['action'] == 'run_sweep'
    assert ld.parse_decision(K, '{"action": "remove", "confidence": 0.9, "reason": "r"}') is None
    assert ld.apply_hard_rules(K, 'run_sweep', facts()) == ('run_sweep', None)
    assert ld.apply_hard_rules(K, 'bogus', facts()) == ('ask', 'invalid_action')


def test_advisor_config():
    assert K in la.FREE_ONLY_KINDS and K in la.CACHE_KINDS and K in la.OPT_IN_KINDS
    assert K in la.UNRAID_KINDS.split(',')
    assert la.UNRAID_DECISIVE[K] == frozenset({'wait', 'run_sweep'})
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow'}) == 'off'
    assert la.kind_mode(K, {'LLM_DECISIONS': 'shadow', 'LLM_MODE_STUCK_UPGRADE_TAG': 'shadow'}) == 'shadow'


def test_shadow_keeps_rules_action_logs_and_caches():
    d = tempfile.mkdtemp(prefix='sut-')
    calls = []

    def run(f):
        def local(prompt):
            calls.append(1)
            return SWEEP
        env = {'LLM_DECISIONS': 'shadow', 'LLM_MODE_STUCK_UPGRADE_TAG': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0',
               'LLM_PENDING_PATH': os.path.join(d, 'p.json'), 'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
        return la.advise(K, f, 'ask', log_path=os.path.join(d, 'log.jsonl'), env=env,
                         layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    assert run(facts()) == 'ask' and len(calls) == 1
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == K and row['mode'] == 'shadow'
    run(facts())
    assert len(calls) == 1


def _drive(monkeypatch, mode):
    advised, pushes = [], []
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: mode)
    monkeypatch.setattr(aw, '_llm_advise', lambda kind, f, r, info=None: advised.append((kind, f, r)) or 'run_sweep')
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'ensure_label_exists', lambda: None)
    monkeypatch.setattr(aw, 'get_radarr_movie_titles', lambda i: {'synthetic movie'})
    monkeypatch.setattr(aw, 'relabel_download_to_base', lambda h, s: ('not_found', None))
    monkeypatch.setattr(aw, 'get_all_torrents', lambda *a, **k: {
        'aa': {'name': 'Synthetic.One', 'label': aw.RADARR_UPG_LABEL}, 'bb': {'name': 'Synthetic.Two', 'label': 'radarr'}})
    monkeypatch.setattr(aw, 'send_pushover', lambda **k: pushes.append(k['title']))
    monkeypatch.setattr(aw, 'record_activity', lambda *a, **k: None)
    aw.handle_upgrade_import({'downloadId': 'ZZ' + str(time.time()), 'movieFile': {'path': '/m/x.mkv'},
                              'movie': {'id': 3, 'title': 'Synthetic Movie', 'year': 2001}}, 'Radarr')
    return advised, pushes


def test_handler_off_never_asks_notification_unchanged(monkeypatch):
    advised, pushes = _drive(monkeypatch, 'off')
    assert advised == [] and pushes == ['Radarr: upgrade tag may be stuck']


def test_handler_shadow_asks_with_facts_only(monkeypatch):
    advised, pushes = _drive(monkeypatch, 'shadow')
    assert pushes == ['Radarr: upgrade tag may be stuck']
    (kind, f, rules), = advised
    assert kind == K and rules == 'ask' and f['upgrade_labeled'] == 1 and f['torrents_known'] == 2
    assert f['has_download_id'] is True and 'Synthetic' not in json.dumps(f) and 'ZZ' not in json.dumps(f)


def test_seam_swallows_errors(monkeypatch):
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    aw._stuck_upgrade_tag_ask('Radarr', False, 'x', False, 'not_found', {})


def test_backtest_counts(tmp_path, capsys):
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in [facts(), {'facts': facts(), 'expected': 'wait'}]) + '\njunk\n')
    replies = iter([ASK, WAIT])
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=2 disagree=0 no_answer=0' in capsys.readouterr().out
    assert bt.backtest([facts()], lambda prompt: 'junk')['no_answer'] == 1
