"""Adversarial fixture for: arr-codec-floor-s3-sort-keys

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


def _radarr(title, score=0, progress=0.0, order=1):
    return {'source': 'queue', 'queue_id': 1, 'hash': (title or '').lower(),
            'title': title, 'score': score, 'progress': progress,
            'order': order}


def _sonarr(title, episodes, score=0, progress=0.0, order=1):
    return {'source': 'queue', 'series_id': 7, 'queue_ids': [1],
            'title': title, 'episodes': set(episodes), 'score': score,
            'progress': progress, 'order': order}


def _radarr_order(*titles_scores):
    cands = [_radarr(t, s) for t, s in titles_scores]
    return [c['title'] for c in sorted(cands, key=target._dupe_candidate_sort_key)]


def _sonarr_order(*specs):
    cands = [_sonarr(t, eps, s) for t, eps, s in specs]
    return [c['title'] for c in sorted(cands, key=target._sonarr_dupe_candidate_sort_key)]


CASES = [
    # Radarr: codec_rank sits ABOVE score -- a lower-scoring HEVC release must
    # beat a higher-scoring AVC one (a plausible wrong fix that puts codec
    # BELOW score fails this).
    ("radarr: better codec beats higher score",
     lambda: _radarr_order(("Movie.HEVC.x265.1080p", 0), ("Movie.AVC.x264.720p", 10)),
     ["Movie.HEVC.x265.1080p", "Movie.AVC.x264.720p"]),

    # Radarr: equal score/progress/order -- HEVC (rank 2) beats AVC (rank 1).
    ("radarr: codec tiebreak on full score tie",
     lambda: _radarr_order(("Movie.AVC.x264.1080p", 5), ("Movie.HEVC.x265.1080p", 5)),
     ["Movie.HEVC.x265.1080p", "Movie.AVC.x264.1080p"]),

    # Radarr regression: same codec (both unknown, rank 0) -- the old contract
    # must hold: higher score first, then progress, then order.
    ("radarr: score/progress/order still decide within one codec",
     lambda: _radarr_order(("Movie.x264.a", 3), ("Movie.x264.b", 9)),
     ["Movie.x264.b", "Movie.x264.a"]),

    # Radarr regression: score tie, progress decides.
    ("radarr: progress still breaks a score+codec tie",
     lambda: [c['title'] for c in sorted(
         [_radarr("A", 5, 0.0), _radarr("B", 5, 42.0)],
         key=target._dupe_candidate_sort_key)],
     ["B", "A"]),

    # Radarr degenerate: a None title must not raise (codec_rank tolerates it)
    # and sorts as rank 0 -- below any named codec release.
    ("radarr: None title does not raise, ranks last vs HEVC",
     lambda: [c['title'] for c in sorted(
         [_radarr(None), _radarr("Movie.HEVC.x265")],
         key=target._dupe_candidate_sort_key)],
     ["Movie.HEVC.x265", None]),

    # Sonarr: coverage stays FIRST -- a 10-episode AVC pack beats a
    # single-episode HEVC grab even though HEVC has the better codec (a wrong
    # fix that puts codec above episode count fails this).
    ("sonarr: coverage still outranks codec",
     lambda: _sonarr_order(("S01E05.HEVC.x265", [1], 9),
                           ("S01.AVC.x264.Pack", list(range(1, 11)), 0)),
     ["S01.AVC.x264.Pack", "S01E05.HEVC.x265"]),

    # Sonarr: equal coverage -- codec_rank now decides above score.
    ("sonarr: codec beats score at equal episode count",
     lambda: _sonarr_order(("S01E05.AVC.x264", [1], 8),
                           ("S01E05.HEVC.x265", [1], 1)),
     ["S01E05.HEVC.x265", "S01E05.AVC.x264"]),

    # Sonarr regression: same codec, score still decides.
    ("sonarr: score still breaks a coverage+codec tie",
     lambda: _sonarr_order(("S01E05.x264.a", [1], 2),
                           ("S01E05.x264.b", [1], 7)),
     ["S01E05.x264.b", "S01E05.x264.a"]),
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
