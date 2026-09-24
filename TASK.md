# TASK: arr-webhook-yearly-upgrade-batches-s11-scheduler-shared-quota

## Confirmed defect (observed, not suspected)

`monthly_search_scheduler` (arr-webhook.py, ~line 3490) still gates each service
independently with `upgrade_batch_due(entry, now)` / `UPGRADE_BATCH_INTERVAL_DAYS`,
so radarr and sonarr can both fire in the same poll and nothing bounds their
combined upgrade volume. Meanwhile the shared weekly quota helpers already landed
(`WEEKLY_UPGRADE_QUOTA` at line 218, `weekly_quota_state` at line 1663,
`record_upgrades_found` at line 1688) are defined but NEVER called from the
scheduler loop or from `monthly_upgrade_cycle` -- so even when upgrades ARE
confirmed queued by a relabel pass, the shared quota count never advances.
Verified by reading the current source: the scheduler's only gate is
`upgrade_batch_due`, and `relabel()`'s integer return value (both relabelers
return an int) is discarded in `monthly_upgrade_cycle`.

## Entry point

arr-webhook.py:3490 (`def monthly_search_scheduler():`) and arr-webhook.py:3518
(`def monthly_upgrade_cycle(...)`); the manual endpoint at ~line 6052
(`/run-monthly-upgrade`) goes through `monthly_upgrade_cycle` too.

## Required change

In arr-webhook.py, replace monthly_search_scheduler's per-service upgrade_batch_due(entry, now) gate (and the now-unused UPGRADE_BATCH_INTERVAL_DAYS/upgrade_batch_due call site -- leave the constant and function defined for backward compat but stop calling upgrade_batch_due from the scheduler loop) with a shared-quota gate: on each hourly poll, compute the current quota entry via weekly_quota_state(state, now); if WEEKLY_UPGRADE_QUOTA - quota_entry['count'] <= 0, log that the weekly quota is exhausted and skip this poll entirely (no service runs). Otherwise, alternate which service gets this poll's turn using a persisted top-level state key 'next_service' (values 'radarr' or 'sonarr', defaulting to 'radarr' when absent): run monthly_upgrade_cycle for that service, then flip 'next_service' to the other value and _save_upgrade_state(state) before the next hourly sleep. monthly_upgrade_cycle must be changed to capture the integer return value of its relabel() call (from s9-relabel-count) and call record_upgrades_found(state, that_count) after the relabel step completes, so the shared quota only advances on CONFIRMED queued upgrades, never on searches merely attempted. The existing manual /run-monthly-upgrade endpoint (around line 5913) must keep working and must also route its relabel count through record_upgrades_found the same way. Keep the existing time.sleep(3600) hourly cadence and the existing purge/wait-before-search/wait-before-relabel structure inside monthly_upgrade_cycle unchanged.

Behaviour that must NOT change:
- `upgrade_batch_due` and `UPGRADE_BATCH_INTERVAL_DAYS` remain defined (backward compat); only their call site in the scheduler loop is removed.
- The scheduler still polls hourly (`time.sleep(3600)` at the end of each iteration) and still runs exactly one service per poll when quota remains.
- `monthly_upgrade_cycle` keeps its purge → wait-before-search → bulk search → wait-before-relabel → relabel structure, including the default waits (1800/300).
- The manual `/run-monthly-upgrade` endpoint keeps its exact contract: 200 with `{'ok': True, 'service': ..., 'skip_waits': ...}` on valid input; 400 with `{'ok': False, 'error': ...}` for an invalid service value.
- `record_upgrades_found` is only ever called with the integer count returned by a completed relabel step -- never from bulk search or purge paths.

## Must contain

- `weekly_quota_state(state, now)`
- `WEEKLY_UPGRADE_QUOTA - quota_entry['count'] <= 0`
- `'next_service'`
- `state.get('next_service') or 'radarr'`
- `record_upgrades_found(state, count)`
- `time.sleep(3600)  # check every hour`

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
