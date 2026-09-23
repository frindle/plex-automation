# TASK: arr-webhook-yearly-upgrade-batches-s1-batch-constants

## Confirmed defect (observed, not suspected)

`arr-webhook.py` has no module-level constants for the yearly upgrade batched
pass. `python3 -c "import ast; t=ast.parse(open('arr-webhook.py').read()); print([n.targets[0].id for n in ast.walk(t) if isinstance(n, ast.Assign)])"`
shows the constant group at lines 212-217 (`BULK_SEARCH_BATCH`, `BULK_SEARCH_DELAY`,
`SONARR_BULK_SEARCH_BATCH`) but no `UPGRADE_BATCH_SIZE` or
`UPGRADE_BATCH_INTERVAL_DAYS` anywhere in the module -- so downstream slices of
the yearly-upgrade-batches feature have nothing to read their batch size and
pass interval from.

## Entry point

arr-webhook.py:213 (immediately after the `BULK_SEARCH_DELAY` line, inside the
existing bulk-search pacing constant group)

## Required change

In arr-webhook.py, add two module-level int constants immediately after the existing BULK_SEARCH_BATCH and BULK_SEARCH_DELAY constant group (around line 213): UPGRADE_BATCH_SIZE read from the environment variable UPGRADE_BATCH_SIZE with default '12' and coerced to int, representing the number of movies or series searched per batched pass; and UPGRADE_BATCH_INTERVAL_DAYS read from the environment variable UPGRADE_BATCH_INTERVAL_DAYS with default '3' and coerced to int, representing the minimum whole days between passes for a given service. Both follow the exact os.environ.get idiom already used by BULK_SEARCH_BATCH on line 212.

Behaviour that must NOT change:
- `BULK_SEARCH_BATCH` still defaults to 50 and `BULK_SEARCH_DELAY` still
  defaults to 180 (the existing bulk-search pacing constants keep their values).
- The module still parses and imports cleanly; no other constant, function or
  route changes.

## Must contain

- `UPGRADE_BATCH_SIZE = int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))`
- `UPGRADE_BATCH_INTERVAL_DAYS = int(os.environ.get('UPGRADE_BATCH_INTERVAL_DAYS', '3'))`

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
