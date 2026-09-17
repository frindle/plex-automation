# TASK: arr-codec-floor-s1-codec-rank

## Confirmed defect (observed, not suspected)

The codec floor has no way to rank a release by its video codec. Verified by
inspection of the current tree: `grep -n "def codec_rank" arr-webhook.py`
returns nothing — there is no helper mapping release names like
`Movie.2024.1080p.WEB-DL.HEVC.x265-GRP` or `show.s01e02.h.265.720p` to a codec
rank, so the three consumer slices (supersede/upgrade/refuse-to-downgrade) have
nothing to call. This slice adds that helper and nothing else.

## Entry point

arr-webhook.py:224 — insert immediately AFTER the `SEASON_RE = re.compile(...)`
line (the line ending in `(?:[Ee]\d{1,3})?')`), before the blank line that
precedes `session = requests.Session()`.

## Required change

Add a module-level helper `def codec_rank(name)` immediately AFTER SEASON_RE (line 224). It maps a release name to an integer codec rank: HEVC/x265/h265/h.265 -> 2, AVC/x264/h264/h.264 -> 1, anything else (unknown) -> 0. Case-insensitive substring/regex match on the name. Pure function, no side effects, no I/O. This is the ONLY change in this slice; the three consumers come in later slices.

Contract details:
- Matching is case-insensitive and must cover every spelling named above,
  including the dotted forms `h.265` / `H.264` (a word-boundary regex that only
  matches bare tokens misses these).
- If a name names both families (e.g. contains both an AVC token and HEVC),
  return 2 — the better codec wins.
- Degenerate input must not raise: empty string returns 0, `None` returns 0.

Behaviour that must NOT change:
- The module still imports cleanly (`importlib` exec of arr-webhook.py succeeds).
- `SEASON_RE` is untouched and still captures the season number (e.g.
  `SEASON_RE.search('S01E02').group(1)` returns `'01'`).
- No existing route, helper, or module-level state is modified; this slice adds
  exactly one function and nothing else.

## Must contain

- `def codec_rank(name):`
- `return 2`
- `return 1`
- `return 0`

## Scope

Only edit `arr-webhook.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
