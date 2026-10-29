import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    import llm_decide
    return llm_decide


GOOD = {'action': 'allow', 'confidence': 0.9, 'reason': 'r'}


def _patch(m, monkeypatch):
    monkeypatch.setattr(m, 'build_prompt', lambda kind, ctx: 'PROMPT:' + kind)
    monkeypatch.setattr(m, 'parse_decision', lambda kind, raw: GOOD if raw == 'REPLY' else None)


def test_off_unknown_mode_and_missing_transport_skip_the_call(monkeypatch):
    m = _load()
    _patch(m, monkeypatch)
    calls = []

    def transport(prompt):
        calls.append(prompt)
        return 'REPLY'

    assert m.decide('upgrade_gate', {}, transport, mode='off') is None
    assert m.decide('upgrade_gate', {}, transport, mode='bogus') is None
    assert m.decide('upgrade_gate', {}, None, mode='shadow') is None
    assert calls == []


def test_shadow_and_enforce_call_transport_with_built_prompt(monkeypatch):
    m = _load()
    _patch(m, monkeypatch)
    seen = []

    def transport(prompt):
        seen.append(prompt)
        return 'REPLY'

    assert m.decide('upgrade_gate', {'a': 1}, transport) == GOOD
    assert m.decide('upgrade_gate', {'a': 1}, transport, mode='enforce') == GOOD
    assert seen == ['PROMPT:upgrade_gate', 'PROMPT:upgrade_gate']


def test_transport_exception_returns_none(monkeypatch):
    m = _load()
    _patch(m, monkeypatch)

    def boom(prompt):
        raise ConnectionError('ollama down')

    assert m.decide('upgrade_gate', {}, boom) is None


def test_build_prompt_exception_returns_none_without_calling_transport(monkeypatch):
    m = _load()
    calls = []

    def bad_prompt(kind, ctx):
        raise ValueError('unknown kind')

    monkeypatch.setattr(m, 'build_prompt', bad_prompt)
    assert m.decide('bogus', {}, lambda p: calls.append(p) or 'REPLY') is None
    assert calls == []


def test_unparsable_reply_gives_none(monkeypatch):
    m = _load()
    _patch(m, monkeypatch)
    assert m.decide('upgrade_gate', {}, lambda p: 'garbage') is None
    assert m.decide('upgrade_gate', {}, lambda p: None) is None
