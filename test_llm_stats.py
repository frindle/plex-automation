"""llm_stats / llm_stats_lines / GET /llm-stats: synthetic rows only."""
import datetime
import json

import pytest

import llm_advisor as la

aw = __import__('arr-webhook')

NOW = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
SECRET = 'SECRET-TITLE-XYZ'
_n = [0]


def _ts(hours_ago=1.0):
    return datetime.datetime.fromtimestamp(NOW - hours_ago * 3600, datetime.timezone.utc).isoformat()


def row(kind='import_identity', answered=True, layer='unraid', ctx=None, hours_ago=1.0, **kw):
    _n[0] += 1
    r = {'ts': _ts(hours_ago), 'kind': kind, 'context_summary': ctx or '%s-%d' % (SECRET, _n[0]),
         'rules_action': 'match', 'llm_action': 'match' if answered else None, 'agree': answered,
         'reason': 'ok' if answered else None, 'mode': 'shadow', 'layer': layer if (answered or layer == 'pending') else None,
         'latency_ms': 100}
    r.update(kw)
    return r


def unans(unraid=None, **kw):
    kw.setdefault('latency_ms', None)
    return row(answered=False, unraid=unraid, **kw)


def stats(rows, kind='import_identity', **kw):
    return la.llm_stats(rows, now=NOW, **kw)[kind]


def test_answered_and_layers_including_cache():
    s = stats([row(), row(), row(layer='cache'), unans({'pass': 'timeout', 'latency_ms': None})])
    assert (s['asked'], s['answered'], s['answered_pct']) == (4, 3, 75.0)
    assert s['by_layer'] == {'unraid': 2, 'cache': 1}
    assert s['median_latency_ms'] == 100


@pytest.mark.parametrize('kw,cause', [
    ({'unraid': {'pass': 'timeout', 'latency_ms': 240000}}, 'cold_load_timeout'),
    ({'unraid': {'pass': 'timeout', 'latency_ms': 30000 - 1}}, 'warm_timeout'),
    ({'unraid': {'pass': 'timeout', 'latency_ms': None}}, 'layer_down'),
    ({'unraid': {'pass': 'other-model-resident'}}, 'other_model_loaded'),
    ({'unraid': {'pass': 'hold', 'hold_cause': 'plex'}}, 'plex_holding_gpu'),
    ({'unraid': {'pass': 'hold'}}, 'other'),
    ({'unraid': {'pass': 'backoff'}}, 'backoff'),
    ({'unraid': {'pass': 'error'}}, 'layer_down'),
    ({'unraid': {'pass': 'unparseable'}}, 'unparseable'),
    ({'unraid': {'pass': 'unsure'}}, 'other'),
    ({'unraid': {'pass': 'backoff'}, 'error': 'unraid backoff'}, 'backoff'),
    ({'error': 'no free layer configured'}, 'no_layer'),
    ({'error': 'hold', 'hold_cause': 'plex', 'layer': 'pending'}, 'plex_holding_gpu'),
    ({'error': 'hold'}, 'other'),
    ({'error': 'model-not-approved', 'local_skip': 'model-not-approved'}, 'other_model_loaded'),
    ({'error': 'model-unknown', 'local_skip': 'model-unknown'}, 'layer_down'),
    ({'error': 'unparseable or invalid decision'}, 'unparseable'),
    ({'error': 'ReadTimeout: slow'}, 'warm_timeout'),
    ({'error': 'ConnectionError: refused'}, 'layer_down'),
    ({'error': 'something odd'}, 'other'),
])
def test_cause_mapping(kw, cause):
    kw = dict(kw)
    un = kw.pop('unraid', None)
    assert stats([unans(un, **kw)])['causes'] == {cause: 1}


def test_veto_and_agree_math():
    rows = [row(), row(), row(),
            row(agree=False, llm_action='mismatch'),
            row(reason='[hard rule spin-off] x', agree=False),
            row(agree=None)]
    s = stats(rows)
    assert s['hard_rule_veto'] == 1
    assert (s['agree'], s['disagree'], s['agree_pct']) == (3, 2, 60.0)


def test_late_answer_counted_once():
    ctx = SECRET + '-gate'
    rows = [row('upgrade_gate', answered=False, layer='pending', ctx=ctx, hours_ago=2, rules_action='allow'),
            row('upgrade_gate', ctx=ctx, hours_ago=1, rules_action='allow'),
            row('upgrade_gate', answered=False, layer='pending', ctx=SECRET + '-other', rules_action='allow')]
    s = stats(rows, 'upgrade_gate')
    assert (s['asked'], s['answered']) == (2, 1)
    assert s['causes'] == {'other': 1}


def test_unanswered_rows_without_pending_key_count_each():
    s = stats([unans({'pass': 'backoff'}), unans({'pass': 'backoff'})])
    assert (s['asked'], s['causes']) == (2, {'backoff': 2})


def test_days_window():
    rows = [row(hours_ago=1), row(hours_ago=24 * 3), row(hours_ago=24 * 10)]
    assert stats(rows, days=7)['asked'] == 2
    assert stats(rows, days=1)['asked'] == 1
    assert stats(rows, days=30)['asked'] == 3


def test_below_target_thresholds():
    ok = [row() for _ in range(20)]
    assert stats(ok)['below_target'] == []
    assert stats([])['below_target'] == []
    # 18/20 answered = 90%: at the line, passes; the cause is 10% of asked, over 5%
    s = stats(ok[:18] + [unans({'pass': 'backoff'}), unans({'pass': 'backoff'})])
    assert s['answered_pct'] == 90.0
    assert any(t.startswith('backoff') for t in s['below_target'])
    assert not any(t.startswith('answered') for t in s['below_target'])
    # one cause at exactly 5% (1 of 20) is on target, 17/20 answered is under 90
    s = stats(ok[:19] + [unans({'pass': 'backoff'})])
    assert s['below_target'] == []
    s = stats(ok[:17] + [unans({'pass': 'backoff'}), unans({'pass': 'error'}), unans({'pass': 'hold'})])
    assert s['below_target'][0].startswith('answered 85%')
    # agree under 95
    s = stats([row() for _ in range(18)] + [row(agree=False, llm_action='mismatch')] * 2)
    assert s['agree_pct'] == 90.0 and any(t.startswith('agree') for t in s['below_target'])
    # any veto
    s = stats(ok + [row(reason='[hard rule r] x')])
    assert any('veto' in t for t in s['below_target'])


def test_lines_and_no_titles():
    rows = [row() for _ in range(17)] + [unans({'pass': 'timeout', 'latency_ms': 240000})]
    st = la.llm_stats(rows, now=NOW)
    lines = la.llm_stats_lines(st)
    assert lines == ['import_identity: 17/18 answered (94%), top cause cold_load_timeout 1, agree 100%']
    assert SECRET not in json.dumps(st) + ''.join(lines)


def test_read_decision_rows(tmp_path):
    p = tmp_path / 'l.jsonl'
    p.write_text(json.dumps(row()) + '\nnot json\n[1]\n\n', encoding='utf-8')
    assert len(la.read_decision_rows(str(p))) == 1
    assert la.read_decision_rows(str(tmp_path / 'missing')) == []


@pytest.fixture
def client(monkeypatch, tmp_path):
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    path = tmp_path / 'llm_decisions.jsonl'
    monkeypatch.setattr(aw, 'LLM_DECISIONS_LOG', str(path))
    return aw.app.test_client(), path


def test_route_no_titles_and_clamp(client):
    import time
    c, path = client
    fresh = row()
    fresh['ts'] = datetime.datetime.fromtimestamp(time.time() - 60, datetime.timezone.utc).isoformat()
    path.write_text(json.dumps(fresh) + '\n', encoding='utf-8')
    body = c.get('/llm-stats').get_json()
    assert body['ok'] is True and body['days'] == 7
    assert body['kinds']['import_identity']['asked'] == 1
    assert body['lines'] and SECRET not in json.dumps(body)
    assert c.get('/llm-stats?days=0').get_json()['days'] == 1
    assert c.get('/llm-stats?days=999').get_json()['days'] == 90
    assert c.get('/llm-stats?days=abc').get_json()['days'] == 7


def test_route_missing_log_and_failure(client, monkeypatch):
    c, path = client
    assert c.get('/llm-stats').get_json()['lines'] == []

    def boom(*a, **k):
        raise RuntimeError('x')
    monkeypatch.setattr(aw.llm_advisor, 'llm_stats', boom)
    body = c.get('/llm-stats').get_json()
    assert body == {'ok': False, 'error': 'RuntimeError'}


def test_digest_failure_only(client, monkeypatch):
    c, path = client
    assert aw._llm_below_target_lines() == []
    import time
    ts = datetime.datetime.fromtimestamp(time.time() - 60, datetime.timezone.utc).isoformat()
    good = dict(row(), ts=ts)
    path.write_text(json.dumps(good) + '\n', encoding='utf-8')
    assert aw._llm_below_target_lines() == []
    bad = dict(unans({'pass': 'backoff'}), ts=ts)
    path.write_text(json.dumps(bad) + '\n', encoding='utf-8')
    out = aw._llm_below_target_lines()
    assert len(out) == 1 and out[0].startswith('import_identity: 0/1') and SECRET not in out[0]
