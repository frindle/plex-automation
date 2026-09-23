# TASK: arr-webhook-yearly-upgrade-batches-s9-relabel-count

## Confirmed defect (observed, not suspected)

Confirmed by direct read of arr-webhook.py (grep for `return` inside each
function's body): relabel_radarr_upgrades (def at line 3146) has exactly two
bare `return` statements (the "no torrents" guard and the "no radarr-labeled
torrents" guard) and no return statement at all at the end of its try block or
in its except block -- every code path falls through to an implicit `return
None`, even the path that DID relabel torrents and computed a real int count
into the local `relabeled` variable, which is then discarded. relabel_sonarr_
upgrades (def at line 3250) has the identical shape: two early bare `return`s,
no return in the except block, and the busy path computes `relabeled_hashes`
but never returns its length. Neither caller (monthly_upgrade_cycle's
`relabel()` call, nor the /run-monthly-upgrade manual-trigger route) can
currently observe how many upgrades were actually confirmed queued this pass
-- a later slice needs that count to gate a shared weekly quota, and today
there is no way to get it without re-deriving it.

## Entry point

arr-webhook.py:3146 (relabel_radarr_upgrades) and arr-webhook.py:3250
(relabel_sonarr_upgrades)

## Required change

In arr-webhook.py, change relabel_radarr_upgrades (around line 3141) and relabel_sonarr_upgrades (around line 3250) so each RETURNS the integer count of torrents it actually relabeled as an upgrade this call, instead of implicitly returning None. relabel_radarr_upgrades already accumulates this in its local `relabeled` int -- return it at the end of the try block, and return 0 from every early `return` inside the function (the 'no torrents' and 'no radarr-labeled torrents' guards) and from the except block. relabel_sonarr_upgrades tracks this as `len(relabeled_hashes)` -- compute and return that same way: 0 from every early return and the except block, `len(relabeled_hashes)` at the end of the try block. Do not change any other behavior, logging, or the existing Deluge/API calls in either function. A relabeled torrent means the arr app found the item already has a file after a search pass -- these are the only place in the codebase that observes a real, confirmed 'an upgrade was found and queued' event, and a later slice will consume this return value to gate a shared weekly quota.

Behaviour that must NOT change:
- All existing logging messages, their exact text and call sites, are unchanged.
- The Deluge/Radarr/Sonarr API calls, their order, and their arguments are unchanged.
- The `relabeled_hashes` / `core.queue_bottom` move-to-bottom behavior is unchanged.
- An exception caught by the existing `except Exception as e: log.error(...)` block
  still logs the same error message; it just also now returns 0 instead of None.

## Must contain

- `return relabeled`
- `return len(relabeled_hashes)`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

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
