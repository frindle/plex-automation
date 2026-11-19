"""torrent_error: shadow-only model decision at the deluge_error_repair hand-offs. Synthetic facts only."""
import json
import os
import tempfile
import time

import backtest_torrent_error as bt
import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')

ASK = '{"action": "ask", "confidence": 0.9, "reason": "r"}'
WAIT = '{"action": "wait", "confidence": 0.9, "reason": "r"}'
DEL = '{"action": "remove_and_research", "confidence": 0.95, "reason": "gone"}'


def facts(**kw):
    f = {'case': 'missing', 'rules_action': 'ask', 'state': 'Error', 'progress': 0.0, 'seeded_days': 3.0,
         'age_days': 9.0, 'tracker_status': 'Announce OK', 'library_file_exists': False}
    f.update(kw)
    return f


def test_kind_and_prompt():
    assert ld.KINDS['torrent_error'] == ('recheck', 'resume', 'relink', 'wait', 'remove_and_research', 'ask')
    p = ld.build_prompt('torrent_error', facts())
    assert 'remove_and_research' in p and '"case": "missing"' in p and 'Policy:' in p


def test_parse():
    assert ld.parse_decision('torrent_error', WAIT)['action'] == 'wait'
    assert ld.parse_decision('torrent_error', '{"action": "delete", "confidence": 0.9, "reason": "r"}') is None
    assert ld.parse_decision('torrent_error', 'junk') is None


def test_hard_rules_model_never_deletes_unless_rules_do():
    assert ld.apply_hard_rules('torrent_error', 'remove_and_research', facts()) == ('ask', 'no_model_delete')
    assert ld.apply_hard_rules('torrent_error', 'remove_and_research', {}) == ('ask', 'no_model_delete')
    assert ld.apply_hard_rules('torrent_error', 'remove_and_research',
                               facts(rules_action='remove_and_research')) == ('remove_and_research', None)
    assert ld.apply_hard_rules('torrent_error', 'wait', facts()) == ('wait', None)
    assert ld.apply_hard_rules('torrent_error', 'bogus', facts()) == ('ask', 'invalid_action')


def test_advisor_config():
    assert 'torrent_error' in la.FREE_ONLY_KINDS and 'torrent_error' in la.CACHE_KINDS
    assert 'torrent_error' in la.UNRAID_KINDS.split(',')
    assert 'ask' not in la.UNRAID_DECISIVE['torrent_error']
    assert la.PENDING_ACTION['torrent_error'] == 'wait'
    assert la.answer_ttl_s('torrent_error', {}) == 24 * 3600.0


def _advise(f, reply, d):
    calls = []

    def local(prompt):
        calls.append(1)
        return reply
    env = {'LLM_DECISIONS': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0', 'LLM_PENDING_PATH': os.path.join(d, 'p.json'),
           'LLM_ANSWERS_PATH': os.path.join(d, 'a.json')}
    out = la.advise('torrent_error', f, 'ask', log_path=os.path.join(d, 'log.jsonl'), env=env,
                    layers={'local': la.Layer('local', local, 'm')}, now=time.time)
    return out, calls


def test_shadow_keeps_rules_action_and_logs():
    d = tempfile.mkdtemp(prefix='te-')
    out, calls = _advise(facts(), DEL, d)
    assert out == 'ask' and calls == [1]
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        row = json.loads(fh.readline())
    assert row['kind'] == 'torrent_error' and row['mode'] == 'shadow'


def test_answer_cache_does_not_reask():
    d = tempfile.mkdtemp(prefix='te-')
    f = facts()
    _, c1 = _advise(f, WAIT, d)
    _, c2 = _advise(dict(f, seeded_days=4.0, age_days=10.0), WAIT, d)  # volatile facts differ, same torrent
    assert c1 == [1] and c2 == []


# ---- webhook seam ----

class Spy:
    def __init__(self, layer='local'):
        self.calls, self.layer = [], layer

    def __call__(self, kind, facts_, rules, info=None):
        self.calls.append((kind, facts_, rules))
        if info is not None:
            info['layer'] = self.layer
        return 'wait'


def info():
    return {'state': 'Error', 'progress': 0.0, 'seeding_time': 3 * 86400, 'time_added': time.time() - 9 * 86400,
            'tracker_status': 'Announce OK', 'name': 'Synthetic.Name', 'save_path': '/x'}


def test_seam_off_never_asks(monkeypatch):
    spy = Spy()
    monkeypatch.setattr(aw, '_llm_advise', spy)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'off')
    aw._torrent_error_shadow({'asked': 0}, False, 'missing', info())
    assert spy.calls == []


def test_seam_shadow_asks_with_facts_only_and_rules_action(monkeypatch):
    spy = Spy()
    monkeypatch.setattr(aw, '_llm_advise', spy)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    aw._torrent_error_shadow({'asked': 0}, False, 'missing', info(), library_file_exists=False)
    (kind, f, rules), = spy.calls
    assert kind == 'torrent_error' and rules == 'ask' and f['case'] == 'missing' and f['seeded_days'] == 3.0
    assert 'Synthetic.Name' not in json.dumps(f) and '/x' not in json.dumps(f)


def test_seam_enforce_still_only_logs(monkeypatch):
    spy = Spy()
    monkeypatch.setattr(aw, '_llm_advise', spy)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'enforce')
    assert aw._torrent_error_shadow({'asked': 0}, False, 'partial', info()) is None
    assert len(spy.calls) == 1


def test_seam_dry_run_never_asks(monkeypatch):
    spy = Spy()
    monkeypatch.setattr(aw, '_llm_advise', spy)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    aw._torrent_error_shadow({'asked': 0}, True, 'missing', info())
    assert spy.calls == []


def test_seam_per_run_cap_and_cache_refund(monkeypatch):
    spy = Spy()
    monkeypatch.setattr(aw, '_llm_advise', spy)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    monkeypatch.setenv('TORRENT_ERROR_LLM_MAX', '2')
    st = {'asked': 0}
    for _ in range(5):
        aw._torrent_error_shadow(st, False, 'missing', info())
    assert len(spy.calls) == 2
    cached = Spy(layer='cache')
    monkeypatch.setattr(aw, '_llm_advise', cached)
    st = {'asked': 0}
    for _ in range(5):
        aw._torrent_error_shadow(st, False, 'missing', info())
    assert len(cached.calls) == 5  # cached answers are free


def test_seam_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('x')
    monkeypatch.setattr(aw, '_llm_advise', boom)
    monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: 'shadow')
    aw._torrent_error_shadow({'asked': 0}, False, 'missing', info())


def test_repair_result_unchanged_by_shadow(monkeypatch):
    """Same deluge_error_repair result, Deluge mutations and activity rows with the seam asking (a model that says
    remove) or off. Covers the library_error and missing hand-offs."""
    torrents = {
        'E1': {'name': 'Gone.Synthetic', 'state': 'Error', 'message': 'x', 'save_path': '/data/dl', 'label': 'l',
               'progress': 0.0},
        'E2': {'name': 'Lib.Synthetic', 'state': 'Error', 'message': 'x', 'save_path': '/data/media/Lib',
               'label': 'l', 'progress': 0.0},
    }

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'result': torrents}

    def run(mode, answer):
        muts, acts = [], []
        monkeypatch.setattr(aw, 'deluge_login', lambda: None)
        monkeypatch.setattr(aw.session, 'post', lambda *a, **k: Resp())
        monkeypatch.setattr(aw, 'deluge_pause_torrent', lambda h: muts.append(('pause', h)))
        monkeypatch.setattr(aw, 'restore_moved_files', lambda **k: {})
        monkeypatch.setattr(aw, 'is_library_path', lambda p: p.startswith('/data/media'))
        monkeypatch.setattr(aw, '_deluge_path_exists', lambda p: False)
        monkeypatch.setattr(aw, '_find_torrent_data', lambda n, r: None)
        monkeypatch.setattr(aw, 'record_activity', lambda cat, summ: acts.append((cat, summ)))
        monkeypatch.setattr(aw, '_reported_deluge_errors', set())
        monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: mode)
        monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: answer)
        res = aw.deluge_error_repair()
        return res, muts, acts
    off = run('off', 'ask')
    assert off[1] and off[2]
    assert off == run('shadow', 'remove_and_research')


# ---- backtest CLI ----

def test_backtest_counts_no_network(tmp_path, capsys):
    rows = [facts(), {'facts': facts(case='partial'), 'expected': 'wait'}, facts(rules_action='ask')]
    p = tmp_path / 'rows.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\nnot json\n')
    replies = iter([ASK, WAIT, DEL])  # DEL is turned into ask by the hard rule -> agrees with rules ask
    assert bt.main([str(p)], transport=lambda prompt: next(replies)) == 0
    assert 'agree=3 disagree=0 no_answer=0' in capsys.readouterr().out


def test_backtest_disagree_and_no_answer():
    replies = ['junk', WAIT]
    res = bt.backtest([facts(), facts()], lambda prompt: replies.pop())
    assert res['disagree'] == 1 and res['no_answer'] == 1 and res['agree'] == 0
