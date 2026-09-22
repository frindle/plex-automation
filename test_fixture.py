"""Adversarial fixture for: arr-codec-floor-s4-pack-vs-pack

Two season packs for the SAME series+season both seeding in the sonarr-labeled
set must resolve to the higher codec_rank (tie-break by keeper signal), with
dry_run honoured exactly like the surrounding passes. Cases below separate a
correct pass from plausible wrong ones: cross-season comparison, rank ties,
keeper tie-breaks, and dry-run mutation leaks.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util
from unittest import mock

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


H265_PACK = 'aa' * 20
AVC_PACK = 'bb' * 20
S1_H265 = 'cc' * 20
SINGLE = 'dd' * 20

NAMES = {
    H265_PACK: 'Show.2024.S02.1080p.WEB.H.265-GroupA',
    AVC_PACK: 'Show.2024.S02.1080p.WEB.x264-GroupB',
    S1_H265: 'Show.2024.S01.1080p.WEB.H.265-GroupA',
    SINGLE: 'Show.2024.S02E03.1080p.WEB.x264-GroupB',
}


def _helper(*args):
    return target.select_same_season_pack_losers(*args)


def _run_dedup(dry_run, names=None, keeper_ids=frozenset()):
    """Run dedup_via_sonarr with Sonarr/Deluge fully mocked; two S02 packs in
    the sonarr-labeled set. Returns (report, supersede_calls)."""
    names = NAMES if names is None else dict(names)
    hashes = list(names)
    torrents = {h: {'name': n, 'label': 'sonarr', 'progress': 100.0}
                for h, n in names.items()}
    series_resp = mock.Mock()
    series_resp.json.return_value = [{'id': 7, 'title': 'Show'}]
    detail_resp = mock.Mock()
    detail_resp.json.return_value = {'title': 'Show', 'originalTitle': ''}
    ef_resp = mock.Mock()
    ef_resp.json.return_value = [{'relativePath': 'show/s02e03.mkv'}]

    supersede_calls = []
    with mock.patch.object(target, 'SONARR_API_KEY', 'k'), \
         mock.patch.object(target.requests, 'get',
                           side_effect=[series_resp, detail_resp, ef_resp]), \
         mock.patch.object(target, 'deluge_login'), \
         mock.patch.object(target, 'ensure_label_exists'), \
         mock.patch.object(target, 'get_all_torrents', return_value=torrents), \
         mock.patch.object(target, '_sonarr_series_imported_download_ids',
                           return_value=set()), \
         mock.patch.object(target, '_sonarr_latest_keeper_by_episode_key',
                           return_value={}), \
         mock.patch.object(target, '_sonarr_latest_source_title_by_episode_key',
                           return_value={}), \
         mock.patch.object(target, '_sonarr_keeper_pack_info',
                           return_value=(set(keeper_ids), set())), \
         mock.patch.object(target, '_sonarr_keeper_single_seasons',
                           return_value=set()), \
         mock.patch.object(target, 'supersede_torrent',
                           side_effect=lambda h: supersede_calls.append(h)), \
         mock.patch.object(target, 'record_activity'):
        report = target.dedup_via_sonarr(dry_run=dry_run)
    return (report, tuple(supersede_calls))


CASES = [
    # helper boundaries -------------------------------------------------------
    ("higher codec_rank wins: AVC pack is the loser",
     lambda: _helper([H265_PACK, AVC_PACK], NAMES),
     [AVC_PACK]),

    ("rank tie with no keeper signal -> keep all (never guess)",
     lambda: _helper([H265_PACK, 'ee' * 20],
                     {H265_PACK: NAMES[H265_PACK],
                      'ee' * 20: 'Show.2024.S02.1080p.WEB.H.265-GroupC'}),
     []),

    ("rank tie broken by keeper signal: non-keeper pack loses",
     lambda: _helper([H265_PACK, 'ee' * 20],
                     {H265_PACK: NAMES[H265_PACK],
                      'ee' * 20: 'Show.2024.S02.1080p.WEB.H.265-GroupC'},
                     keeper_ids={H265_PACK}),
     ['ee' * 20]),

    ("single-pack group -> nothing to supersede",
     lambda: _helper([AVC_PACK], NAMES),
     []),

    ("None/empty inputs must not raise and select nothing",
     lambda: (_helper(None, None) == []) and (_helper([], {}) == []),
     True),

    # dedup_via_sonarr integration -------------------------------------------
    ("dry_run: loser reported once, no supersede_torrent / record_activity",
     lambda: _run_dedup(True)[0] == [{
         'series_id': 7, 'series': 'Show', 'season': 2,
         'pack_hash': AVC_PACK,
         'pack_name': NAMES[AVC_PACK],
     }],
     True),

    ("dry_run: zero supersede_torrent calls",
     lambda: _run_dedup(True)[1] == (),
     True),

    ("live: loser pack is superseded exactly once",
     lambda: _run_dedup(False)[1] == (AVC_PACK,),
     True),

    # adversarial: a wrong impl that compares across seasons fails these -----
    ("different seasons never compare: S01 H.265 vs S02 AVC -> no action",
     lambda: (_run_dedup(True, names={S1_H265: NAMES[S1_H265],
                                      AVC_PACK: NAMES[AVC_PACK]})[0] == []),
     True),

    ("a single (SxxExx) never joins the pack group -> no action",
     lambda: (_run_dedup(True, names={H265_PACK: NAMES[H265_PACK],
                                      SINGLE: NAMES[SINGLE]})[0] == []),
     True),
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
