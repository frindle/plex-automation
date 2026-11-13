"""Frozen test: paid_cap_reached(env, path, month) for plex-llm-paid-cap step 1."""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="frozen-test-")


def _load_llm_advisor():
    import llm_advisor
    return llm_advisor


def _log(name, rows):
    p = os.path.join(TMP, name)
    with open(p, 'w', encoding='utf-8') as fh:
        for ts, layers in rows:
            fh.write(json.dumps({'ts': ts, 'kind': 'upgrade_gate', 'paid_calls': [{'layer': l} for l in layers]}) + '\n')
    return p


def test_cap_reached_at_count():
    la = _load_llm_advisor()
    p = _log('a.jsonl', [
        ('2026-11-02T01:00:00+00:00', ['paid']),
        ('2026-11-03T01:00:00+00:00', ['paid', 'claude']),
        ('2026-10-30T01:00:00+00:00', ['paid', 'paid', 'claude']),
    ])
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '3'}, p, '2026-11') is True
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '4'}, p, '2026-11') is False
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': ' 2 '}, p, '2026-11') is True
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '4'}, p, '2026-10') is False
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '3'}, p, '2026-10') is True


def test_bad_or_missing_cap_defaults_to_300():
    la = _load_llm_advisor()
    p299 = _log('b299.jsonl', [('2026-11-05T00:00:00+00:00', ['paid'] * 299)])
    p300 = _log('b300.jsonl', [('2026-11-05T00:00:00+00:00', ['paid'] * 150 + ['claude'] * 150)])
    for env in ({}, {'LLM_PAID_MONTHLY_MAX_CALLS': ''}, {'LLM_PAID_MONTHLY_MAX_CALLS': 'abc'},
                {'LLM_PAID_MONTHLY_MAX_CALLS': '-1'}, {'LLM_PAID_MONTHLY_MAX_CALLS': '2.5'},
                {'LLM_PAID_MONTHLY_MAX_CALLS': None}):
        assert la.paid_cap_reached(env, p299, '2026-11') is False, env
        assert la.paid_cap_reached(env, p300, '2026-11') is True, env
    assert la.paid_cap_reached(None, p300, '2026-11') is False


def test_zero_cap_always_reached():
    la = _load_llm_advisor()
    missing = os.path.join(TMP, 'nope.jsonl')
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '0'}, missing, '2026-11') is True
    assert la.paid_cap_reached({'LLM_PAID_MONTHLY_MAX_CALLS': '5'}, missing, '2026-11') is False


TESTS = [test_cap_reached_at_count, test_bad_or_missing_cap_defaults_to_300, test_zero_cap_always_reached]
GUARDS = []


if __name__ == "__main__":
    import asyncio as _asyncio
    import inspect as _inspect
    import os as _os
    import sys as _sys
    _order = TESTS[::-1] if _os.environ.get("TEST_ORDER") == "reverse" else TESTS
    _failed = 0
    for _t in _order:
        try:
            _r = _t()
            if _inspect.iscoroutine(_r):  # an async test
                _asyncio.run(_r)
            print(f"RESULT {_t.__name__} PASS", flush=True)
        except Exception as _e:
            _failed += 1
            print(f"RESULT {_t.__name__} FAIL {type(_e).__name__}: {_e}", flush=True)
    print(f"{len(_order)} tests, {_failed} failed")
    _sys.exit(1 if _failed else 0)
