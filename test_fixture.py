"""Adversarial fixture for: arr-sonarr-new-request-priority

The defect: in handle_grab, a NEW Sonarr grab (episode has no file yet) is a
bare early return -- the torrent just sits at Deluge's FIFO tail behind every
in-flight download. Upgrades are explicitly moved to core.queue_bottom; new
requests get nothing. The fix must move non-upgrade Sonarr grabs AHEAD of what
is already queued (core.queue_top), and only for them.

The fixture patches target.prioritize_new_sonarr_grab when it exists (post-fix)
and tolerates its absence at baseline -- that is exactly the discrimination:
at baseline no code path can move a new grab to the top, so case 1 fails; with
the fix in place every case passes.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util
from unittest.mock import MagicMock

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


def _sonarr_grab(download_id="abc123"):
    return {
        "downloadId": download_id,
        "series": {"id": 7},
        "episodes": [{"id": 42}],
        "release": {"size": 5 * 1024 ** 3},
    }


def _patch_prio():
    """Patch the prioritization helper if it exists; return (mock_or_None, undo)."""
    real = getattr(target, 'prioritize_new_sonarr_grab', None)
    if real is None:
        return None, lambda: None
    mock = MagicMock()
    target.prioritize_new_sonarr_grab = mock
    return mock, (lambda: setattr(target, 'prioritize_new_sonarr_grab', real))


def _case_new_sonarr_grab_goes_to_top():
    """Non-upgrade Sonarr grab: must be moved ahead of the existing queue via
    core.queue_top, with this download's hash as the parameter. The REAL helper
    runs here (no patch) against a mocked Deluge session -- at baseline no code
    path issues core.queue_top for a new grab, so this case fails."""
    post = MagicMock()
    target.session = MagicMock(post=post)
    target.is_upgrade_sonarr = lambda data: False
    real_login = target.deluge_login
    target.deluge_login = MagicMock()  # no real Deluge in this environment
    try:
        target.handle_grab(_sonarr_grab(), "Sonarr")
    finally:
        del target.is_upgrade_sonarr
        target.deluge_login = real_login
    calls = [c.kwargs.get("json") or (c.args[1] if len(c.args) > 1 else None)
             for c in post.call_args_list]
    calls = [p for p in calls if isinstance(p, dict)]
    tops = [p for p in calls if p.get("method") == "core.queue_top"]
    return bool(tops) and any(p.get("params") == [["abc123"]] for p in tops)


def _case_upgrade_sonarr_grab_not_prioritized():
    """Upgrade Sonarr grab: the new-request prioritization must NOT fire --
    upgrades keep their existing throttle path (queue_bottom), never queue_top."""
    target.is_upgrade_sonarr = lambda data: True
    prio, undo_prio = _patch_prio()
    real_sleep, real_login = target.time.sleep, target.deluge_login
    target.time.sleep = lambda *a, **k: None  # skip the 3s Deluge-registration delay
    target.deluge_login = MagicMock()          # no real Deluge in this environment
    try:
        target.handle_grab(_sonarr_grab(), "Sonarr")
    finally:
        del target.is_upgrade_sonarr
        undo_prio()
        target.time.sleep, target.deluge_login = real_sleep, real_login
    return prio is None or not prio.called


def _case_missing_download_id_skips():
    """No downloadId in the Grab payload: skipped before any Deluge call."""
    prio, undo_prio = _patch_prio()
    try:
        target.handle_grab({"series": {"id": 7}, "episodes": [{"id": 42}]}, "Sonarr")
    finally:
        undo_prio()
    return prio is None or not prio.called


def _case_radarr_source_not_prioritized():
    """Radarr grabs keep their existing behaviour -- the Sonarr-scoped
    prioritization must not fire for a Radarr source, and nothing may raise."""
    target.is_upgrade_radarr = lambda data: None  # non-upgrade Radarr grab
    prio, undo_prio = _patch_prio()
    try:
        target.handle_grab({"downloadId": "def456", "movie": {"id": 3}}, "Radarr")
    finally:
        del target.is_upgrade_radarr
        undo_prio()
    return prio is None or not prio.called


def _case_deluge_failure_does_not_raise():
    """A Deluge error in the prioritization path must be swallowed (logged),
    never raised out of handle_grab -- same contract as the other grab-time
    helpers."""
    target.session = MagicMock(post=MagicMock(side_effect=RuntimeError("deluge down")))
    target.is_upgrade_sonarr = lambda data: False
    prio, undo_prio = _patch_prio()
    try:
        target.handle_grab(_sonarr_grab(), "Sonarr")
    except Exception as e:
        return "raised {}: {}".format(type(e).__name__, e)
    finally:
        del target.is_upgrade_sonarr
        undo_prio()
    return True


CASES = [
    ("new (non-upgrade) Sonarr grab is moved to top of Deluge queue via core.queue_top",
     _case_new_sonarr_grab_goes_to_top, True),
    ("upgrade Sonarr grab does NOT get new-request prioritization",
     _case_upgrade_sonarr_grab_not_prioritized, True),
    ("Grab payload with no downloadId is skipped without any Deluge call",
     _case_missing_download_id_skips, True),
    ("Radarr source keeps existing behaviour -- Sonarr-scoped prioritization does not fire",
     _case_radarr_source_not_prioritized, True),
    ("Deluge failure in the prioritization path is swallowed, never raised out of handle_grab",
     _case_deluge_failure_does_not_raise, True),
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
