"""Per-kind LLM modes: LLM_MODE_<KIND> sets one kind on its own; LLM_DECISIONS is the default and the kill switch."""
import importlib.util
import json
import os
import tempfile
import time
import types

import pytest

import llm_advisor as la

HERE = os.path.dirname(os.path.abspath(__file__))
GOOD = '{"action": "reject", "confidence": 0.9, "reason": "r"}'


@pytest.mark.parametrize('env,kind,want', [
    ({}, 'import_identity', 'off'),
    ({'LLM_DECISIONS': 'shadow'}, 'import_identity', 'shadow'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_IMPORT_IDENTITY': 'enforce'}, 'import_identity', 'enforce'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_IMPORT_IDENTITY': ' Enforce '}, 'import_identity', 'enforce'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_IMPORT_IDENTITY': 'enforce'}, 'upgrade_gate', 'shadow'),
    ({'LLM_DECISIONS': 'enforce', 'LLM_MODE_UPGRADE_GATE': 'shadow'}, 'upgrade_gate', 'shadow'),
    ({'LLM_DECISIONS': 'enforce', 'LLM_MODE_UPGRADE_GATE': 'off'}, 'upgrade_gate', 'off'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_DUPE_CONFIRM': 'bogus'}, 'dupe_confirm', 'shadow'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_DUPE_CONFIRM': ''}, 'dupe_confirm', 'shadow'),
    # Kill switch: off at the top beats any per-kind value.
    ({'LLM_DECISIONS': 'off', 'LLM_MODE_IMPORT_IDENTITY': 'enforce'}, 'import_identity', 'off'),
    ({'LLM_DECISIONS': 'nonsense', 'LLM_MODE_IMPORT_IDENTITY': 'enforce'}, 'import_identity', 'off'),
    ({'LLM_DECISIONS': 'enforce'}, None, 'enforce'),
])
def test_kind_mode(env, kind, want):
    assert la.kind_mode(kind, env) == want


def _advise(env_extra):
    d = tempfile.mkdtemp(prefix='kindmode-')
    calls = []

    def local(prompt):
        calls.append('local')
        return GOOD

    env = dict({'LLM_LOCAL_WAIT_MIN': '0', 'LLM_PENDING_PATH': os.path.join(d, 'pending.json')}, **env_extra)
    out = la.advise('upgrade_gate', {'title': 'T', 'size_gb': 12}, 'allow', log_path=os.path.join(d, 'log.jsonl'),
                    env=env, layers={'local': la.Layer('local', local, 'm-local')}, now=time.time)
    with open(os.path.join(d, 'log.jsonl'), encoding='utf-8') as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    return out, calls, rows


def test_advise_uses_kind_override_over_global_shadow():
    out, calls, rows = _advise({'LLM_DECISIONS': 'shadow', 'LLM_MODE_UPGRADE_GATE': 'enforce'})
    assert out == 'reject' and calls == ['local']
    assert rows[-1]['mode'] == 'enforce'


def test_advise_kind_shadow_under_global_enforce_keeps_rules():
    out, calls, rows = _advise({'LLM_DECISIONS': 'enforce', 'LLM_MODE_UPGRADE_GATE': 'shadow'})
    assert out == 'allow' and calls == ['local']
    assert rows[-1]['mode'] == 'shadow'


@pytest.mark.parametrize('env,want', [
    ({'LLM_DECISIONS': 'shadow'}, 'off'),                                    # new kinds wait for their back-test
    ({'LLM_DECISIONS': 'enforce'}, 'off'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_TORRENT_ERROR': 'shadow'}, 'shadow'),
    ({'LLM_DECISIONS': 'shadow', 'LLM_MODE_TORRENT_ERROR': 'bogus'}, 'off'),
    ({'LLM_DECISIONS': 'off', 'LLM_MODE_TORRENT_ERROR': 'shadow'}, 'off'),   # the kill switch still wins
])
def test_opt_in_kinds_stay_off_until_set(env, want):
    assert la.kind_mode('torrent_error', env) == want
    env2 = {k.replace('TORRENT_ERROR', 'UNREGISTERED_TRIAGE'): v for k, v in env.items()}
    assert la.kind_mode('unregistered_triage', env2) == want
    assert la.kind_mode('dupe_confirm', {'LLM_DECISIONS': 'shadow'}) == 'shadow'   # the first three follow


def test_advise_global_off_never_asks():
    d = tempfile.mkdtemp(prefix='kindmode-')
    calls = []
    out = la.advise('upgrade_gate', {'title': 'T'}, 'allow', log_path=os.path.join(d, 'log.jsonl'),
                    env={'LLM_DECISIONS': 'off', 'LLM_MODE_UPGRADE_GATE': 'enforce'},
                    layers={'local': la.Layer('local', lambda p: calls.append(1) or GOOD, 'm')}, now=time.time)
    assert out == 'allow' and calls == [] and not os.path.exists(os.path.join(d, 'log.jsonl'))


def test_layer_status_reports_modes_and_pays_only_for_enforcing_kinds():
    st = la.layer_status({'LLM_DECISIONS': 'shadow', 'LLM_MODE_UPGRADE_GATE': 'enforce',
                          'DARKBLOOM_BASE_URL': 'http://local.invalid', 'DARKBLOOM_CLOUD_API_KEY': 'k'})
    assert st['mode'] == 'shadow'
    assert st['modes'] == {'dupe_confirm': 'shadow', 'import_identity': 'shadow', 'torrent_error': 'off', 'stuck_import': 'off', 'stuck_upgrade_tag': 'off', 'duplicate_folder': 'off', 'audit_findings': 'off',
                           'unregistered_triage': 'off', 'upgrade_gate': 'enforce'}
    assert st['kinds']['upgrade_gate'] == ['local', 'paid']
    assert st['kinds']['import_identity'] == ['local']
    assert st['kinds']['dupe_confirm'] == ['local']


@pytest.fixture
def aw(monkeypatch):
    monkeypatch.setenv('DELUGE_PASSWORD', 'x')
    spec = importlib.util.spec_from_file_location('arr_webhook_kindmode', os.path.join(HERE, 'arr-webhook.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_webhook_mode_helpers_follow_kind(aw, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'shadow')
    monkeypatch.setenv('LLM_MODE_DUPE_CONFIRM', 'enforce')
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'enforce')
    assert aw._llm_mode() == 'shadow'
    assert aw._llm_mode('dupe_confirm') == 'enforce'
    assert aw._dupe_confirm_mode() == 'enforce'
    monkeypatch.delenv('LLM_MODE_DUPE_CONFIRM')
    assert aw._dupe_confirm_mode() == 'shadow'
    monkeypatch.setenv('LLM_MODE_DUPE_CONFIRM', 'off')
    assert aw._dupe_confirm_mode() == 'off'


def test_late_gate_enforces_when_only_upgrade_gate_enforces(aw, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'shadow')
    monkeypatch.setenv('LLM_MODE_UPGRADE_GATE', 'enforce')
    calls = []

    def fake_advise(kind, facts, rules, info=None):
        info['pending'] = False
        return 'reject'
    monkeypatch.setattr(aw, '_llm_advise', fake_advise)
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: calls.append(('remove', h)))
    monkeypatch.setattr(aw, 'record_activity', lambda c, s: calls.append(('act', c)))
    monkeypatch.setattr(aw.session, 'post', lambda *a, **k: types.SimpleNamespace())
    monkeypatch.setattr(aw.time, 'sleep', lambda s: None)
    aw._late_gate('Radarr', 'abc', 'T', {}, 'allow', 'radarr-upgrade', poll_s=0)
    assert ('remove', 'abc') in calls
    calls.clear()
    monkeypatch.setenv('LLM_MODE_UPGRADE_GATE', 'shadow')
    aw._late_gate('Radarr', 'abc', 'T', {}, 'allow', 'radarr-upgrade', poll_s=0)
    assert ('remove', 'abc') not in calls
