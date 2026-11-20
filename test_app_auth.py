"""Request gate (app_auth.py): modes, each credential type, /share identity, action routes, Plex token.
All keys, emails, audiences and tokens here are made up for the test; no network is used."""
import base64
import json
import logging
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import app_auth
import media_share as ms

aw = __import__('arr-webhook')

TEAM = 'team-a'
AUD = 'aud-one'
ADMIN = 'admin-user'
FRIEND = 'friend-a'
OTHER = 'friend-b'
PROTECTED = '/llm-stats'

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def jwk(key, kid):
    n = key.public_key().public_numbers()
    return {'kty': 'RSA', 'kid': kid, 'alg': 'RS256',
            'n': b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, 'big')),
            'e': b64(n.e.to_bytes(3, 'big'))}


def make_jwt(email=FRIEND, key=KEY, kid='k1', aud=AUD, iss=None, exp=None, **extra):
    head = {'alg': 'RS256', 'kid': kid, 'typ': 'JWT'}
    claims = {'aud': [aud], 'iss': iss or f'https://{TEAM}', 'exp': exp or time.time() + 600}
    if email:
        claims['email'] = email
    claims.update(extra)
    signing = f'{b64(json.dumps(head).encode())}.{b64(json.dumps(claims).encode())}'
    sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f'{signing}.{b64(sig)}'


class FakeCerts:
    def __init__(self):
        self.calls = []

    def __call__(self, url, timeout=None, **kw):
        self.calls.append(url)

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {'keys': [jwk(KEY, 'k1')]}
        return R()


@pytest.fixture
def env(monkeypatch, tmp_path):
    certs = FakeCerts()
    monkeypatch.setattr(app_auth.requests, 'get', certs)
    app_auth.reset_key_cache()
    tok = tmp_path / 'tokens'
    tok.write_text('# mover\ntoken-aaaa\ntoken-bbbb\n')
    basic = tmp_path / 'basic'
    basic.write_text('arr:pw-one\n')
    monkeypatch.setenv('CF_ACCESS_TEAM_DOMAIN', f'https://{TEAM}/')
    monkeypatch.setenv('CF_ACCESS_AUDS', f'{AUD},aud-two')
    monkeypatch.setenv('APP_AUTH_TOKEN_FILE', str(tok))
    monkeypatch.setenv('APP_AUTH_BASIC_FILE', str(basic))
    monkeypatch.setenv('APP_AUTH_MODE', 'enforce')
    monkeypatch.setattr(ms, 'ADMIN_EMAIL', ADMIN)
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    yield certs
    app_auth.reset_key_cache()


def client():
    # The test client's peer is loopback by default; real callers are not.
    c = aw.app.test_client()
    c.environ_base['REMOTE_ADDR'] = 'remote-peer'
    return c


def get(path='/llm-stats', **headers):
    return client().get(path, headers=headers)


def basic(user, pw):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{pw}'.encode()).decode()}


# ── modes ────────────────────────────────────────────────────────────────────

def test_enforce_refuses_without_credential(env):
    r = get()
    assert r.status_code == 403 and r.get_json()['ok'] is False


def test_off_allows_everything(env, monkeypatch):
    monkeypatch.setenv('APP_AUTH_MODE', 'off')
    assert get().status_code == 200


def test_log_is_the_default_and_allows_but_logs(env, monkeypatch, caplog):
    monkeypatch.delenv('APP_AUTH_MODE')
    assert app_auth.mode() == 'log'
    with caplog.at_level(logging.WARNING):
        r = get()
    assert r.status_code == 200
    line = [m for m in caplog.messages if 'would refuse' in m]
    assert line and 'class=api' in line[0] and 'no credential presented' in line[0], caplog.messages


def test_log_mode_never_prints_the_secret_it_rejected(env, monkeypatch, caplog):
    monkeypatch.setenv('APP_AUTH_MODE', 'log')
    with caplog.at_level(logging.WARNING):
        get(Authorization='Bearer wrong-token-value')
    text = ' '.join(caplog.messages)
    assert 'bearer rejected' in text and 'wrong-token-value' not in text


def test_unknown_mode_fails_closed(env, monkeypatch):
    monkeypatch.setenv('APP_AUTH_MODE', 'enforced-typo')
    assert app_auth.mode() == 'enforce'
    assert get().status_code == 403


def test_health_is_always_open(env):
    assert get('/health').status_code == 200


# ── credentials ──────────────────────────────────────────────────────────────

def test_valid_jwt_passes(env):
    assert get(**{'Cf-Access-Jwt-Assertion': make_jwt()}).status_code == 200


def test_second_configured_aud_passes(env):
    assert get(**{'Cf-Access-Jwt-Assertion': make_jwt(aud='aud-two')}).status_code == 200


@pytest.mark.parametrize('bad', [
    dict(aud='someone-else'),
    dict(exp=time.time() - 3600),
    dict(iss='https://other-team'),
    dict(key=OTHER_KEY),
    dict(kid='unknown'),
])
def test_bad_jwt_refused(env, bad):
    assert get(**{'Cf-Access-Jwt-Assertion': make_jwt(**bad)}).status_code == 403


def test_tampered_and_garbage_jwt_refused(env):
    good = make_jwt()
    head, body, sig = good.split('.')
    forged = b64(json.dumps({'aud': [AUD], 'iss': f'https://{TEAM}', 'exp': time.time() + 600,
                             'email': ADMIN}).encode())
    assert get(**{'Cf-Access-Jwt-Assertion': f'{head}.{forged}.{sig}'}).status_code == 403
    assert get(**{'Cf-Access-Jwt-Assertion': 'not.a.jwt'}).status_code == 403
    assert get(**{'Cf-Access-Jwt-Assertion': 'x'}).status_code == 403


def test_jwt_without_team_config_refused(env, monkeypatch):
    monkeypatch.delenv('CF_ACCESS_AUDS')
    assert get(**{'Cf-Access-Jwt-Assertion': make_jwt()}).status_code == 403


def test_keys_are_cached(env):
    for _ in range(3):
        assert get(**{'Cf-Access-Jwt-Assertion': make_jwt()}).status_code == 200
    assert len(env.calls) == 1 and env.calls[0] == f'https://{TEAM}/cdn-cgi/access/certs'


def test_bearer_from_file(env):
    assert get(Authorization='Bearer token-bbbb').status_code == 200
    assert get(Authorization='Bearer token-cccc').status_code == 403
    assert get(Authorization='Bearer ').status_code == 403


def test_bearer_with_no_token_file_refused(env, monkeypatch, tmp_path):
    monkeypatch.setenv('APP_AUTH_TOKEN_FILE', str(tmp_path / 'missing'))
    assert get(Authorization='Bearer token-aaaa').status_code == 403
    monkeypatch.delenv('APP_AUTH_TOKEN_FILE')
    assert get(Authorization='Bearer ').status_code == 403


def test_token_file_changes_are_picked_up(env, tmp_path):
    f = tmp_path / 'tokens'
    f.write_text('fresh-token-12345\n')
    assert get(Authorization='Bearer fresh-token-12345').status_code == 200
    assert get(Authorization='Bearer token-aaaa').status_code == 403


def test_basic_works_on_webhook_only(env):
    c = client()
    ok = c.post('/webhook/radarr', json={}, headers=basic('arr', 'pw-one'))
    assert ok.status_code == 200
    assert c.post('/webhook/radarr', json={}, headers=basic('arr', 'nope')).status_code == 403
    assert c.post('/webhook/sonarr', json={}).status_code == 403
    assert get(**basic('arr', 'pw-one')).status_code == 403   # not a webhook route
    assert get('/share/', **basic('arr', 'pw-one')).status_code == 403


def test_bearer_and_jwt_work_on_webhook(env):
    c = client()
    assert c.post('/webhook/radarr', json={}, headers={'Authorization': 'Bearer token-aaaa'}).status_code == 200
    assert c.post('/webhook/radarr', json={}, headers={'Cf-Access-Jwt-Assertion': make_jwt()}).status_code == 200


# ── /share identity ──────────────────────────────────────────────────────────

@pytest.fixture
def share(env, monkeypatch, tmp_path):
    db = tmp_path / 'share.db'
    monkeypatch.setattr(ms, 'DB_PATH', str(db))
    ms.init_db()
    movies = tmp_path / 'movies'
    for d in ('Allowed One', 'Secret Two'):
        (movies / d).mkdir(parents=True)
        (movies / d / 'f.txt').write_text('x')
    monkeypatch.setattr(ms, 'LIBRARIES', {'movies': str(movies), 'tv': str(tmp_path / 'tv'),
                                          'music': str(tmp_path / 'music')})
    con = sqlite3.connect(db)
    for email in (FRIEND, OTHER):
        con.execute("INSERT INTO friends (email, sftp_host, libraries, allowed_titles, created_at) "
                    "VALUES (?, 'sftp-host', '[\"movies\"]', ?, 'now')",
                    (email, json.dumps({'movies': ['Allowed One']})))
    con.execute("INSERT INTO uploads (friend_email, library, rel_path, status, created_at) "
                "VALUES (?, 'movies', 'Allowed One', 'done', 'now')", (FRIEND,))
    con.commit()
    con.close()
    return str(movies)


def as_jwt(email):
    return {'Cf-Access-Jwt-Assertion': make_jwt(email=email)}


def test_share_spoofed_email_header_is_403_in_every_mode(share, monkeypatch):
    spoof = {'Cf-Access-Authenticated-User-Email': ADMIN}
    for m in ('enforce', 'log', 'off'):
        monkeypatch.setenv('APP_AUTH_MODE', m)
        assert get('/share/', **spoof).status_code == 403, m
        assert get('/share/admin', **spoof).status_code == 403, m


def test_share_header_does_not_override_validated_identity(share):
    h = dict(as_jwt(FRIEND), **{'Cf-Access-Authenticated-User-Email': ADMIN})
    assert get('/share/admin', **h).status_code == 403
    assert get('/share/', **h).status_code == 200


def test_share_identity_comes_from_jwt(share):
    assert get('/share/', **as_jwt(FRIEND)).status_code == 200
    assert get('/share/admin', **as_jwt(FRIEND)).status_code == 403
    assert get('/share/admin', **as_jwt(ADMIN)).status_code == 200


def test_share_service_token_jwt_has_no_identity(share):
    assert get('/share/', **{'Cf-Access-Jwt-Assertion': make_jwt(email=None)}).status_code == 403


def test_title_check_resolves_dotdot(share):
    ok = ms._check_title_access
    ms_titles = {FRIEND: {'movies': ['Allowed One']}}
    assert ok(FRIEND, 'movies', 'Allowed One/f.txt') is True
    assert ok(FRIEND, 'movies', 'Secret Two') is False
    assert ok(FRIEND, 'movies', 'Allowed One/../Secret Two') is False
    assert ok(FRIEND, 'movies', 'Allowed One/../Secret Two/f.txt') is False
    assert ok(FRIEND, 'movies', '../movies/Secret Two') is False
    assert ok(FRIEND, 'movies', './Allowed One/./f.txt') is True
    assert ok(FRIEND, 'movies', 'Secret Two/../Allowed One/f.txt') is True
    assert ms_titles


def test_dotdot_title_bypass_blocked_on_download(share):
    h = as_jwt(FRIEND)
    assert get('/share/download/movies?path=Allowed One/f.txt', **h).status_code == 200
    assert get('/share/download/movies?path=Allowed One/../Secret Two/f.txt', **h).status_code == 404
    assert get('/share/download/movies?path=Secret Two/f.txt', **h).status_code == 404


def test_status_has_owner_check(share):
    assert get('/share/status/1', **as_jwt(FRIEND)).status_code == 200
    assert get('/share/status/1', **as_jwt(OTHER)).status_code == 404
    assert get('/share/status/1', **as_jwt(ADMIN)).status_code == 200


def test_friend_test_connection_needs_validated_identity(share, monkeypatch):
    calls = []
    monkeypatch.setattr(ms, '_test_connection', lambda *a: calls.append(a) or (True, ''))
    c = client()
    form = {'protocol': 'sftp', 'sftp_host': 'sftp-host', 'sftp_user': 'u', 'sftp_password': 'p'}
    r = c.post('/share/test-connection', data=form,
               headers={'Cf-Access-Authenticated-User-Email': FRIEND})
    assert r.status_code == 403 and calls == []
    r = c.post('/share/test-connection', data=form, headers=as_jwt(FRIEND))
    assert r.status_code == 200 and r.get_json()['ok'] is True and len(calls) == 1


# ── action routes ────────────────────────────────────────────────────────────

MUTATING_GET = [
    '/plex-dupe-fix?apply=1', '/orphan-scan?delete=1', '/retire-seeds?apply=1', '/upgrade-lane-cap?apply=true',
    '/relabel-by-plex?apply=yes', '/unlabel-library-seeds?apply=1', '/fix-stuck-upgrade-tags?apply=1',
    '/verify-labels', '/purge-non-radarr?apply=1', '/delete-paths?apply=1', '/torrent-move',
]


@pytest.mark.parametrize('path', MUTATING_GET)
def test_converted_route_rejects_get(env, monkeypatch, path):
    monkeypatch.setenv('APP_AUTH_MODE', 'off')
    r = get(path)
    assert r.status_code == 405 and r.headers['Allow'] == 'POST', (path, r.status_code)


@pytest.mark.parametrize('path', MUTATING_GET)
def test_converted_route_rejects_non_json_post(env, monkeypatch, path):
    monkeypatch.setenv('APP_AUTH_MODE', 'off')
    c = client()
    for ctype in ('text/plain', 'application/x-www-form-urlencoded'):
        r = c.post(path, data='{"moves": []}', content_type=ctype)
        assert r.status_code == 415, (path, ctype, r.status_code)
    assert c.post(path, data='x').status_code == 415


@pytest.mark.parametrize('path', MUTATING_GET)
def test_converted_route_accepts_json_post_at_the_gate(env, path):
    with aw.app.test_request_context(path, method='POST', json={}):
        assert app_auth._action_refusal() is None


@pytest.mark.parametrize('path', [p.split('?')[0] for p in MUTATING_GET
                                  if p.split('?')[0] not in ('/verify-labels', '/purge-non-radarr',
                                                             '/delete-paths', '/torrent-move')])
def test_dry_run_reports_stay_on_get(env, path):
    with aw.app.test_request_context(path, method='GET'):
        assert app_auth._action_refusal() is None


def test_action_rules_apply_with_valid_credentials_too(env):
    r = client().get('/verify-labels', headers={'Authorization': 'Bearer token-aaaa'})
    assert r.status_code == 405


def test_unauthenticated_post_is_refused_before_the_action_rule(env):
    r = client().post('/torrent-move', json={'moves': []})
    assert r.status_code == 403


def test_torrent_move_and_delete_paths_ignore_text_plain_bodies(env, monkeypatch):
    monkeypatch.setenv('APP_AUTH_MODE', 'off')
    c = client()
    h = {'Content-Type': 'application/json'}
    assert c.post('/delete-paths', data='not json', headers=h).status_code == 400
    assert c.post('/delete-paths', json={'paths': 'a-string'}).status_code == 400
    assert c.post('/torrent-move', data='{"moves": [1]', headers=h).status_code == 400


# ── Plex token ───────────────────────────────────────────────────────────────

def test_plex_token_goes_in_a_header_not_the_url(monkeypatch):
    monkeypatch.setattr(aw, 'PLEX_TOKEN', 'tok-secret-1234')
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {'ok': 1}
        return R()
    monkeypatch.setattr(aw.requests, 'get', fake_get)
    assert aw._plex_get('/library/sections', {'includeGuids': 1}) == {'ok': 1}
    assert seen['headers']['X-Plex-Token'] == 'tok-secret-1234'
    assert 'X-Plex-Token' not in (seen['params'] or {}) and 'tok-secret-1234' not in seen['url']


def test_plex_error_never_echoes_the_token(monkeypatch):
    monkeypatch.setattr(aw, 'PLEX_TOKEN', 'tok-secret-1234')
    monkeypatch.setenv('APP_AUTH_MODE', 'off')

    def boom(url, **kw):
        raise aw.requests.exceptions.ConnectionError(f'failed {url}?X-Plex-Token=tok-secret-1234')
    monkeypatch.setattr(aw.requests, 'get', boom)
    r = client().get('/plex-dupe-scan')
    assert r.status_code == 500
    assert 'tok-secret-1234' not in r.get_data(as_text=True)


# ── loopback reports ─────────────────────────────────────────────────────────

def from_addr(path, addr, method='GET', **headers):
    return client().open(path, method=method, headers=headers,
                         environ_overrides={'REMOTE_ADDR': addr})


def test_loopback_get_report_passes_in_enforce(env):
    assert from_addr(PROTECTED, '127.0.0.1').status_code == 200
    assert from_addr(PROTECTED, '::1').status_code == 200


def test_non_loopback_get_report_still_refused(env):
    assert from_addr(PROTECTED, 'remote-peer').status_code == 403


def test_forwarded_for_cannot_fake_loopback(env):
    r = from_addr(PROTECTED, 'remote-peer', **{'X-Forwarded-For': '127.0.0.1', 'X-Real-Ip': '127.0.0.1'})
    assert r.status_code == 403


def test_loopback_never_opens_action_routes_posts_or_share(env):
    assert from_addr('/retire-seeds?apply=1', '127.0.0.1').status_code == 403
    assert from_addr('/verify-labels', '127.0.0.1').status_code == 403
    assert from_addr(PROTECTED, '127.0.0.1', method='POST').status_code == 403
    assert from_addr('/webhook/radarr', '127.0.0.1', method='POST').status_code == 403
    assert from_addr('/share/', '127.0.0.1').status_code == 403
