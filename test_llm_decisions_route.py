"""GET /llm-decisions: last N records of LLM_DECISIONS_LOG, optional disagree filter, never raises."""
import json

import pytest

aw = __import__('arr-webhook')


def _rec(i, agree):
    return {'ts': f'2026-10-29T00:00:{i:02d}+00:00', 'kind': 'upgrade_gate', 'rules_action': 'throttle',
            'llm_action': 'throttle' if agree else 'reject', 'agree': agree, 'n': i}


@pytest.fixture
def client(monkeypatch, tmp_path):
    mp = monkeypatch
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    path = tmp_path / 'llm_decisions.jsonl'
    mp.setattr(aw, 'LLM_DECISIONS_LOG', str(path))
    return aw.app.test_client(), path


def _write(path, lines):
    path.write_text(''.join(line + '\n' for line in lines), encoding='utf-8')


def test_missing_file_returns_empty_list(client):
    c, path = client
    assert not path.exists()
    resp = c.get('/llm-decisions')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['entries'] == []
    assert body['count'] == 0


def test_limit_returns_last_n_in_file_order(client):
    c, path = client
    _write(path, [json.dumps(_rec(i, True)) for i in range(6)])
    body = c.get('/llm-decisions?limit=2').get_json()
    assert [r['n'] for r in body['entries']] == [4, 5]
    assert body['count'] == 2
    default = c.get('/llm-decisions').get_json()
    assert [r['n'] for r in default['entries']] == [0, 1, 2, 3, 4, 5]


def test_disagree_keeps_only_agree_false(client):
    c, path = client
    recs = [_rec(0, True), _rec(1, False), _rec(2, True), _rec(3, False)]
    no_agree = {'kind': 'dupe_keeper', 'n': 4}
    _write(path, [json.dumps(r) for r in recs] + [json.dumps(no_agree)])
    body = c.get('/llm-decisions?disagree=1').get_json()
    assert [r['n'] for r in body['entries']] == [1, 3]
    assert all(r['agree'] is False for r in body['entries'])
    limited = c.get('/llm-decisions?disagree=1&limit=1').get_json()
    assert [r['n'] for r in limited['entries']] == [3]


def test_bad_lines_and_bad_limit_never_raise(client):
    c, path = client
    _write(path, ['not json {', json.dumps(_rec(1, False)), '[1, 2]', '', json.dumps(_rec(2, True))])
    resp = c.get('/llm-decisions?limit=abc')
    assert resp.status_code == 200
    assert [r['n'] for r in resp.get_json()['entries']] == [1, 2]
    resp2 = c.get('/llm-decisions?disagree=1&limit=-5')
    assert resp2.status_code == 200
    assert isinstance(resp2.get_json()['entries'], list)
