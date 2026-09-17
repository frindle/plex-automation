"""Adversarial fixture for: arr-codec-floor-s1-codec-rank

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
spec.loader.exec_module(target)


CASES = [
    # --- rank 2: HEVC family, every spelling the spec names -----------------
    ("HEVC uppercase maps to rank 2",
     lambda: target.codec_rank('Movie.2024.1080p.WEB-DL.HEVC.x265-GRP'), 2),
    # A word-boundary regex like r'\b(hevc|x265|h265)\b' misses the dotted form;
    # a plain 'h265' substring check also misses it. Both must map to 2.
    ("dotted h.265 lowercase maps to rank 2",
     lambda: target.codec_rank('show.s01e02.h.265.720p'), 2),
    # --- rank 1: AVC family --------------------------------------------------
    ("x264 maps to rank 1",
     lambda: target.codec_rank('Movie.2023.2160p.BluRay.x264-GRP'), 1),
    # Dotted H.264 must not be missed by a word-boundary regex, and must not be
    # swallowed into rank 2 by an over-eager 'h26' prefix match.
    ("dotted H.264 uppercase maps to rank 1",
     lambda: target.codec_rank('Movie.H.264.1080p'), 1),
    ("bare AVC token maps to rank 1",
     lambda: target.codec_rank('Movie.AVC.1080p'), 1),
    # --- ordering / precedence ------------------------------------------------
    # A plausible wrong fix checks the AVC branch first and returns 1 here;
    # HEVC must win when both codecs are named in one release name.
    ("HEVC wins when both AVC and HEVC appear",
     lambda: target.codec_rank('hybrid.avc.hevc.release'), 2),
    # --- rank 0: unknown / degenerate inputs ---------------------------------
    ("unknown codec (xvid) maps to 0",
     lambda: target.codec_rank('Movie.xvid.720p'), 0),
    ("empty name maps to 0 without raising",
     lambda: target.codec_rank(''), 0),
    ("None maps to 0 without raising",
     lambda: target.codec_rank(None), 0),
    # --- regression half: the module around the insertion point still works --
    ("regression: SEASON_RE still captures the season number",
     lambda: target.SEASON_RE.search('S01E02').group(1), '01'),
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
