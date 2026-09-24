"""Adversarial fixture for: arr-deluge-false-supersede-915

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

The defect (2026-09-15): Shang-Chi UHD BluRay x265-j3rico and Deadpool 2
UHD BluRay x265-UnKn0wn were relabeled superseded in Deluge with no user
action. The supersede-orphan fixes (525804e, b368987) guard the MOVE after a
supersede and route queue-dupe losers through supersede -- neither touches
the DECISION in dedup_via_radarr that relabels every non-keeper matched
torrent as superseded. The remaining gap: that decision never asks the
tracker whether the candidate is still registered, so an unregistered
torrent (nothing left to seed) gets labeled superseded and moved into the
seed dir -- pure damage with no user action.

The fix adds a pure gate `should_supersede_candidate(info)` -- fail-safe
toward 'may supersede' on unknown/empty status, mirroring
torrent_is_unregistered's direction of caution -- and wires it into the
relabel loop in dedup_via_radarr so unregistered candidates are skipped.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

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


def _dedup_source():
    """Source text of dedup_via_radarr -- used by the wiring cases below."""
    return inspect_getsource()


def inspect_getsource():
    import inspect
    return inspect.getsource(target.dedup_via_radarr)


CASES = [
    # --- the gate: unregistered candidates must NOT be superseded -----------
    ("tracker says 'unregistered' -> do not supersede",
     lambda: target.should_supersede_candidate({'tracker_status': 'Torrent is unregistered'}),
     False),

    ("tracker says 'torrent not found' -> do not supersede",
     lambda: target.should_supersede_candidate({'tracker_status': 'Error: torrent not found on tracker'}),
     False),

    # --- the gate: everything else still supersedable (fail-safe) -----------
    ("registered status -> may supersede",
     lambda: target.should_supersede_candidate({'tracker_status': 'Seeding, 3 peers'}),
     True),

    ("empty tracker_status -> fail-safe toward supersede",
     lambda: target.should_supersede_candidate({'tracker_status': ''}),
     True),

    ("missing tracker_status key -> fail-safe toward supersede (no raise)",
     lambda: target.should_supersede_candidate({}),
     True),

    ("None info -> fail-safe toward supersede (no raise)",
     lambda: target.should_supersede_candidate(None),
     True),

    # --- wiring: the gate must actually sit in the relabel decision ---------
    ("dedup_via_radarr consults should_supersede_candidate before relabeling",
     lambda: 'should_supersede_candidate' in _dedup_source(),
     True),

    ("dedup_via_radarr still performs the supersede for approved candidates",
     lambda: 'supersede_torrent(' in _dedup_source(),
     True),

    # --- regression half: the helper it delegates to keeps its behaviour ----
    ("torrent_is_unregistered: marker match is case-insensitive substring",
     lambda: target.torrent_is_unregistered({'tracker_status': 'UNREGISTERED by tracker'}),
     True),

    ("torrent_is_unregistered: ordinary status stays registered",
     lambda: target.torrent_is_unregistered({'tracker_status': 'Seeding, 3 peers'}),
     False),

    # --- regression half: the orphan guard from 525804e still behaves -------
    ("should_hard_delete_on_upgrade: unregistered + seeded long enough -> True",
     lambda: target.should_hard_delete_on_upgrade(
         {'tracker_status': 'unregistered', 'seeding_time': 365 * 86400}, True),
     True),

    ("should_hard_delete_on_upgrade: different group never hard-deletes",
     lambda: target.should_hard_delete_on_upgrade(
         {'tracker_status': 'unregistered', 'seeding_time': 365 * 86400}, False),
     False),

    # --- end-to-end wiring through dedup_via_radarr (dry run) ---------------
    # The gate must sit in the DECISION loop itself, not just be present in
    # the source: a REGISTERED candidate still gets the relabel action, and
    # an UNREGISTERED one is skipped before any action.
    ("dedup_via_radarr still offers to supersede a registered candidate",
     lambda: _run_dedup_gate_case('Seeding, 3 peers') == 1,
     True),

    ("dedup_via_radarr skips the unregistered candidate (no relabel action)",
     lambda: _run_dedup_gate_case('Torrent is unregistered') == 0,
     True),
]


def _run_dedup_gate_case(candidate_tracker_status):
    """Drive dedup_via_radarr with one keeper + one candidate and return the
    number of 'WOULD relabel superseded' actions logged for the candidate.

    Keeper   : exact filename match against movieFile.relativePath (spared).
    Candidate: same title/year, progress 100 -- its fate is decided purely by
               the gate on tracker_status.

    Mutants this kills:
      * deleting the `continue` after the skip log -> the unregistered
        candidate falls through to the relabel action (case 2 sees 1).
      * negating/dropping the gate condition -> the registered candidate is
        skipped instead (case 1 sees 0), and the unregistered one is
        relabeled (case 2 sees 1).
    """
    import unittest.mock as mock

    keeper_name = 'Shang-Chi.and.the.Legion.of.Ringmasters.2021.UHD.BluRay.x265-keeper.mkv'
    cand_name = 'Shang-Chi.and.the.Legion.of.Ringmasters.2021.UHD.BluRay.x265-j3rico'

    movie_json = [{
        'id': 7,
        'title': 'Shang-Chi and the Legion of Ringmasters',
        'year': 2021,
        'hasFile': True,
        'movieFile': {'relativePath': keeper_name},
    }]

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    with mock.patch.object(target, 'RADARR_API_KEY', 'test-key'), \
         mock.patch.object(target, 'deluge_login'), \
         mock.patch.object(target, 'ensure_label_exists'), \
         mock.patch.object(target, 'get_all_torrents', return_value={
             'KEEPERHASH0001': {'name': keeper_name, 'label': 'radarr', 'progress': 100.0},
             'CANDIDATEHSH01': {'name': cand_name, 'label': 'radarr', 'progress': 100.0,
                                'tracker_status': candidate_tracker_status},
         }), \
         mock.patch.object(target, 'supersede_torrent'), \
         mock.patch.object(target, 'record_activity'), \
         mock.patch.object(target.requests, 'get', side_effect=[
             _Resp(movie_json),  # /api/v3/movie list
             _Resp({'title': 'Shang-Chi and the Legion of Ringmasters', 'originalTitle': ''}),  # movie detail (titles)
         ]), \
         mock.patch.object(target.log, 'info') as log_info:
        target.dedup_via_radarr(dry_run=True)

    lines = [str(c.args[0]) for c in log_info.call_args_list]
    return len([l for l in lines if 'WOULD relabel superseded' in l and cand_name in l])


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
