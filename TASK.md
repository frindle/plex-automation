# TASK: arr-webhook-yearly-upgrade-batches-s3-state-save

## Confirmed defect (observed, not suspected)

`arr-webhook.py` has `_load_upgrade_state()` (reads `UPGRADE_STATE_PATH`) and
`_save_seed_state()` (writes `SEED_STATE_PATH`), but NO `_save_upgrade_state()`:
the upgrade-batch state can be loaded but never persisted. Verified by grep:
`def _save_upgrade_state` is absent from the file while `_load_upgrade_state`,
`UPGRADE_STATE_PATH` and `_save_seed_state` are all present (lines 135, 1642,
1656).

## Entry point

arr-webhook.py:1656 -- add `_save_upgrade_state` immediately before the existing
`_save_seed_state`, mirroring its exact save-with-exception-handling idiom.

## Required change

In arr-webhook.py, add a function _save_upgrade_state that takes a state dict argument, serializes it to JSON, and writes it to the module-level UPGRADE_STATE_PATH constant (the env-var-configurable path used by this codebase's other sidecars), wrapped in try/except Exception where the except branch only logs via log.warning and never propagates an exception to the caller, matching the exact save-with-exception-handling idiom of _save_seed_state at line 1624.

Behaviour that must NOT change:
- `_load_upgrade_state()` still reads `UPGRADE_STATE_PATH` and returns `{}` on
  missing/invalid file.
- `_save_seed_state(state)` still writes JSON to `SEED_STATE_PATH`, swallows
  write failures with a `log.warning`, and never raises.
- Everything else in the module (routes, schedulers, imports) is untouched; the
  file must still parse and import cleanly.

## Must contain

- `def _save_upgrade_state(state):`
- `open(UPGRADE_STATE_PATH, 'w')`
- `_json.dump(state, f)`
- `except Exception as e:`
- `log.warning(f'[upgrade-batches] failed to persist state: {e}')`

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
