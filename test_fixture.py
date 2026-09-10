"""Adversarial fixture for: asian-tv-router

Two layers, because the fix has two parts:
  1. `route_series_to_asian(data)` -- the decision+move. Driven directly with a
     mocked Sonarr API; asserts return value AND the editor PUT payload.
  2. The `elif event == 'SeriesAdd':` WIRING in sonarr_webhook(). Driven through
     the real Flask route with a webhook POST, so a wrong event name / missing
     dispatch / inverted branch is caught (calling the function directly cannot
     see the wiring).

Cases separate "routed the CJK series correctly" from "made the test go green":
Korean/Chinese in the default root move; English and already-Asian never move;
missing fields never raise; Grab must NOT route; SeriesAdd must.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import threading
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

ASIAN = '/data/Media/TV Shows - Asian'
DEFAULT = '/data/Media/TV Shows'


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _run(series_obj):
    """Patch Sonarr GET/PUT, call the router directly, return a dict of what it
    did: return value, the editor PUT json, and the headers each call sent."""
    cap = {}

    def fake_get(url, *a, **k):
        cap['get_headers'] = k.get('headers')
        cap['get_url'] = url
        return _Resp(series_obj)

    def fake_put(url, *a, **k):
        cap['put_headers'] = k.get('headers')
        cap['put_json'] = k.get('json')
        return _Resp({})

    orig_get, orig_put = target.requests.get, target.requests.put
    target.requests.get = fake_get
    target.requests.put = fake_put
    try:
        cap['ret'] = target.route_series_to_asian({'series': {'id': series_obj.get('id')}})
    finally:
        target.requests.get = orig_get
        target.requests.put = orig_put
    return cap


def ret_of(s):
    return _run(s).get('ret')


def put_of(s):
    return _run(s).get('put_json')


def moved(s):
    return _run(s).get('put_json') is not None


def get_header_present(s):
    return 'X-Api-Key' in (_run(s).get('get_headers') or {})


def put_header_present(s):
    return 'X-Api-Key' in (_run(s).get('put_headers') or {})


def webhook_routes(event, series_obj):
    """Drive the REAL sonarr_webhook() Flask route with a webhook POST; return
    True iff the editor PUT fired. Waits for the daemon thread the handler
    spawns. Restores requests only after the move has fired (positive) or the
    router has finished without moving (negative), so no real network call
    escapes."""
    fired = threading.Event()

    def fake_get(url, *a, **k):
        return _Resp(series_obj)

    def fake_put(url, *a, **k):
        fired.set()
        return _Resp({})

    orig_get, orig_put = target.requests.get, target.requests.put
    target.requests.get = fake_get
    target.requests.put = fake_put
    try:
        client = target.app.test_client()
        client.post('/webhook/sonarr',
                    json={'eventType': event, 'series': {'id': series_obj.get('id')}})
        # positive: unblocks the instant the PUT fires; negative: full 2s (the
        # non-moving router finishes well under that, so restore is safe).
        got = fired.wait(timeout=2)
    finally:
        target.requests.get = orig_get
        target.requests.put = orig_put
    return got


def get_called_for(data):
    """True iff route_series_to_asian issues the Sonarr GET for this payload.
    Proves the `if not series_id` guard short-circuits BEFORE any HTTP (a case
    that just checks the return value can't -- the exception path also yields
    None)."""
    calls = {'n': 0}

    def fake_get(url, *a, **k):
        calls['n'] += 1
        return _Resp({})

    orig = target.requests.get
    target.requests.get = fake_get
    try:
        target.route_series_to_asian(data)
    finally:
        target.requests.get = orig
    return calls['n'] > 0


_KOREAN = {'id': 1, 'originalLanguage': {'name': 'Korean'}, 'rootFolderPath': DEFAULT}
_ENGLISH = {'id': 2, 'originalLanguage': {'name': 'English'}, 'rootFolderPath': DEFAULT}
_JP_ALREADY = {'id': 3, 'originalLanguage': {'name': 'Japanese'}, 'rootFolderPath': ASIAN}
_CHINESE = {'id': 4, 'originalLanguage': {'name': 'Chinese'}, 'rootFolderPath': DEFAULT}
_NOLANG = {'id': 5, 'rootFolderPath': DEFAULT}


CASES = [
    # --- route_series_to_asian: the decision + move ---
    ("korean in default root -> returns asian root", lambda: ret_of(_KOREAN), ASIAN),
    ("korean move PUTs editor with asian rootFolderPath",
     lambda: put_of(_KOREAN).get('rootFolderPath'), ASIAN),
    ("korean move targets exactly this series id",
     lambda: put_of(_KOREAN).get('seriesIds'), [1]),
    ("korean move sets moveFiles true", lambda: put_of(_KOREAN).get('moveFiles'), True),
    ("chinese in default root -> returns asian root", lambda: ret_of(_CHINESE), ASIAN),
    ("english in default root -> no move", lambda: moved(_ENGLISH), False),
    ("english -> returns None", lambda: ret_of(_ENGLISH), None),
    ("japanese already in asian root -> no move (idempotent)",
     lambda: moved(_JP_ALREADY), False),
    ("missing originalLanguage -> no move, no raise", lambda: moved(_NOLANG), False),
    ("no series id -> None, no HTTP",
     lambda: target.route_series_to_asian({'series': {}}), None),
    # --- API auth headers (a wrong header name breaks the real Sonarr call) ---
    ("series GET carries X-Api-Key header", lambda: get_header_present(_KOREAN), True),
    ("editor PUT carries X-Api-Key header", lambda: put_header_present(_KOREAN), True),
    # --- the SeriesAdd WIRING in sonarr_webhook() (direct calls can't see it) ---
    ("SeriesAdd webhook routes a korean series", lambda: webhook_routes('SeriesAdd', _KOREAN), True),
    ("Grab webhook does NOT route (wrong event)", lambda: webhook_routes('Grab', _KOREAN), False),
    ("missing series id -> no Sonarr GET (guard short-circuits)",
     lambda: get_called_for({'series': {}}), False),
    ("unknown event (not Grab/Download/SeriesAdd) does NOT route",
     lambda: webhook_routes('Test', _KOREAN), False),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
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
