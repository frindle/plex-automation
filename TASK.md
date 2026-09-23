# TASK: arr-webhook-yearly-upgrade-batches-s2-state-load

## Confirmed defect (observed, not suspected)

The yearly upgrade batch machinery has no way to persist its progress yet:
`grep -n UPGRADE_STATE_PATH arr-webhook.py` returns nothing -- there is no state
path constant and no `_load_upgrade_state` loader in the module. The sibling
state loaders (`_load_seed_state`, `_load_unpack_state`) already exist, so this
slice adds the missing upgrade-batch state load idiom to match them.

## Entry point

arr-webhook.py:1617 (the `_load_seed_state` function whose exact
load-with-exception-handling idiom must be matched)

## Required change

In arr-webhook.py, add a module-level constant UPGRADE_STATE_PATH read from the environment variable UPGRADE_STATE_PATH with default '/data/upgrade_batch_state.json', and a function _load_upgrade_state that opens UPGRADE_STATE_PATH, parses the JSON file using the same json module alias already used by _load_seed_state and _load_unpack_state, and returns the parsed object; on FileNotFoundError or ValueError it returns an empty dict instead of raising, matching the exact load-with-exception-handling idiom of _load_seed_state at line 1617.

Behaviour that must NOT change:
- `_load_seed_state` still loads valid JSON from SEED_STATE_PATH and returns `{}` on missing/corrupt file (regression half).
- All existing imports, constants, functions, and routes in arr-webhook.py remain untouched -- this is an additive slice only.

## Must contain

- `UPGRADE_STATE_PATH = os.environ.get('UPGRADE_STATE_PATH', '/data/upgrade_batch_state.json')`
- `def _load_upgrade_state():`
- `with open(UPGRADE_STATE_PATH) as f:`
- `return _json.load(f)`
- `except (FileNotFoundError, ValueError):`

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
