"""Adversarial fixture for: arr-codec-floor-s2-supersede-floor

Drives handle_upgrade_import (Radarr path) with one existing torrent and the
new import, then asserts which of supersede_torrent / remove_torrent fired and
whether a 'supersede-skipped' activity was recorded. The codec floor must block
DOWNGRADES only: same-codec and genuine upgrades keep flowing through both the
soft-supersede and hard-delete paths exactly as before.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC -- see scaffold note: dataclasses on 3.14 resolves
# string annotations through sys.modules[cls.__module__].
sys.modules["target"] = target
spec.loader.exec_module(target)

NEW_HASH = 'NEWHASH00000000000000000000000000000001'
OLD_HASH = 'OLDHASH00000000000000000000000000000002'


def _run_case(import_name, old_name, old_info_extra=None):
    """Run one upgrade import and return
    (supersede_calls, remove_calls, skipped_activity_recorded)."""
    superseded, removed, activities = [], [], []

    def fake_supersede(h):
        superseded.append(h)

    def fake_remove(h, *a, **k):
        removed.append(h)

    def fake_activity(cat, summary):
        activities.append((cat, summary))

    old_info = {'name': old_name, 'label': 'radarr'}
    if old_info_extra:
        old_info.update(old_info_extra)
    torrents = {
        NEW_HASH: {'name': import_name, 'label': 'radarr'},
        OLD_HASH: old_info,
    }

    data = {
        'downloadId': NEW_HASH,
        'movieFile': {'path': f'/downloads/{import_name}', 'releaseGroup': 'GROUP'},
        'movie': {'title': 'Show', 'year': 2019, 'id': None},
    }

    saved = {}

    def patch(name, value):
        saved[name] = getattr(target, name)
        setattr(target, name, value)

    # The module-level dedupe set persists across cases; clear it so the same
    # downloadId is not treated as a duplicate upgrade import.
    patch('_recent_upgrade_download_ids', set())
    patch('deluge_login', lambda *a, **k: None)
    patch('ensure_label_exists', lambda *a, **k: None)
    patch('relabel_download_to_base', lambda h, s: ('flipped', 'x'))
    patch('get_all_torrents', lambda *a, **k: torrents)
    patch('supersede_torrent', fake_supersede)
    patch('remove_torrent', fake_remove)
    patch('record_activity', fake_activity)
    patch('queued_superseded_targets', lambda t: [])
    patch('purge_queued_superseded', lambda t: (0, []))

    try:
        target.handle_upgrade_import(data, 'Radarr')
    finally:
        for name, value in saved.items():
            setattr(target, name, value)
    return (len(superseded), len(removed),
            any(c == 'supersede-skipped' for c, _ in activities))


CASES = [
    # The defect itself: existing x265/HEVC must survive an x264 import --
    # neither soft-superseded nor hard-deleted, and the skip is recorded.
    ("downgrade blocked: existing x265 kept when import is x264",
     lambda: _run_case('Show 2019 x264.mkv', 'Show 2019 x265.mkv'),
     (0, 0, True)),

    # One-directional guard: same-codec must still supersede as before.
    ("same-codec upgrade still supersedes (x264 -> x264)",
     lambda: _run_case('Show 2019 x264-GROUP.mkv', 'Show 2019 x264-OTHER.mkv'),
     (1, 0, False)),

    # A genuine higher-codec upgrade must NOT be blocked.
    ("genuine upgrade still supersedes (existing x264, import x265)",
     lambda: _run_case('Show 2019 x265.mkv', 'Show 2019 x264.mkv'),
     (1, 0, False)),

    # The hard-delete path must be floored too: same-group REPACK, tracker
    # unregistered, seed obligation met -- baseline deletes outright; with the
    # floor in place an x265 existing release survives even that branch.
    ("hard-delete downgrade blocked even when same-group/unregistered/seed-met",
     lambda: _run_case(
         'Show 2019 x264-GROUP.REPACK.mkv',
         'Show 2019 x265-GROUP.REPACK.mkv',
         {'tracker_status': 'unregistered', 'seeding_time': 30 * 86400}),
     (0, 0, True)),

    # Regression half: names with no codec token at all are unaffected.
    ("no-codec names unaffected: plain mkv still supersedes",
     lambda: _run_case('Show 2019.mkv', 'Show 2019 1080p.mkv'),
     (1, 0, False)),
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
