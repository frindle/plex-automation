# TASK: arr-webhook-yearly-upgrade-batches-s8-tests

## Confirmed defect (observed, not suspected)

The yearly-upgrade batch machinery in `arr-webhook.py` (`sort_ids_by_year_desc`,
`advance_upgrade_cursor`, the interval gate inside `monthly_search_scheduler`,
and `POST /run-monthly-upgrade`) has NO test coverage: there is no
`test_upgrade_batches.py` alongside the other `test_*.py` files, so a regression
in cursor wraparound, stale-cursor clamping, year/firstAired ordering, or the
interval gate would ship silently. Reproduced by listing the repo root: every
other behaviour (radarr upgrade label, grab dedupe, queue dupes, ...) has a
dedicated test file; the upgrade-batch slice does not.

## Entry point

`arr-webhook.py`: `sort_ids_by_year_desc` (~line 6545), `advance_upgrade_cursor`
(~line 220), the interval gate in `monthly_search_scheduler` (~line 3431), and
the `/run-monthly-upgrade` route (~line 5997).

## Required change

Create test_upgrade_batches.py in the repo root (alongside the existing test_*.py files) with pytest tests covering: sort_ids_by_year_desc ordering including missing-year and firstAired fallback tie-breaking; advance_upgrade_cursor advancement and wraparound across multiple simulated passes plus stale-cursor clamping when total shrinks; the interval gate behavior where a service does not re-trigger before UPGRADE_BATCH_INTERVAL_DAYS have elapsed since its last_run but does trigger after, and triggers on the first-ever poll with empty state; and that POST /run-monthly-upgrade still returns {'ok': True} and triggers only one next batch per requested service rather than a full-catalog sweep, using the same mocking and fixture idioms as existing tests like test_radarr_upgrade_label.py.

To make the interval gate directly testable (it currently lives inline in
`monthly_search_scheduler`'s loop body), extract it into a module-level helper
`upgrade_batch_due(entry, now=None)` with identical semantics: returns True when
the entry has no `last_run` stamp (first-ever poll / empty state) or when at
least `UPGRADE_BATCH_INTERVAL_DAYS` have elapsed since `last_run`; an unparseable
stamp is treated as due. `monthly_search_scheduler` must call this helper
instead of inlining the check, so both paths share one implementation.

Behaviour that must NOT change:
- `sort_ids_by_year_desc`: newest effective year first; effective year is
  `year` when truthy else the first four digits of `firstAired`; no usable year
  sorts last; ties keep input order (stable); input list not mutated.
- `advance_upgrade_cursor(cursor, total)`: up to `UPGRADE_BATCH_SIZE` consecutive
  indices from the clamped cursor, wrapping past the end back to 0; stale cursor
  clamps into range when total shrinks; `total <= 0` returns `([], 0)`.
- Interval gate: not due before `UPGRADE_BATCH_INTERVAL_DAYS` elapse since
  `last_run`; due at/after that boundary; first-ever poll (empty state) is due.
- `POST /run-monthly-upgrade`: still returns `{'ok': True, ...}` with the
  requested service echoed back; invalid `?service=` is a clean 400 with an
  error body (not a 500); one bounded batch pass per requested service only.

## Must contain

- `upgrade_batch_due`
- `UPGRADE_BATCH_INTERVAL_DAYS`
- `/run-monthly-upgrade`
- in test_upgrade_batches.py: `sort_ids_by_year_desc`
- in test_upgrade_batches.py: `advance_upgrade_cursor`
- in test_upgrade_batches.py: `test_client()`

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
This is not about adding bogus assertions for constants; it's about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
