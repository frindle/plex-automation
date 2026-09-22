# TASK: arr-codec-floor-s4-pack-vs-pack

## Confirmed defect (observed, not suspected)

`dedup_via_sonarr` resolves singles-vs-pack, redundant-pack-vs-singles and
per-episode dupes, but when TWO season packs for the SAME series+season both
seed in the sonarr-labeled set (e.g. an H.265 re-rip next to the AVC original),
no pass compares a pack against another pack: neither `select_singles_superseded_by_pack`
nor `select_pack_superseded_by_singles` returns packs, and the per-episode
grouping skips them (no SxxExx token). The lower-codec pack keeps seeding
forever. Reproduced by unit-driving the decision surface with two same-season
packs in the matched set: zero supersede decisions are produced.

## Entry point

arr-webhook.py:1436 — inside `dedup_via_sonarr`, between the redundant-pack
pass and the per-episode grouping (`by_episode = {}`).

## Required change

Add a same-season pack-vs-pack pass alongside the existing pack passes (near
line 1436, before the per-episode grouping): build a dict named exactly
`same_season_packs` keyed by `(series_id, season)` that groups matched
season-pack torrents (no SxxExx token — `EPISODE_RE` must not match — and a
`SEASON_RE` match). For each group with more than one pack, keep the one with
the higher `codec_rank` (tie-break by existing keeper signals — coverage/
score/history as the other passes do) and supersede the loser pack(s). Honour
dry_run exactly as the surrounding passes do: no `supersede_torrent`, no
`record_activity` when dry_run, and append one report dict per would-be-
superseded loser pack. Never cross-supersede packs in different seasons —
different dict keys must never compare against each other.

Behaviour that must NOT change:
- Singles-vs-pack (`select_singles_superseded_by_pack`) decisions unchanged.
- Redundant-pack-vs-singles (`select_pack_superseded_by_singles`) decisions unchanged.
- Per-episode dupe resolution (history / filename-exact / sourceTitle keepers) unchanged.
- Individual-episode torrents are never selected by the new pass.
- A single season pack with no same-season rival is never touched.

## Must contain

- `select_same_season_pack_losers`
- `same_season_packs`
- `lower-codec pack, same season`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

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
