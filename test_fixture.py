"""Adversarial fixture for radarr-sourcetitle-fallback.

Ported from Sonarr #2 (test_blank_did.py) since this fix mirrors it: the same
release-name-generic selector (select_episode_keeper_by_source_title) is reused,
and the NEW parts are a Radarr I/O helper + the wiring into dedup_via_radarr.

Discriminating cases (fail at baseline, pass on the fix): the new helper is
defined, and dedup_via_radarr actually calls both the helper and the selector.
Relevance cases (pin correctness): the selector picks the sourceTitle match for
a MOVIE, ext-strips, and returns None on 0/>1/None rather than guessing.
"""
import ast
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

H_KEEP = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
H_OTHER = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
KEPT = 'Now.You.See.Me.2.2016.1080p.BluRay.x264-GROUP'
OTHER = 'Now.You.See.Me.2.2016.720p.WEB.x264-OTHER'
NAMES = {H_KEEP: KEPT, H_OTHER: OTHER}


def _calls_in(fn_name):
    """Set of function names called inside function `fn_name` in arr-webhook.py."""
    tree = ast.parse(open('arr-webhook.py').read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    if fn is None:
        return set()
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


f = target.select_episode_keeper_by_source_title

CASES = [
    # --- relevance: the reused selector is correct for a MOVIE case ---
    ("sourceTitle match picks the keeper (movie)",
     lambda: f([H_KEEP, H_OTHER], NAMES, KEPT), H_KEEP),
    ("ext-strip: a .mkv sourceTitle still matches",
     lambda: f([H_KEEP], {H_KEEP: KEPT + '.mkv'}, KEPT), H_KEEP),
    ("case-insensitive match",
     lambda: f([H_KEEP, H_OTHER], NAMES, KEPT.upper()), H_KEEP),
    ("0 matches -> None (fall back, do not guess)",
     lambda: f([H_KEEP, H_OTHER], NAMES, 'Totally.Different.Release-XYZ'), None),
    (">1 identical matches -> None (ambiguous, do not guess)",
     lambda: f([H_KEEP, H_OTHER], {H_KEEP: KEPT, H_OTHER: KEPT}, KEPT), None),
    ("None sourceTitle -> None, no raise",
     lambda: f([H_KEEP], NAMES, None), None),
    ("non-string sourceTitle -> None, no raise",
     lambda: f([H_KEEP], NAMES, 12345), None),
    # --- discriminating: the NEW helper exists ---
    ("_radarr_last_imported_source_title is defined",
     lambda: hasattr(target, '_radarr_last_imported_source_title'), True),
    # --- discriminating: the fix is WIRED into dedup_via_radarr ---
    ("dedup_via_radarr calls the sourceTitle helper",
     lambda: '_radarr_last_imported_source_title' in _calls_in('dedup_via_radarr'), True),
    ("dedup_via_radarr calls the sourceTitle selector",
     lambda: 'select_episode_keeper_by_source_title' in _calls_in('dedup_via_radarr'), True),
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
