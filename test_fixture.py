"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s6-batched-search

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

Each case: (description, callable_returning_actual, expected)
"""
import importlib.util
import json as _json
import os
import sys
import tempfile

# Module-level constants are read from env at IMPORT time -- set them before
# exec_module so the target sees a small batch size and an isolated state file.
_STATE_DIR = tempfile.mkdtemp(prefix='upgrade-batch-fixture-')
os.environ['UPGRADE_BATCH_SIZE'] = '4'
os.environ['UPGRADE_STATE_PATH'] = os.path.join(_STATE_DIR, 'state.json')

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _state():
    try:
        with open(os.environ['UPGRADE_STATE_PATH']) as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _set_state(state):
    with open(os.environ['UPGRADE_STATE_PATH'], 'w') as f:
        _json.dump(state, f)


class _FakeAPI:
    """Records every command POST and answers catalog GETs from canned data."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.commands = []  # list of json bodies posted to /api/v3/command

    def get(self, *a, **k):
        return _Resp(list(self.catalog))

    def post(self, url, headers=None, json=None, timeout=None):
        if str(url).endswith('/command'):
            self.commands.append(json)
        return _Resp({'id': 100 + len(self.commands)})


def _run(service, catalog):
    """Drive the real Flask app through its test client; stub only the
    Radarr/Sonarr HTTP layer. Returns (response_dict, commands_posted)."""
    fake = _FakeAPI(catalog)
    orig_get, orig_post = target.requests.get, target.requests.post
    try:
        target.requests.get = fake.get
        target.requests.post = fake.post
        client = target.app.test_client()
        r = client.post('/run-bulk-search?service=' + service)
        return (r.status_code, r.get_json(), list(fake.commands))
    finally:
        target.requests.get, target.requests.post = orig_get, orig_post


# 7 monitored movies; newest-first order must be 2024, 2023, 2021, 2019,
# 2015, 2008, 1999. With UPGRADE_BATCH_SIZE=4 the first pass searches exactly
# the four newest: ids [7, 6, 5, 4].
MOVIES = [
    {'id': 1, 'monitored': True, 'year': 2008},
    {'id': 2, 'monitored': False, 'year': 2030},   # unmonitored: excluded
    {'id': 3, 'monitored': True, 'year': 1999},
    {'id': 4, 'monitored': True, 'year': 2019},
    {'id': 5, 'monitored': True, 'year': 2021},
    {'id': 6, 'monitored': True, 'year': 2023},
    {'id': 7, 'monitored': True, 'year': 2024},
]

# 5 monitored series; newest-first: 9 (2022), 8 (2018), 10 (2016), 11 (no year).
SERIES = [
    {'id': 8, 'monitored': True, 'firstAired': '2018-05-01T00:00:00Z'},
    {'id': 9, 'monitored': True, 'year': 2022},
    {'id': 10, 'monitored': True, 'firstAired': '2016-01-01T00:00:00Z'},
    {'id': 11, 'monitored': True},
]


def case_radarr_first_pass():
    _set_state({})
    status, body, commands = _run('radarr', MOVIES)
    entry = _state().get('radarr') or {}
    return (status, body.get('ok'), body.get('cursor_before'), body.get('cursor_after'),
            [c for c in commands if c and c.get('name') == 'MoviesSearch'],
            entry.get('cursor'), bool(entry.get('last_run')))


def case_radarr_second_pass_wraps():
    # Cursor 3 from a prior pass: next slice is indices 3,4,5 then wraps to 0.
    _set_state({'radarr': {'cursor': 3, 'last_run': '2026-01-01T00:00:00+00:00'}})
    status, body, commands = _run('radarr', MOVIES)
    entry = _state().get('radarr') or {}
    return (status, [c for c in commands if c and c.get('name') == 'MoviesSearch'],
            entry.get('cursor'))


def case_sonarr_per_item_commands():
    # Sonarr's SeriesSearch takes ONE seriesId per command: 4 separate posts,
    # newest-first ids [9, 8, 10, 11], cursor advances to 4.
    _set_state({})
    status, body, commands = _run('sonarr', SERIES)
    entry = _state().get('sonarr') or {}
    return (status,
            [c for c in commands if c and c.get('name') == 'SeriesSearch'],
            entry.get('cursor'), bool(entry.get('last_run')))


def case_invalid_service_400():
    _set_state({})
    client = target.app.test_client()
    r = client.post('/run-bulk-search?service=plexarr')
    return (r.status_code, r.get_json())


def case_empty_catalog_noop():
    # No monitored items: no command may be posted and the call must not 500.
    _set_state({})
    status, body, commands = _run('radarr', [{'id': 1, 'monitored': False}])
    return (status, body.get('ok'), len(commands))


CASES = [
    ("radarr pass searches only the UPGRADE_BATCH_SIZE newest monitored movies "
     "(ids 7,6,5,4 -- not all 6, unmonitored id 2 excluded) and persists "
     "cursor=4 with a last_run timestamp",
     case_radarr_first_pass,
     (200, True, 0, 4, [{'name': 'MoviesSearch', 'movieIds': [7, 6, 5, 4]}], 4, True)),

    ("radarr second pass resumes at stored cursor 3 and wraps past the end "
     "(ids 4,1,3,7), persisting next cursor (3+4)%6=1",
     case_radarr_second_pass_wraps,
     (200, [{'name': 'MoviesSearch', 'movieIds': [4, 1, 3, 7]}], 1)),

    ("sonarr pass issues one SeriesSearch command PER series in the slice "
     "(ids 9,8,10,11 newest-first) and persists cursor (0+4)%4=0 with last_run",
     case_sonarr_per_item_commands,
     (200, [{'name': 'SeriesSearch', 'seriesId': 9},
            {'name': 'SeriesSearch', 'seriesId': 8},
            {'name': 'SeriesSearch', 'seriesId': 10},
            {'name': 'SeriesSearch', 'seriesId': 11}], 0, True)),

    ("unknown service is rejected with a 400 and an error body (not a 500)",
     case_invalid_service_400,
     (400, {'ok': False, 'error': 'service must be radarr or sonarr'})),

    ("empty monitored catalog: no search command posted, still ok (no crash)",
     case_empty_catalog_noop,
     (200, True, 0)),
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
