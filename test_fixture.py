"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s7-scheduler-gate

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
import sys
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# media_share imports paramiko at module level; it is not installed in the
# verify environment. Stub it BEFORE importing the target so the real app
# (Flask `app`) loads and can be driven through its test client.
import types
if 'paramiko' not in sys.modules:
    try:
        import paramiko  # noqa: F401
    except ImportError:
        sys.modules['paramiko'] = types.ModuleType('paramiko')

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


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()

def _one_poll(state):
    """Run exactly one hourly poll of monthly_search_scheduler and return the
    ordered list of services whose cycle it started. time.sleep is patched to
    raise StopIteration so the loop exits after that single poll, and
    monthly_upgrade_cycle is recorded instead of running (no 30-minute waits)."""
    calls = []
    with patch.object(target.time, 'sleep', side_effect=StopIteration), \
         patch.object(target, '_load_upgrade_state', return_value=state), \
         patch.object(target, 'monthly_upgrade_cycle',
                      side_effect=lambda svc: calls.append(svc)):
        try:
            target.monthly_search_scheduler()
        except StopIteration:
            pass
    return calls

class _PinnedDatetime(datetime):
    """datetime whose now() returns a fixed instant (naive or tz-aware)."""
    _fixed = None
    @classmethod
    def now(cls, tz=None):
        if cls._fixed is not None:
            return cls._fixed.replace(tzinfo=tz) if tz else cls._fixed
        return super().now(tz)

def _one_poll_pinned(state, fixed_now_utc):
    """Like _one_poll but with the scheduler's clock pinned to `fixed_now_utc`
    so a timestamp exactly UPGRADE_BATCH_INTERVAL_DAYS old yields elapsed_days
    that is EXACTLY the interval -- where >= and > diverge. The scheduler does
    `import datetime` inside its loop, so we swap sys.modules['datetime'] for
    a module whose datetime class has now() pinned."""
    calls = []
    pass
    _PinnedDatetime._fixed = fixed
    fake_mod = types.ModuleType('datetime')
    fake_mod.datetime = _PinnedDatetime
    fake_mod.timezone = timezone  # refimpl calls datetime.now(datetime.timezone.utc)
    try:
        with patch.object(target.time, 'sleep', side_effect=StopIteration), \
             patch.dict(sys.modules, {'datetime': fake_mod}), \
             patch.object(target, '_load_upgrade_state', return_value=state), \
             patch.object(target, 'monthly_upgrade_cycle',
                          side_effect=lambda svc: calls.append(svc)):
            try:
                target.monthly_search_scheduler()
            except StopIteration:
                pass
    finally:
        _PinnedDatetime._fixed = None
    return calls

def _route(method, url):
    """Drive the real Flask app through its test client; (status, body)."""
    with target.app.test_client() as c:
        r = c.open(url, method=method)
        return r.status_code, r.get_json()


CASES = [
    # --- scheduler gate: absent state -> both services due on the first poll --
    ("absent state runs radarr then sonarr",
     lambda: _one_poll({}), ['radarr', 'sonarr']),

    # --- entry present but no 'last_run' key -> still due ---------------------
    ("entry without last_run key is due",
     lambda: _one_poll({'radarr': {'cursor': 5}, 'sonarr': {}}),
     ['radarr', 'sonarr']),

    # --- fresh timestamps (well inside the interval) -> nothing runs ----------
    ("fresh last_run within interval runs nothing",
     lambda: _one_poll({
         'radarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(hours=1))},
         'sonarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS - 1))},
     }), []),

    # --- stale timestamps (past the interval) -> both run again ---------------
    ("stale last_run past interval runs both",
     lambda: _one_poll({
         'radarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS + 1))},
         'sonarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS + 2))},
     }), ['radarr', 'sonarr']),

    # --- exact boundary: exactly UPGRADE_BATCH_INTERVAL_DAYS elapsed -> due ----
    # (minus 1s so the few ms of fixture overhead can't push it back under the
    # threshold; a correct >= comparison still fires at this point)
    ("exactly interval-days elapsed is due",
     lambda: _one_poll({
         'radarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS, seconds=1))},
         'sonarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(hours=1))},
     }), ['radarr']),

    # --- EXACT boundary, clock pinned: elapsed_days == interval exactly -------
    # With now() frozen and last_run exactly UPGRADE_BATCH_INTERVAL_DAYS old,
    # elapsed_days is precisely the interval (no ms of fixture overhead). A
    # `>` comparison would skip this service; only `>=` fires it.
    ("exactly-interval-elapsed with pinned clock is due",
     lambda: _one_poll_pinned({
         'radarr': {'last_run': _iso(datetime(2026, 9, 1, 0, 0, 0) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS))},
         'sonarr': {'last_run': _iso(datetime(2026, 9, 1, 0, 0, 0) - timedelta(hours=1))},
     }, datetime(2026, 9, 1, 0, 0, 0)), ['radarr']),

    # --- per-service independence: only the stale one runs --------------------
    ("only the stale service is due",
     lambda: _one_poll({
         'radarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(days=target.UPGRADE_BATCH_INTERVAL_DAYS + 5))},
         'sonarr': {'last_run': _iso(datetime.now(timezone.utc) - timedelta(hours=2))},
     }), ['radarr']),

    # --- degenerate: unparseable last_run must not raise, treated as due ------
    ("unparseable last_run is due and does not raise",
     lambda: _one_poll({'radarr': {'last_run': 'not-a-date'}, 'sonarr': {}}),
     ['radarr', 'sonarr']),

    # --- regression: manual trigger endpoint still works unchanged ------------
    ("POST /run-monthly-upgrade?service=radarr&skip_waits=1 -> 200 ok",
     lambda: _route('POST', '/run-monthly-upgrade?service=radarr&skip_waits=1'),
     (200, {'ok': True, 'service': 'radarr', 'skip_waits': True,
            'message': 'monthly upgrade cycle started; watch container logs'})),

    # --- regression: invalid service -> 400 with the right error body ---------
    ("POST /run-monthly-upgrade?service=plex -> 400",
     lambda: _route('POST', '/run-monthly-upgrade?service=plex'),
     (400, {'ok': False, 'error': 'service must be radarr, sonarr or both'})),
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
