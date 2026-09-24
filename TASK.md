# TASK: arr-webhook-yearly-upgrade-batches-s10-shared-weekly-quota

## Confirmed defect (observed, not suspected)

The yearly-upgrade throttle is a per-service days-since-last-run gate
(`UPGRADE_BATCH_INTERVAL_DAYS`, checked by `upgrade_batch_due` at line 3431):
radarr and sonarr each get their own full pass on independent schedules, so in
a single week both services can queue unbounded upgrades together. There is no
shared cap on the number of upgrades actually confirmed queued across BOTH
services within a rolling window -- verified by reading `monthly_search_scheduler`
(line 3456), which gates each service independently and never consults any
combined counter, and by confirming no quota/count key exists in the upgrade
state shape persisted by `_save_upgrade_state`.

## Entry point

arr-webhook.py:217 (`UPGRADE_BATCH_SIZE`, where `WEEKLY_UPGRADE_QUOTA` lands)
and arr-webhook.py:1655 (`_save_upgrade_state`, next to which the helpers land).

## Required change

In arr-webhook.py, add a new env-var-configurable module-level int constant WEEKLY_UPGRADE_QUOTA read from the environment variable WEEKLY_UPGRADE_QUOTA with default '10' (same os.environ.get + int() idiom as UPGRADE_BATCH_SIZE at line 217), placed immediately after it -- this replaces UPGRADE_BATCH_INTERVAL_DAYS as the throttle: it is a SHARED cap across BOTH radarr and sonarr combined on the number of upgrades actually confirmed queued (per s9-relabel-count's return value) within a rolling 7-day window, not a per-service days-since-last-run gate. Add a helper function `weekly_quota_state(state, now=None)` that reads a top-level 'quota' key in the state dict (shape: {'count': int, 'week_start': <ISO-8601 string>}), defaulting to {'count': 0, 'week_start': <now>} when absent; if now minus the parsed week_start is >= 7 days (or week_start is unparseable), the window has expired and the function returns a FRESH {'count': 0, 'week_start': <now, ISO-8601>} dict (does not mutate the passed-in state); otherwise it returns the existing entry unchanged. Add a second helper `record_upgrades_found(state, n)` that takes the current (already-reset-if-needed) quota entry, adds n to its 'count', and writes it back into state['quota'], then calls _save_upgrade_state(state); it does not enforce the cap itself -- callers check remaining capacity via WEEKLY_UPGRADE_QUOTA - quota_entry['count'] before deciding whether to run a pass. now defaults to a naive-UTC datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) clock, matching the convention already used by upgrade_batch_due.

Behaviour that must NOT change:
- `UPGRADE_BATCH_SIZE`, `UPGRADE_BATCH_INTERVAL_DAYS`, and every existing
  constant keep their values and env-var names; `advance_upgrade_cursor` and
  `upgrade_batch_due` behave exactly as before (the interval gate still works
  for callers that use it).
- `_save_upgrade_state` / `_load_upgrade_state` semantics are unchanged: the
  new helpers persist through `_save_upgrade_state`, which already swallows
  persistence errors with a warning.
- `weekly_quota_state` never mutates the state dict passed in (a fresh window
  is returned as a NEW dict; an existing valid entry is returned as-is).
- The 'quota' key shape stays exactly {'count': int, 'week_start': ISO-8601
  string} so it round-trips through JSON persistence.

## Must contain

- `WEEKLY_UPGRADE_QUOTA = int(os.environ.get('WEEKLY_UPGRADE_QUOTA', '10'))`
- `def weekly_quota_state(state, now=None):`
- `def record_upgrades_found(state, n):`
- `'week_start'`
- `_save_upgrade_state(state)`

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
