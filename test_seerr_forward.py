"""Tests for forwarding Seerr webhooks onward (SEERR_FORWARD_URL). Synthetic data, fake HTTP; no network.
Run: python -m pytest test_seerr_forward.py"""
import json
import threading

from test_seerr_sync import _load

BODY = json.dumps(dict(notification_type='MEDIA_APPROVED', media=dict(media_type='movie'))).encode()
TEST_BODY = json.dumps(dict(notification_type='TEST_NOTIFICATION')).encode()
JSON_CT = 'application/json'
LOOP = 'X-Arr-Webhook-Forwarded'


class InlineThread:
    """Runs the target at start() so the forward happens inside the request; the sync job is only recorded."""
    ran = []

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        InlineThread.ran.append(self.target.__name__)
        if self.target.__name__ != '_seerr_sync_job':
            self.target(*self.args)


class FwdResp:
    def __init__(self, code):
        self.status_code = code


class FwdRequests:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(dict(url=url, data=data, headers=dict(headers or {}), timeout=timeout))
        o = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(o, Exception):
            raise o
        return FwdResp(o)


def _setup(url='http://ha.invalid/hook', auth='Bearer synthetic', outcomes=()):
    aw = _load()
    InlineThread.ran = []
    aw.threading.Thread = InlineThread
    aw.SEERR_FORWARD_URL = url
    aw.SEERR_FORWARD_AUTH = auth
    fr = FwdRequests(outcomes)
    aw.requests = fr
    return aw, fr


def _post(aw, body=BODY, ct=JSON_CT, headers=None):
    try:
        return aw.app.test_client().post('/webhook/seerr', data=body, content_type=ct, headers=headers or {})
    finally:
        aw.threading.Thread = threading.Thread


def test_unset_makes_no_call():
    aw, fr = _setup(url='')
    r = _post(aw)
    assert r.status_code == 200 and fr.calls == []
    assert '_seerr_forward_job' not in InlineThread.ran


def test_forwards_body_type_auth_and_loop_header():
    aw, fr = _setup()
    r = _post(aw)
    assert r.status_code == 200 and r.get_json() == dict(status='ok')
    assert len(fr.calls) == 1
    c = fr.calls[0]
    assert c['url'] == 'http://ha.invalid/hook' and c['data'] == BODY
    assert c['headers']['Content-Type'] == JSON_CT
    assert c['headers']['Authorization'] == 'Bearer synthetic'
    assert c['headers'][LOOP] == '1'
    assert c['timeout'] == (5, 10)


def test_incoming_auth_is_not_passed_through():
    aw, fr = _setup(auth='')
    _post(aw, headers=dict(Authorization='Bearer incoming-secret'))
    assert len(fr.calls) == 1 and 'Authorization' not in fr.calls[0]['headers']


def test_incoming_auth_never_replaces_configured_auth():
    aw, fr = _setup(auth='Bearer configured')
    _post(aw, headers=dict(Authorization='Bearer incoming-secret'))
    assert fr.calls[0]['headers']['Authorization'] == 'Bearer configured'


def test_failure_leaves_response_unchanged():
    aw, fr = _setup(outcomes=[OSError('down'), OSError('down')])
    r = _post(aw)
    assert r.status_code == 200 and r.get_json() == dict(status='ok')
    assert len(fr.calls) == 2


def test_5xx_retries_once_then_stops():
    aw, fr = _setup(outcomes=[503, 502, 200])
    r = _post(aw)
    assert r.status_code == 200 and len(fr.calls) == 2


def test_4xx_is_not_retried():
    aw, fr = _setup(outcomes=[404, 200])
    _post(aw)
    assert len(fr.calls) == 1


def test_loop_header_present_means_no_forward():
    aw, fr = _setup()
    r = _post(aw, headers=dict([(LOOP, '1')]))
    assert r.status_code == 200 and fr.calls == []


def test_test_notification_and_non_json_are_forwarded_raw():
    aw, fr = _setup()
    c = aw.app.test_client()
    c.post('/webhook/seerr', data=TEST_BODY, content_type=JSON_CT)
    c.post('/webhook/seerr', data=b'not json', content_type='text/plain')
    aw.threading.Thread = threading.Thread
    assert [x['data'] for x in fr.calls] == [TEST_BODY, b'not json']
    assert fr.calls[1]['headers']['Content-Type'] == 'text/plain'


def test_bad_scheme_is_treated_as_unset():
    aw = _load()
    assert aw._seerr_forward_url_checked('ftp://ha.invalid/x') == ''
    assert aw._seerr_forward_url_checked('ha.invalid/hook') == ''
    assert aw._seerr_forward_url_checked('   ') == ''
    assert aw._seerr_forward_url_checked(None) == ''
    assert aw._seerr_forward_url_checked('https://ha.invalid/hook') == 'https://ha.invalid/hook'


def test_sync_still_scheduled_alongside_forward():
    aw, fr = _setup()
    _post(aw)
    assert '_seerr_sync_job' in InlineThread.ran and len(fr.calls) == 1
