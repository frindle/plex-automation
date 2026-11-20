"""One request gate for the whole app (arr-webhook routes and the /share blueprint).

APP_AUTH_MODE
  off      no credential check (the action rules below still apply)
  log      default: every request is allowed, but one that would be refused is logged with its
           route class and which credential was missing (never a secret)
  enforce  a request without a valid credential gets 403

A request passes when it carries any of:
  * a Cloudflare Access JWT (Cf-Access-Jwt-Assertion) signed by the team's keys, with the
    configured issuer and one of the configured AUDs
  * Authorization: Bearer <token>, the token being a line of APP_AUTH_TOKEN_FILE (or the line is
    `sha256:<hex>` of the token, so the caller's token can stay in its own Keychain)
  * HTTP Basic, on /webhook/* only, user:password being a line of APP_AUTH_BASIC_FILE
/health is always open. /share/dl/<token> is open at the gate: the link token is its credential
and the route checks it.

The /share pages take the user's identity from the validated JWT only (jwt_email), never from the
Cf-Access-Authenticated-User-Email header, which anyone who reaches the port can set.

Action routes (ACTION_ROUTES) change things; a request that would change something must be a POST
with a JSON content type. A GET is refused (405) and so is a body the browser could send across
sites without a preflight (415). Dry-run reports stay on GET.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import threading
import time

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import g, jsonify, request

log = logging.getLogger(__name__)

MODES = ('off', 'log', 'enforce')
_LEEWAY = 60            # seconds of clock skew allowed on exp / nbf
_KEYS_TTL = 3600        # seconds a fetched key set is trusted
_KEYS_MIN_REFETCH = 60  # an unknown kid never refetches more often than this


def mode():
    raw = (os.environ.get('APP_AUTH_MODE') or 'log').strip().lower()
    if raw in MODES:
        return raw
    # A typo must never leave the app open.
    log.warning('app-auth: unknown APP_AUTH_MODE %r, treating as enforce', raw)
    return 'enforce'


# ── Cloudflare Access JWT ─────────────────────────────────────────────────────

def _team_domain():
    d = (os.environ.get('CF_ACCESS_TEAM_DOMAIN') or '').strip()
    for prefix in ('https://', 'http://'):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip('/')


def _auds():
    return {a.strip() for a in (os.environ.get('CF_ACCESS_AUDS') or '').split(',') if a.strip()}


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


_keys_lock = threading.Lock()
_keys = {'by_kid': {}, 'at': 0.0, 'domain': ''}


def _fetch_keys(domain):
    r = requests.get(f'https://{domain}/cdn-cgi/access/certs', timeout=10)
    r.raise_for_status()
    out = {}
    for k in (r.json().get('keys') or []):
        if k.get('kty') != 'RSA' or not k.get('kid'):
            continue
        try:
            n = int.from_bytes(_b64d(k['n']), 'big')
            e = int.from_bytes(_b64d(k['e']), 'big')
            out[k['kid']] = rsa.RSAPublicNumbers(e, n).public_key()
        except Exception:
            continue
    return out


def _key_for(kid, domain):
    now = time.time()
    with _keys_lock:
        fresh = _keys['domain'] == domain and now - _keys['at'] < _KEYS_TTL
        if fresh and kid in _keys['by_kid']:
            return _keys['by_kid'][kid]
        if _keys['domain'] == domain and now - _keys['at'] < _KEYS_MIN_REFETCH:
            return _keys['by_kid'].get(kid)
        try:
            by_kid = _fetch_keys(domain)
        except Exception as exc:
            log.warning('app-auth: key fetch failed (%s)', type(exc).__name__)
            # Keep serving the last good set for a known kid; an unknown kid stays unknown.
            return _keys['by_kid'].get(kid) if _keys['domain'] == domain else None
        _keys.update(by_kid=by_kid, at=now, domain=domain)
        return by_kid.get(kid)


def reset_key_cache():
    with _keys_lock:
        _keys.update(by_kid={}, at=0.0, domain='')


def verify_jwt(token):
    """Return (claims, '') for a valid Access token, else (None, reason)."""
    domain, auds = _team_domain(), _auds()
    if not domain or not auds:
        return None, 'jwt not configured'
    try:
        head_b64, body_b64, sig_b64 = token.split('.')
        header = json.loads(_b64d(head_b64))
        claims = json.loads(_b64d(body_b64))
        sig = _b64d(sig_b64)
    except Exception:
        return None, 'jwt malformed'
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None, 'jwt malformed'
    if header.get('alg') != 'RS256':
        return None, 'jwt alg'
    key = _key_for(header.get('kid') or '', domain)
    if key is None:
        return None, 'jwt unknown key'
    try:
        key.verify(sig, f'{head_b64}.{body_b64}'.encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return None, 'jwt bad signature'
    now = time.time()
    try:
        if float(claims['exp']) < now - _LEEWAY:
            return None, 'jwt expired'
        if 'nbf' in claims and float(claims['nbf']) > now + _LEEWAY:
            return None, 'jwt not yet valid'
    except (KeyError, TypeError, ValueError):
        return None, 'jwt exp missing'
    if claims.get('iss') != f'https://{domain}':
        return None, 'jwt issuer'
    aud = claims.get('aud')
    aud = {aud} if isinstance(aud, str) else set(aud or [])
    if not aud & auds:
        return None, 'jwt audience'
    return claims, ''


def _jwt_state():
    """(claims, reason) for this request's Access JWT, computed once per request."""
    cached = getattr(g, '_app_auth_jwt', None)
    if cached is not None:
        return cached
    token = request.headers.get('Cf-Access-Jwt-Assertion', '')
    state = verify_jwt(token) if token else (None, 'no jwt')
    g._app_auth_jwt = state
    return state


def jwt_email():
    """The validated Access identity's email, or ''. A service-token JWT has none."""
    claims, _ = _jwt_state()
    email = (claims or {}).get('email')
    return email.strip() if isinstance(email, str) else ''


# ── Bearer and Basic from files ───────────────────────────────────────────────

_file_cache = {}


def _file_lines(path):
    """Non-comment lines of a file, re-read when it changes. Missing file -> no lines."""
    if not path:
        return []
    try:
        st = os.stat(path)
    except OSError:
        return []
    hit = _file_cache.get(path)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    try:
        with open(path, 'r') as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith('#')]
    except OSError:
        return []
    _file_cache[path] = ((st.st_mtime_ns, st.st_size), lines)
    return lines


def _any_equal(candidate, secrets):
    ok = False
    for s in secrets:
        # No early exit: the time taken does not say which line matched.
        ok |= hmac.compare_digest(candidate.encode(), s.encode())
    return ok


_HASH_PREFIX = 'sha256:'


def _token_matches(candidate, line):
    """A token-file line is either the plain token or `sha256:<hex>` of it (only the hash need be stored
    where the caller's token lives elsewhere, e.g. a Keychain). Both compare in constant time."""
    if line[:len(_HASH_PREFIX)].lower() == _HASH_PREFIX:
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        return hmac.compare_digest(digest.encode(), line[len(_HASH_PREFIX):].strip().lower().encode())
    return hmac.compare_digest(candidate.encode(), line.encode())


def _bearer_ok():
    h = request.headers.get('Authorization', '')
    if not h[:7].lower() == 'bearer ':
        return None
    tokens = _file_lines(os.environ.get('APP_AUTH_TOKEN_FILE', ''))
    cand = h[7:].strip()
    if not cand:
        return False
    ok = False
    for line in tokens:
        ok |= _token_matches(cand, line)   # no early exit, as in _any_equal
    return ok


def _basic_ok():
    h = request.headers.get('Authorization', '')
    if not h[:6].lower() == 'basic ':
        return None
    try:
        cand = base64.b64decode(h[6:].strip(), validate=True).decode()
    except Exception:
        return False
    creds = _file_lines(os.environ.get('APP_AUTH_BASIC_FILE', ''))
    return ':' in cand and _any_equal(cand, creds)


# ── Gate ──────────────────────────────────────────────────────────────────────

def route_class(path):
    if path == '/health':
        return 'health'
    if path.startswith('/share/dl/'):
        return 'share-link'
    if path.startswith('/webhook/'):
        return 'webhook'
    if path == '/share' or path.startswith('/share/'):
        return 'share'
    return 'api'


_LOOPBACK = ('127.0.0.1', '::1', '::ffff:127.0.0.1')


def caller_class():
    """Where the socket peer is, never its address: loopback, lan (private range), public or unknown.
    For the log-mode watch, so an unknown caller can be placed without writing an IP into the log."""
    addr = request.remote_addr or ''
    if addr in _LOOPBACK:
        return 'loopback'
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return 'unknown'
    return 'lan' if ip.is_private else 'public'


def _loopback_report():
    """A GET report fetched from inside the container (the nightly state job) needs no credential.
    Only the socket peer counts: remote_addr is never taken from X-Forwarded-For, and a proxied
    request arrives from the proxy's address, not loopback. Action routes and /share never qualify."""
    return (request.method == 'GET' and request.remote_addr in _LOOPBACK
            and request.path not in ACTION_ROUTES and route_class(request.path) == 'api')


def check_credentials():
    """(ok, credential kind or '', what was missing)."""
    cls = route_class(request.path)
    if cls in ('health', 'share-link'):
        return True, 'open', ''
    if _loopback_report():
        return True, 'loopback', ''
    claims, jwt_reason = _jwt_state()
    if claims is not None:
        return True, 'jwt', ''
    bearer = _bearer_ok()
    if bearer:
        return True, 'bearer', ''
    basic = None
    if cls == 'webhook':
        basic = _basic_ok()
        if basic:
            return True, 'basic', ''
    tried = []
    if jwt_reason != 'no jwt':
        tried.append(jwt_reason)
    if bearer is False:
        tried.append('bearer rejected')
    if basic is False:
        tried.append('basic rejected')
    if not tried:
        wanted = 'jwt, bearer or basic' if cls == 'webhook' else 'jwt or bearer'
        tried.append(f'no credential presented (need {wanted})')
    return False, '', '; '.join(tried)


_TRUTHY = ('1', 'true', 'yes')


def _flag(name):
    return lambda: request.args.get(name, '').lower() in _TRUTHY


ACTION_ROUTES = {
    '/plex-dupe-fix': _flag('apply'),
    '/orphan-scan': _flag('delete'),
    '/retire-seeds': _flag('apply'),
    '/upgrade-lane-cap': _flag('apply'),
    '/relabel-by-plex': _flag('apply'),
    '/unlabel-library-seeds': _flag('apply'),
    '/fix-stuck-upgrade-tags': _flag('apply'),
    '/verify-labels': lambda: True,
    '/purge-non-radarr': lambda: True,
    '/delete-paths': lambda: True,
    '/torrent-move': lambda: True,
}


def _action_refusal():
    pred = ACTION_ROUTES.get(request.path)
    if pred is None or not pred():
        return None
    if request.method != 'POST':
        resp = jsonify({'ok': False, 'error': 'mutation requires POST'})
        resp.status_code = 405
        resp.headers['Allow'] = 'POST'
        return resp
    if not request.is_json:
        return jsonify({'ok': False, 'error': 'mutation requires Content-Type: application/json'}), 415
    return None


def gate():
    m = mode()
    if m != 'off':
        ok, _kind, missing = check_credentials()
        if not ok:
            if m == 'enforce':
                log.warning('app-auth: refused class=%s method=%s path=%s caller=%s: %s',
                            route_class(request.path), request.method, request.path, caller_class(), missing)
                return jsonify({'ok': False, 'error': 'authentication required'}), 403
            log.warning('app-auth: would refuse (mode=log) class=%s method=%s path=%s caller=%s: %s',
                        route_class(request.path), request.method, request.path, caller_class(), missing)
    return _action_refusal()


def init_app(app):
    """Register the gate first, so it runs before any other before_request."""
    app.before_request_funcs.setdefault(None, []).insert(0, gate)
