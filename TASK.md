# TASK: arr-deluge-false-supersede-915

## Confirmed defect (observed, not suspected)

On 2026-09-15 two complete torrents — `Shang-Chi.and.the.Legion.of.Ringmasters.2021.UHD.BluRay.x265-j3rico` and `Deadpool.2.2018.UHD.BluRay.x265-UnKn0wn` — were relabeled `superseded` in Deluge with no user action (no grab, no upgrade fired by the user). Verified against the code path: `dedup_via_radarr()` is the only pass that relabels radarr-labeled torrents superseded on its own schedule, and its relabel loop calls `supersede_torrent(h)` for every matched non-keeper torrent unconditionally — it never consults `tracker_status`. A candidate whose tracker has already unregistered it can no longer accrue seeding credit; labeling it superseded (and moving it into the seed dir) is pure damage with nothing left to seed. The recent supersede-orphan fixes do NOT cover this: 525804e only guards the *move* after a supersede decision has already been made, and b368987 only changes how queue-dupe losers are routed — neither touches the supersede DECISION in `dedup_via_radarr`. Reproduced mechanically by the fixture's gate cases: at baseline there is no function that can refuse to supersede an unregistered candidate.

## Entry point

`arr-webhook.py`:1160 (the `supersede_torrent(h)` call inside `dedup_via_radarr`'s relabel loop; the decision block starts around line 1128)

## Required change

diagnose then fix: on 9/15 two torrents (Shang-Chi UHD BluRay x265-j3rico, Deadpool 2 UHD BluRay x265-UnKn0wn) were incorrectly relabeled superseded in Deluge with no user action; determine whether the recent supersede-orphan fixes (commits 525804e, b368987) already cover this failure mode, and if not, close the remaining gap in the supersede-decision path

Concretely: add a pure decision gate `should_supersede_candidate(info)` that returns False when the tracker has unregistered the torrent (reusing the existing `torrent_is_unregistered` markers), fail-safe toward True on unknown/empty status and non-dict input, and wire it into `dedup_via_radarr`'s relabel loop so an unregistered candidate is skipped (logged) instead of being relabeled superseded.

Behaviour that must NOT change:
- In-flight guard still holds: torrents with progress < 99.0 are never matched as supersede candidates in dedup_via_radarr.
- Keeper identification chain unchanged: filename-match, then Radarr-history downloadId, then sourceTitle; a movie with no identifiable keeper is still skipped entirely (no relabeling).
- `torrent_is_unregistered` keeps its exact semantics (case-insensitive substring over the UNREGISTERED_MARKERS list; empty/unknown status -> False).
- `should_hard_delete_on_upgrade` unchanged: same-group AND unregistered AND seed obligation met.
- The 525804e orphan guard in `supersede_torrent` (`_torrent_has_local_data`) and the `cleanup_superseded` orphan reaper keep working as before.

## Must contain

- `should_supersede_candidate`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `arr-webhook.py` (the fix) and `test_fixture.py` (adversarial cases only -- never weaken or delete existing ones); do not edit `verify.sh`.
test_fixture.py is the test fixture; it may only GROW with new adversarial cases.

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
