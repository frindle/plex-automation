"""stuck_import: shadow-only model advice when an import is still stuck after the rescue. Synthetic facts only."""
import json
import os
import tempfile
import time

import pytest

import backtest_stuck_import as bt
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

ASK = '{"action": "ask", "confidence": 0.9, "reason": "r"}'
WAIT = '{"action": "wait", "confidence": 0.9, "reason": "r"}'


def facts(**kw):
    f = {'service': 'Sonarr', 'reason_class': 'unknown_media', 'rescue_class': 'no_clean_candidate',
         'age_hours': 5.0, 'rules_action': 'ask'}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def inline_shadow(monkeypatch):
    monkeypatch.setattr(aw, '_run_llm_shadow', lambda name, fn: fn())


def test_kind_prompt_parse_hard_rules():
    assert ld.KINDS['stuck_import'] == ('wait', 'retry_import', 'ask')
    p = ld.build_prompt('stuck_import', facts())
    assert 'retry_import' in p and '"reason_class": "unknown_media"' in p and 'Policy:' in p
    assert ld.parse_decision('stuck_import', WAIT)['action'] == 'wait'
    assert ld.parse_decision('stuck_import', '{"action": "delete", "confidence": 0.9, "reason": "r"}') is None
    assert ld.apply_hard_rules('stuck_import', 'wait', facts()) == ('wait', None)
    assert ld.apply_hard_rules('stuck_import', 'bogus', facts()) == ('ask', 'invalid_action')


def test_advisor_config():
    assert 'stuck_import' in la.FREE_ONLY_KINDS and 'stuck_import' in la.CACHE_KINDS
    assert 'stuck_import' in la.OPT_IN_KINDS and 'stuck_import' in la.UNRAID_KINDS.split(',')
    assert 'ask' not in la.UNRAID_DECISIVE['stuck_import']
    assert la.kind_mode('stuck_import', {'LLM_DECISIONS': 'shadow'}) == 'off'
    assert la.kind_mode('stuck_import', {'LLM_DECISIONS': 'shadow', 'LLM_MODE_STUCK_IMPORT': 'shadow'}) == 'shadow'


def _advise(f, reply, d):
    calls = []

    def local(prompt):
        calls.append(1)
        return reply
    env = {'LLM_DECISIONS': 'shadow', 'LLM_MODE_STUCK_IMPORT': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0',
           'LLM_PENDING_PATH': os.path.join(d, 'p.json'), 'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
    out = la.advise('stuck_import', f, 'ask', log_path=os.path.join(d, 'log.jsonl'), env=env,
                    layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    return out, calls


def test_shadow_keeps_rules_action_logs_and_caches():
    d = tempfile.mkdtemp(prefix='si-')
    out, c1 = _advise(facts(), WAIT, d)
    assert out == 'ask' and c1 == [1]
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == 'stuck_import' and row['mode'] == 'shadow'
    _, c2 = _advise(facts(age_hours=9.0), WAIT, d)   # age drifts, same case: cached
    assert c2 == []


def test_classifiers():
    assert aw._import_reason_class('Sample file detected') == 'sample'
    assert aw._import_reason_class('Not an upgrade for existing episode') == 'not_upgrade'
    assert aw._import_reason_class('Unknown Series') == 'unknown_media'
    assert aw._import_reason_class('') == 'other'
    assert aw._import_rescue_class('no clean candidate (2 total)') == 'no_clean_candidate'
    assert aw._import_rescue_class('manualimport lookup failed: x') == 'lookup_failed'
    assert aw._import_rescue_class('ManualImport POST failed: x') == 'import_failed'


def test_facts_have_no_names_and_age_from_added():
    added = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 7200))
    f = aw._stuck_import_facts('Radarr', {'title': 'Synthetic.Title', 'downloadId': 'ABC', 'added': added},
                               'Unknown Movie', 'no clean candidate')
    assert 1.9 <= f['age_hours'] <= 2.2 and f['service'] == 'Radarr' and f['rules_action'] == 'ask'
    assert 'Synthetic' not in json.dumps(f) and 'ABC' not in json.dumps(f)
    assert aw._stuck_import_facts('Radarr', {}, '', '')['age_hours'] is None


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def _world(monkeypatch, mode, answer='wait'):
    calls, pushes, posts = [], [], []
    q = [{'id': 1, 'downloadId': 'H1', 'movieId': 4, 'title': 'Synthetic.Movie.2001', 'trackedDownloadState': 'importBlocked',
          'statusMessages': [{'messages': ['Unknown Movie']}]}]

    def get(url, **k):
        return _Resp({'records': q} if url.endswith('/queue') else [])

    def post(url, **k):
        posts.append(url)
        return _Resp({})
    monkeypatch.setattr(aw, 'RADARR_API_KEY', 'rk')
    monkeypatch.setattr(aw, 'SONARR_API_KEY', '')
    monkeypatch.setattr(aw, '_ib_seen', {})
    monkeypatch.setattr(aw, 'requests', type('R', (), {'get': staticmethod(get), 'post': staticmethod(post)})())
    monkeypatch.setattr(aw, 'send_pushover', lambda title, message, url=None: pushes.append(title))
    monkeypatch.setattr(aw, 'record_activity', lambda *a, **k: None)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: mode)

    def adv(kind, f, rules, info=None):
        calls.append((kind, f, rules))
        if info is not None:
            info['layer'] = 'local'
        return answer
    monkeypatch.setattr(aw, '_llm_advise', adv)
    monkeypatch.setattr(aw, '_try_radarr_manual_import', lambda rec: (False, 'no clean candidate (0 total)'))
    return calls, pushes, posts


def test_wiring_off_never_asks_and_notification_unchanged(monkeypatch):
    calls, pushes, posts = _world(monkeypatch, 'off')
    aw.check_import_blocked()
    assert calls == [] and pushes == ['Radarr import stuck'] and posts == []


def test_wiring_shadow_asks_after_notify_and_changes_nothing(monkeypatch):
    calls, pushes, posts = _world(monkeypatch, 'shadow', answer='retry_import')
    aw.check_import_blocked()
    assert pushes == ['Radarr import stuck'] and posts == []
    (kind, f, rules), = [c for c in calls if c[0] == 'stuck_import']
    assert rules == 'ask' and f['reason_class'] == 'unknown_media' and f['rescue_class'] == 'no_clean_candidate'
    assert 'Synthetic' not in json.dumps(f) and 'H1' not in json.dumps(f)
    aw.check_import_blocked()   # second pass: notification deduped, no second question
    assert len(calls) == 1 and pushes == ['Radarr import stuck']


def test_wiring_cap_and_errors(monkeypatch):
    calls, _, _ = _world(monkeypatch, 'shadow')
    monkeypatch.setenv('STUCK_IMPORT_LLM_MAX', '2')
    aw._stuck_import_shadow([facts() for _ in range(5)])
    assert len(calls) == 2
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    aw._stuck_import_shadow([facts()])   # swallowed


def test_backtest_counts(tmp_path, capsys):
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in [facts(), {'facts': facts(), 'expected': 'wait'}]) + '\njunk\n')
    replies = iter([ASK, WAIT])
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=2 disagree=0 no_answer=0' in capsys.readouterr().out
    assert bt.backtest([facts()], lambda prompt: 'junk')['no_answer'] == 1
