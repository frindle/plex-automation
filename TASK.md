# TASK: arr-webhook-yearly-upgrade-batches-s7-scheduler-gate

## Confirmed defect (observed, not suspected)

`monthly_search_scheduler` (arr-webhook.py, the hourly polling loop starting at
line 3438) still fires on `now.day == 1 and now.month != last_run_month`. The
batched-search slices already landed: `radarr_bulk_search` / `sonarr_bulk_search`
are bounded single-batch passes that persist a per-service `cursor` plus a fresh
`last_run` timestamp through `_save_upgrade_state`, and the interval constant
`UPGRADE_BATCH_INTERVAL_DAYS` (line 237) exists for exactly this gate. But the
scheduler ignores all of it: on the 1st of every month it runs both services'
full cycles regardless of when each last ran, and on any other day it never
checks at all -- so a service whose `last_run` is stale (e.g. after a long
downtime) waits up to a full month, while one that just ran gets re-run the next
1st. Verified by reading the loop body: the only condition in play is the
day/month check; `_load_upgrade_state`, `UPGRADE_BATCH_INTERVAL_DAYS` and
`last_run` are never consulted anywhere inside `monthly_search_scheduler`.

## Entry point

arr-webhook.py:3442 (the `if now.day == 1 and now.month != last_run_month:` line
inside `monthly_search_scheduler`)

## Required change

In arr-webhook.py, replace the 'if now.day == 1 and now.month != last_run_month' condition inside monthly_search_scheduler's hourly polling loop near line 3385 with a per-service interval check: on each hourly poll -- keeping the existing one-check-per-hour cadence -- for each service name in radarr and sonarr, read that entry from _load_upgrade_state and call monthly_upgrade_cycle for that service only if the entry is absent or has no 'last_run' key, or UPGRADE_BATCH_INTERVAL_DAYS days have elapsed since its last_run timestamp; because monthly_upgrade_cycle now calls the batched radarr_bulk_search and sonarr_bulk_search, both the scheduled path and the existing /run-monthly-upgrade manual trigger endpoint near line 5913, which keeps working unchanged, execute exactly one next batch per service rather than a full-catalog sweep.

Behaviour that must NOT change:
- The poll cadence stays `time.sleep(3600)` -- one check per hour, no new sleep.
- Services still run sequentially (radarr before sonarr), never in parallel, so
  the two bulk searches don't stack announces on the same tracker.
- `monthly_upgrade_cycle` itself is untouched: purge -> wait -> batched bulk
  search -> wait -> relabel, with its existing default waits.
- The `/run-monthly-upgrade` endpoint keeps working unchanged: valid service
  (`radarr`, `sonarr`, `both`) returns 200 with the same body; an invalid
  service still returns 400 with the same error message.
- `_load_upgrade_state` / `_save_upgrade_state` and the batched bulk-search
  functions are not modified by this change.

## Must contain

- `for service in ('radarr', 'sonarr'):`
- `UPGRADE_BATCH_INTERVAL_DAYS`
- `'last_run'`
- `monthly_upgrade_cycle(service)`
- `time.sleep(3600)  # check every hour`

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
