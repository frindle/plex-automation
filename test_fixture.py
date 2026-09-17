"""Adversarial fixture for: arr-new-grab-queue-top-r2

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

This fixture drives the REAL Flask app through its test client (POST to
/webhook/radarr and /webhook/sonarr), with Deluge + *arr APIs stubbed at the
module boundary, and asserts on status codes, response bodies, and the exact
ordered sequence of Deluge JSON-RPC calls handle_grab makes:

  * a NEW (non-upgrade) grab must produce core.queue_top with [downloadId],
    JSON-RPC id 92, and timeout=10 -- for both Radarr and Sonarr payloads
  * an upgrade grab over 10GB must STILL be throttled to core.queue_bottom
    and must NOT be topped (a fix that tops everything fails this)
  * an upgrade grab under 10GB keeps its old behaviour: no Deluge calls at all
  * a Grab payload with no downloadId makes NO Deluge call (no queue_top of None)
  * a Deluge outage on the new-grab path is swallowed: route still answers
    200 {'status': 'ok'} and the worker error never propagates

Each case: (description, callable_returning_actual, expected)
"""
import logging
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

GB = 1024 ** 3
HASH = 'abcdef0123456789abcdef0123456789abcdef01'


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Cap(logging.Handler):
    """Captures what handle_grab logs so the fixture can assert on it."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


_CAP = _Cap()
target.log.addHandler(_CAP)


class _World:
    """Fake Deluge + *arr API. Records every Deluge JSON-RPC call in order and
    runs the webhook worker threads synchronously (daemon-thread semantics kept:
    a worker exception is captured, never propagated to the route)."""

    def __init__(self, has_file=False, deluge_down=False):
        self.has_file = has_file
        self.deluge_down = deluge_down
        self.posts = []          # (method, params) in call order
        self.calls = []          # full Deluge JSON-RPC requests: method/params/id/timeout
        self.logins = 0
        self.thread_errors = []

    def install(self):
        aw = target

        class _T:
            def __init__(self, target=None, args=(), daemon=False):
                self._t, self._a = target, tuple(args or ())

            def start(self):
                try:
                    self._t(*self._a)
                except Exception as e:
                    self.thread_errors.append(e)

        real = (aw.requests, aw.session, aw.threading, aw.time,
                aw.deluge_login, aw.record_activity)
        aw.requests = type('R', (), {'get': staticmethod(self.get),
                                     'post': staticmethod(self.post)})()
        aw.session = type('S', (), {'post': staticmethod(self.post)})()
        aw.threading = type('Th', (), {'Thread': _T})()
        aw.time = type('Time', (), {'sleep': staticmethod(lambda *a, **k: None)})()
        aw.deluge_login = self.login
        aw.record_activity = lambda *a, **k: None
        return real

    def restore(self, real):
        (target.requests, target.session, target.threading, target.time,
         target.deluge_login, target.record_activity) = real

    # ── Deluge ──────────────────────────────────────────────────────────
    def login(self):
        if self.deluge_down:
            raise ConnectionError('deluge unreachable')
        self.logins += 1

    def post(self, url, json=None, timeout=None, **kw):
        method = (json or {}).get('method')
        if '/json' in url:  # Deluge JSON-RPC endpoint
            self.posts.append((method, (json or {}).get('params')))
            self.calls.append({'url': url, 'method': method,
                               'params': (json or {}).get('params'),
                               'id': (json or {}).get('id'),
                               'timeout': timeout})
            if self.deluge_down:
                raise ConnectionError('deluge unreachable')
            if method == 'label.get_labels':
                return _Resp({'result': ['radarr-upgrade', 'sonarr-upgrade']})
            return _Resp({'result': None})
        # *arr command endpoint (RefreshMovie/RefreshSeries) -- best-effort no-op
        return _Resp({})

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        if '/api/v3/history' in url:
            return _Resp({'records': []})
        if '/api/v3/movie/' in url or '/api/v3/episode/' in url:
            return _Resp({'hasFile': self.has_file})
        raise AssertionError(f'unexpected GET {url}')


def _run(route, payload, has_file=False, deluge_down=False):
    w = _World(has_file=has_file, deluge_down=deluge_down)
    real = w.install()
    _CAP.records.clear()
    try:
        resp = target.app.test_client().post(route, json=payload)
        return (resp.status_code, resp.get_json(), list(w.posts), w.logins,
                list(w.thread_errors), list(_CAP.records), list(w.calls))
    finally:
        w.restore(real)


def _new_radarr(size_gb=5):
    return {'eventType': 'Grab', 'downloadId': HASH, 'movie': {'id': 7},
            'release': {'size': int(size_gb * GB)}}


def _new_sonarr(size_gb=5):
    return {'eventType': 'Grab', 'downloadId': HASH, 'series': {'id': 3},
            'episodes': [{'id': 11}], 'release': {'size': int(size_gb * GB)}}


CASES = [
    ("new Radarr grab is moved to the TOP of the Deluge queue via "
     "core.queue_top with exactly [downloadId], after deluge_login, and the "
     "route still answers 200 {'status': 'ok'}",
     lambda: (lambda r: (r[0], r[1], r[2], r[3], bool(r[4]),
                         any(f'moved {HASH} to top of queue' in m for m in r[5])))(
         _run('/webhook/radarr', _new_radarr())),
     (200, {'status': 'ok'}, [('core.queue_top', [HASH])], 1, False, True)),

    ("new Sonarr grab is also topped (both sources share the branch)",
     lambda: (lambda r: (r[0], r[1], r[2]))(_run('/webhook/sonarr', _new_sonarr())),
     (200, {'status': 'ok'}, [('core.queue_top', [HASH])])),

    ("regression: an upgrade grab over 10GB is STILL throttled to "
     "core.queue_bottom and must NOT be topped",
     lambda: (lambda r: (r[0], any(m == 'core.queue_bottom' for m, _ in r[2]),
                         not any(m == 'core.queue_top' for m, _ in r[2])))(
         _run('/webhook/radarr', _new_radarr(size_gb=20), has_file=True)),
     (200, True, True)),

    ("regression: an upgrade grab under 10GB keeps its old behaviour -- no "
     "Deluge calls at all (no queue_top, no queue_bottom)",
     lambda: (lambda r: (r[0], r[2]))(_run('/webhook/radarr', _new_radarr(size_gb=5), has_file=True)),
     (200, [])),

    ("a Grab payload with a missing downloadId makes NO Deluge call at all "
     "(no queue_top of None/empty)",
     lambda: (lambda r: (r[0], r[2]))(_run('/webhook/radarr', {'eventType': 'Grab', 'movie': {'id': 7}})),
     (200, [])),

    ("a Deluge outage on the new-grab path is swallowed: route still answers "
     "200 {'status': 'ok'}, no worker exception escapes, and the failure is logged",
     lambda: (lambda r: (r[0], r[1], bool(r[4]), any('failed to move' in m for m in r[5])))(
         _run('/webhook/radarr', _new_radarr(), deluge_down=True)),
     (200, {'status': 'ok'}, False, True)),

    ("the queue_top JSON-RPC request carries id 92 under the key 'id' -- a "
     "renamed key ('id_X') or a shifted value (91/93) breaks the Deluge call",
     lambda: (lambda r: next((c['id'] for c in r[6] if c['method'] == 'core.queue_top'), None))(
         _run('/webhook/radarr', _new_radarr())),
     92),

    ("the queue_top request is sent with timeout=10 -- a drifted constant "
     "(9/11) changes the Deluge call contract",
     lambda: (lambda r: next((c['timeout'] for c in r[6] if c['method'] == 'core.queue_top'), None))(
         _run('/webhook/radarr', _new_radarr())),
     10),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
