"""GET /torrent-breakdown: read-only; one get_torrents_status RPC after deluge_login, JSON of breakdown()."""
import pytest

aw = __import__('arr-webhook')


@pytest.fixture
def client(monkeypatch):
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    mp = monkeypatch
    calls = []
    mp.setattr(aw, 'deluge_login', lambda: calls.append(('login', None)))
    box = {'result': {'h1': {'name': 'a', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
                             'seeding_time': 0, 'save_path': '/data/Downloads/Complete'}}}

    def rpc(method, params):
        calls.append((method, params))
        return box['result']

    mp.setattr(aw, '_deluge_rpc', rpc)
    return aw.app.test_client(), calls, box


def test_get_returns_breakdown_json(client):
    c, calls, box = client
    resp = c.get('/torrent-breakdown')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total'] == 1 and body['sonarr']['count'] == 1 and body['unscoped'] == 0, body
    assert body['sonarr']['by_label'] == {'sonarr': 1}, body


def test_login_first_and_only_one_read_rpc(client):
    c, calls, box = client
    c.get('/torrent-breakdown')
    assert calls == [('login', None),
                     ('core.get_torrents_status', [{}, ['name', 'label', 'state', 'progress', 'seeding_time', 'save_path']])], calls


def test_none_result_is_treated_as_empty_and_post_is_refused(client):
    c, calls, box = client
    box['result'] = None
    body = c.get('/torrent-breakdown').get_json()
    assert body['total'] == 0 and body['unscoped'] == 0, body
    assert c.post('/torrent-breakdown').status_code == 405
