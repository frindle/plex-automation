"""unregistered_triage: shadow-only model triage of the unregistered-torrent sweep. Prompt, parsing, hard rules,
answer cache, per-sweep cap, mass-outage guard never asks, shadow leaves the rules behavior unchanged. Synthetic
facts, no network. Run: python -m pytest test_llm_unregistered.py"""
import json
import os
import tempfile
import time

import pytest

import backtest_unregistered as bt
import llm_advisor as la
import llm_decide as ld
import test_unregistered_sweep as tus

aw = tus.aw
A, B, C, D, E, F = tus.A, tus.B, tus.C, tus.D, tus.E, tus.F
NOW = tus.NOW
K = 'unregistered_triage'


def facts(**kw):
    f = {'tracker_message_class': 'unregistered', 'tracker_unregistered': 1, 'tracker_total': 40,
         'seed_days': 30, 'seed_floor_days': 21, 'library_copy': 'yes', 'library_hardlinked': None,
         'age_days': 60, 'label': 'sonarr', 'in_arr_queue': False, 'rules_action': 'remove'}
    f.update(kw)
    return f


def reply(action, conf=0.9):
    return json.dumps({'action': action, 'confidence': conf, 'reason': 'r'})


# ---- prompt / parse / hard rules ----

def test_prompt_has_menu_and_facts_only():
    p = ld.build_prompt(K, facts())
    for a in ('keep', 'remove_keep_library', 'remove_and_research', 'ask'):
        assert a in p
    assert '"tracker_unregistered": 1' in p and '/' not in json.dumps(facts())


def test_parse_valid_and_invalid():
    assert ld.parse_decision(K, reply('remove_and_research'))['action'] == 'remove_and_research'
    assert ld.parse_decision(K, reply('delete')) is None
    assert ld.parse_decision(K, 'nonsense') is None


@pytest.mark.parametrize('action,f,want', [
    ('remove_keep_library', facts(), ('remove_keep_library', None)),
    ('remove_and_research', facts(library_copy='no'), ('remove_and_research', None)),
    ('remove_keep_library', facts(library_copy='no'), ('ask', 'no_library_copy')),
    ('remove_keep_library', facts(library_copy='unknown'), ('ask', 'no_library_copy')),
    ('remove_keep_library', facts(rules_action='keep'), ('keep', 'rules_keep')),
    ('remove_and_research', facts(rules_action='keep', library_copy='no'), ('keep', 'rules_keep')),
    ('remove_and_research', {'library_copy': 'no'}, ('keep', 'rules_keep')),   # missing rules_action fails closed
    ('keep', facts(), ('keep', None)),
    ('ask', facts(rules_action='keep'), ('ask', None)),
])
def test_hard_rules(action, f, want):
    assert ld.apply_hard_rules(K, action, f) == want


def test_invalid_decision_resolves_to_rules_and_malformed_facts_ask():
    assert ld.resolve_action(K, 'remove', {'action': 'bogus', 'confidence': 1, 'reason': ''}, 'enforce') == 'remove'
    assert ld.apply_hard_rules(K, 'remove_keep_library', {'rules_action': 'remove', 'library_copy': ['x']})[0] == 'ask'


# ---- advisor ----

def test_kind_is_free_only_cached_unraid_pending_keep():
    assert K in la.FREE_ONLY_KINDS and K in la.CACHE_KINDS and K in la.UNRAID_KINDS.split(',')
    assert la.UNRAID_DECISIVE[K] == {'keep', 'remove_keep_library', 'remove_and_research'}
    assert la.PENDING_ACTION[K] == 'keep' and la.ANSWER_TTL_H[K] == 24.0


def test_advise_caches_answer_and_never_pays():
    d = tempfile.mkdtemp(prefix='unreg-')
    env = {'LLM_DECISIONS': 'shadow', 'LLM_LOCAL_WAIT_MIN': '0', 'LLM_PENDING_PATH': os.path.join(d, 'p.json'),
           'LLM_ANSWERS_PATH': os.path.join(d, 'a.json'), 'DARKBLOOM_CLOUD_API_KEY': 'k'}
    calls = []
    layers = {'local': la.Layer('local', lambda p: calls.append(1) or reply('remove_keep_library'), 'm'),
              'paid': la.Layer('paid', lambda p: calls.append('paid') or reply('keep'), 'p')}
    infos = []
    for _ in range(2):
        info = {}
        out = la.advise(K, facts(), 'remove', log_path=os.path.join(d, 'l.jsonl'), env=env, layers=layers,
                        now=time.time, info=info)
        infos.append(info)
        assert out == 'remove'            # shadow: the rules action stands
    assert calls == [1] and infos[1]['layer'] == 'cache'


# ---- sweep wiring ----

@pytest.fixture
def world(monkeypatch):
    def make(torrents, mode='shadow', **kw):
        w = tus.World(torrents, **kw)
        monkeypatch.setattr(aw, 'session', w)
        monkeypatch.setattr(aw.requests, 'get', w.get)
        monkeypatch.setattr(aw.requests, 'delete', w.delete)
        monkeypatch.setattr(aw, 'deluge_login', lambda: None)
        monkeypatch.setattr(aw, '_llm_mode', lambda kind=None: mode)
        w.asked = []

        def fake(kind, fcts, rules, info=None):
            w.asked.append((kind, fcts, rules))
            if info is not None:
                info['layer'] = 'local'
            return rules
        monkeypatch.setattr(aw, '_llm_advise', fake)
        aw._invalidate_snapshot()
        aw._unregistered_reported.clear()
        return w
    return make


def three(status='Unregistered'):
    return {A: tus.tor('One', status), B: tus.tor('Two', status, label='radarr'), E: tus.tor('Three', status)}


def test_mode_off_never_asks(world):
    w = world(three(), mode='off')
    aw.unregistered_sweep(apply=False, now=NOW)
    assert w.asked == []


def test_shadow_asks_each_with_facts_only_and_rules_unchanged(world):
    w = world(three(), imported={E})
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert len(w.asked) == 3 and {a[0] for a in w.asked} == {K}
    assert all(a[2] == 'remove' for a in w.asked)
    blob = json.dumps(w.asked)
    assert 'One' not in blob and 'Two' not in blob and '/data' not in blob
    assert sorted(r['outcome'] for r in rows) == ['removed', 'removed (files kept)', 'removed (files kept)']
    assert sorted(w.removed()) == sorted([(A, False), (B, False), (E, True)])
    assert not any('llm' in r for r in rows)


def test_rules_identical_with_mode_off_and_shadow(world):
    outs = []
    for mode in ('off', 'shadow', 'enforce'):
        w = world(three(), mode=mode, imported={E})
        rows = aw.unregistered_sweep(apply=True, now=NOW)
        outs.append((rows, sorted(w.removed()), w.deletes))
    assert outs[0] == outs[1] == outs[2]


def test_seed_floor_keep_is_the_rules_action(world):
    w = world({A: tus.tor('Young', 'Unregistered', seeded=3 * 86400, added_days=4)})
    aw.unregistered_sweep(apply=True, now=NOW)
    assert [a[2] for a in w.asked] == ['keep']


def test_mass_outage_trip_never_asks(world):
    t = {h: dict(tus.tor('T' + h[0], 'Unregistered'), tracker_host='trk.invalid') for h in (A, B, C, D, E)}
    t[F] = dict(tus.tor('Other', 'Unregistered'), tracker_host='other.invalid')
    w = world(t)
    aw.unregistered_sweep(apply=True, now=NOW)
    assert [a[1]['tracker_total'] for a in w.asked] == [1]   # only the other tracker's torrent


def test_per_sweep_cap(world, monkeypatch):
    monkeypatch.setenv('UNREGISTERED_LLM_MAX', '2')
    w = world(three())
    aw.unregistered_sweep(apply=False, now=NOW)
    assert len(w.asked) == 2


def test_cached_answers_do_not_use_the_cap(world, monkeypatch):
    monkeypatch.setenv('UNREGISTERED_LLM_MAX', '1')
    w = world(three())

    def fake(kind, fcts, rules, info=None):
        w.asked.append(kind)
        info['layer'] = 'cache'
        return rules
    monkeypatch.setattr(aw, '_llm_advise', fake)
    aw.unregistered_sweep(apply=False, now=NOW)
    assert len(w.asked) == 3


def test_model_failure_never_breaks_the_sweep(world, monkeypatch):
    w = world(three())
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 1 / 0)
    rows = aw.unregistered_sweep(apply=True, now=NOW)
    assert len(rows) == 3 and len(w.removed()) == 3


# ---- backtest CLI ----

def test_backtest_counts():
    rows = [facts(), facts(rules_action='keep'), facts(library_copy='no'), facts(tracker_total=7)]
    answers = iter([reply('remove_keep_library'), reply('remove_and_research'), reply('remove_keep_library'),
                    'garbage'])
    out = bt.backtest(rows, lambda p: next(answers))
    # row2: remove vs keep rules -> hard rule makes it keep -> agrees; row3 no library -> ask
    assert out == {'agree': 2, 'disagree': 0, 'ask': 1, 'none': 1, 'rows': 4}
