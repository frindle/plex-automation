import importlib.util
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def aw(monkeypatch):
    monkeypatch.setenv('DELUGE_PASSWORD', 'x')
    spec = importlib.util.spec_from_file_location('arr_webhook_latetest', os.path.join(HERE, 'arr-webhook.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _wire(aw, monkeypatch, answers):
    calls = []
    seq = iter(answers)

    def fake_advise(kind, facts, rules, info=None):
        a = next(seq)
        if a == 'pending':
            info['pending'] = True
            return rules
        info['pending'] = False
        return a
    monkeypatch.setattr(aw, '_llm_advise', fake_advise)
    monkeypatch.setattr(aw, 'deluge_login', lambda: calls.append('login'))
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: calls.append(('remove', h, remove_data)))
    monkeypatch.setattr(aw, 'ensure_label_exists_named', lambda l: calls.append(('ensure', l)))
    monkeypatch.setattr(aw, 'set_torrent_label', lambda h, l: calls.append(('label', h, l)))
    monkeypatch.setattr(aw, 'record_activity', lambda c, s: calls.append(('act', c)))
    monkeypatch.setattr(aw.session, 'post', lambda *a, **k: calls.append(('post', k['json']['method'])) or types.SimpleNamespace())
    monkeypatch.setattr(aw.time, 'sleep', lambda s: None)
    return calls


def test_late_reject_removes_torrent_in_enforce(aw, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'enforce')
    calls = _wire(aw, monkeypatch, ['pending', 'pending', 'reject'])
    aw._late_gate('Radarr', 'abc', 'T', {}, 'allow', 'radarr-upgrade', poll_s=0)
    assert ('remove', 'abc', True) in calls and ('act', 'llm-reject-late') in calls


def test_late_throttle_labels_when_rules_allowed(aw, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'enforce')
    calls = _wire(aw, monkeypatch, ['throttle'])
    aw._late_gate('Radarr', 'abc', 'T', {}, 'allow', 'radarr-upgrade', poll_s=0)
    assert ('label', 'abc', 'radarr-upgrade') in calls and ('post', 'core.queue_bottom') in calls


def test_late_shadow_only_logs(aw, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'shadow')
    calls = _wire(aw, monkeypatch, ['reject'])
    aw._late_gate('Radarr', 'abc', 'T', {}, 'allow', 'radarr-upgrade', poll_s=0)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] in ('remove', 'label')]
