import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    import llm_decide
    return llm_decide


def test_unknown_kind_raises_value_error():
    m = _load()
    for kind in ('bogus', None, ''):
        try:
            m.build_prompt(kind, {})
        except ValueError:
            continue
        raise AssertionError('no ValueError for %r' % (kind,))


def test_prompt_names_kind_actions_and_answer_format():
    m = _load()
    p = m.build_prompt('upgrade_gate', {'title': 'Movie'})
    assert isinstance(p, str)
    assert 'upgrade_gate' in p
    for a in ('allow', 'throttle', 'reject'):
        assert a in p
    assert 'confidence' in p and 'reason' in p and 'JSON' in p
    assert 'keep_first' not in p


def test_context_is_embedded_as_sorted_key_json():
    m = _load()
    p = m.build_prompt('dupe_keeper', {'b': 2, 'a': 1})
    assert '{"a": 1, "b": 2}' in p
    assert m.build_prompt('dupe_keeper', {'b': 2, 'a': 1}) == m.build_prompt('dupe_keeper', {'a': 1, 'b': 2})


def test_non_serialisable_context_values_become_str():
    m = _load()

    class Odd:
        def __str__(self):
            return 'ODD-VALUE'

    p = m.build_prompt('import_identity', {'x': Odd(), 'p': Path('/tmp/a')})
    assert 'ODD-VALUE' in p
    assert '/tmp/a' in p
