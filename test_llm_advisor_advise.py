"""Frozen test: advise() honours the monthly paid-call cap (plex-llm-paid-cap step 2). Shadow never pays (11-13)."""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="frozen-test-")
GOOD = '{"action": "reject", "confidence": 0.9, "reason": "r"}'


def _load_llm_advisor():
    import llm_advisor
    return llm_advisor


def _run(cap, logged, mode='shadow'):
    la = _load_llm_advisor()
    d = tempfile.mkdtemp(dir=TMP)
    log = os.path.join(d, 'log.jsonl')
    month = time.strftime('%Y-%m', time.gmtime())
    with open(log, 'w', encoding='utf-8') as fh:
        if logged:
            fh.write(json.dumps({'ts': month + '-01T00:00:00+00:00', 'kind': 'upgrade_gate',
                                 'paid_calls': [{'layer': 'paid'}] * logged}) + '\n')
    calls = []

    def local(prompt):
        calls.append('local')
        raise ConnectionError('down')

    def paid(prompt):
        calls.append('paid')
        return GOOD

    def claude(prompt):
        calls.append('claude')
        return GOOD

    layers = {'local': la.Layer('local', local, 'm-local'), 'paid': la.Layer('paid', paid, 'm-paid'),
              'claude': la.Layer('claude', claude, 'm-claude')}
    env = {'LLM_PAID_MONTHLY_MAX_CALLS': str(cap), 'LLM_LOCAL_WAIT_MIN': '0',
           'LLM_PENDING_PATH': os.path.join(d, 'pending.json')}
    out = la.advise('upgrade_gate', {'title': 'T', 'size_gb': 12}, 'allow', mode=mode, log_path=log, env=env,
                    layers=layers, now=time.time)
    with open(log, encoding='utf-8') as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    return out, calls, rows, layers


def test_capped_skips_paid_and_claude():
    out, calls, rows, _ = _run(cap=2, logged=2, mode='enforce')
    assert out == 'allow', out
    assert calls == ['local'], calls
    assert len(rows) == 2, rows
    assert rows[-1].get('paid_cap') is True, rows[-1]


def test_zero_cap_enforce_rules_only():
    out, calls, rows, layers = _run(cap=0, logged=0, mode='enforce')
    assert out == 'allow', out
    assert calls == ['local'], calls
    assert rows[-1].get('paid_cap') is True, rows[-1]
    assert sorted(layers) == ['claude', 'local', 'paid'], sorted(layers)


def test_under_cap_still_calls_paid():
    out, calls, rows, _ = _run(cap=5, logged=2, mode='enforce')
    assert out == 'reject', out
    assert calls == ['local', 'paid'], calls
    assert 'paid_cap' not in rows[-1], rows[-1]


def test_shadow_never_calls_paid_even_under_cap():
    out, calls, rows, _ = _run(cap=5, logged=2)
    assert out == 'allow', out
    assert calls == ['local'], calls
    assert 'paid_cap' not in rows[-1], rows[-1]


TESTS = [test_capped_skips_paid_and_claude, test_zero_cap_enforce_rules_only, test_under_cap_still_calls_paid,
         test_shadow_never_calls_paid_even_under_cap]
GUARDS = [test_under_cap_still_calls_paid]


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
