import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    import llm_decide
    return llm_decide


def _lines(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines()]


def test_writes_one_jsonl_record_with_all_keys(tmp_path):
    m = _load()
    p = tmp_path / 'd.jsonl'
    dec = {'action': 'reject', 'confidence': 0.9, 'reason': 'worse audio'}
    assert m.log_decision(str(p), 'upgrade_gate', {'title': 'T'}, 'allow', dec, 'shadow') is True
    rows = _lines(p)
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {'ts', 'kind', 'context_summary', 'rules_action', 'llm_action', 'confidence', 'reason', 'mode', 'agree'}
    assert (r['kind'], r['rules_action'], r['llm_action'], r['mode']) == ('upgrade_gate', 'allow', 'reject', 'shadow')
    assert r['confidence'] == 0.9 and r['reason'] == 'worse audio' and r['context_summary'] == {'title': 'T'}
    assert r['agree'] is False
    assert r['ts'].endswith('+00:00') or r['ts'].endswith('Z')


def test_agree_true_when_model_matches_rules_and_false_without_decision(tmp_path):
    m = _load()
    p = tmp_path / 'd.jsonl'
    assert m.log_decision(str(p), 'upgrade_gate', {}, 'allow', {'action': 'allow', 'confidence': 0.8, 'reason': 'r'}, 'enforce')
    assert m.log_decision(str(p), 'upgrade_gate', {}, 'allow', None, 'shadow')
    a, b = _lines(p)
    assert a['agree'] is True
    assert b['agree'] is False and b['llm_action'] is None and b['confidence'] is None and b['reason'] is None


def test_appends_instead_of_overwriting(tmp_path):
    m = _load()
    p = tmp_path / 'd.jsonl'
    for i in range(3):
        assert m.log_decision(str(p), 'dupe_keeper', {'i': i}, 'keep_first', None, 'shadow') is True
    assert [r['context_summary']['i'] for r in _lines(p)] == [0, 1, 2]
    assert p.read_text().endswith('\n')


def test_empty_path_and_missing_directory_return_false_without_creating(tmp_path):
    m = _load()
    assert m.log_decision('', 'upgrade_gate', {}, 'allow', None, 'shadow') is False
    assert m.log_decision(None, 'upgrade_gate', {}, 'allow', None, 'shadow') is False
    nope = tmp_path / 'missing' / 'd.jsonl'
    assert m.log_decision(str(nope), 'upgrade_gate', {}, 'allow', None, 'shadow') is False
    assert not (tmp_path / 'missing').exists()


def test_non_serialisable_summary_is_stringified(tmp_path):
    m = _load()
    p = tmp_path / 'd.jsonl'
    assert m.log_decision(str(p), 'upgrade_gate', {'p': Path('/x/y')}, 'allow', None, 'shadow') is True
    assert _lines(p)[0]['context_summary'] == {'p': '/x/y'}
