# TASK: arr-webhook-yearly-upgrade-batches-s5-cursor-advance

## Confirmed defect (observed, not suspected)

The yearly-upgrade batched pass in `arr-webhook.py` already defines its pacing
constant (`UPGRADE_BATCH_SIZE`, line 217) but has no cursor-advancing helper:
there is no function that turns a stored cursor plus the current catalog size
into "which indices to search this pass" and "where to resume next pass". A
stale cursor persisted from a larger catalog would index out of bounds on a
smaller one, so the advance must clamp. Verified by grep: `advance_upgrade_cursor`
does not exist anywhere in the file while `UPGRADE_BATCH_SIZE` does.

## Entry point

arr-webhook.py:217 (the `UPGRADE_BATCH_SIZE` constant; add the new function
immediately after it, before any route definitions)

## Required change

In arr-webhook.py, add a pure function advance_upgrade_cursor that takes two int arguments cursor and total and returns a pair: the list of indices to search this pass, and the cursor position for the next pass. It clamps cursor into the range 0 through total-1 when total is positive (so a stale cursor from a larger catalog never indexes out of bounds), collects up to UPGRADE_BATCH_SIZE consecutive indices starting at the clamped cursor wrapping around past the end back to index 0 until either UPGRADE_BATCH_SIZE indices are collected or one full lap is exhausted, and computes next_cursor as (clamped_cursor + number of collected indices) modulo total; when total is zero it returns an empty list with next cursor 0.

Behaviour that must NOT change:
- `UPGRADE_BATCH_SIZE` keeps its existing value/env-var behaviour (`int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))`).
- Every other function, route, and constant in arr-webhook.py is untouched; the module still imports cleanly and parses.
- The new function is pure: no I/O, no globals mutated, same inputs always give the same pair.

## Must contain

- `def advance_upgrade_cursor(cursor, total):`
- `return [], 0`
- `% total`

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
