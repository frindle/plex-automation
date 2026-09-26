# TASK: arr-webhook-recent-upgrade-priority-s1b-recent-year-window

## Confirmed defect (observed, not suspected)

The landed `_is_recent_year` helper in `arr-webhook.py` misclassifies release
years three ways. Reproduced directly against the committed code with a pinned
clock (`now.year = 2026`):

- `_is_recent_year(2024)` returns **True** -- two years before now must NOT be
  recent (the parent intent is strictly "current year or immediately preceding
  year"). Cause: `RECENT_YEAR_WINDOW = 3`.
- `_is_recent_year(2027)` returns **False** -- a next-year/pre-release title is
  the MOST recent thing in an *arr catalog and must be recent. Cause: the check
  is bounded on both sides (`now.year - W <= y <= now.year`).
- With no injected clock, `datetime.now()` (naive local time) is used while
  every other clock read in this module uses `datetime.now(timezone.utc)`.

## Entry point

arr-webhook.py:6739 -- the `RECENT_YEAR_WINDOW = 3` constant and the
`_is_recent_year(year, now=None)` helper immediately below it (lines 6739-6749).

## Required change

In arr-webhook.py, CORRECT the existing `_is_recent_year(year, now=None)` helper (landed by slice s1-is-recent-year) so it matches the parent intent for this feature. The landed version is WRONG in three ways and is already committed on the chain:
  (1) it uses `RECENT_YEAR_WINDOW = 3`, so a release from THREE years ago counts as 'recent'. The parent intent is strictly 'the current year or the immediately preceding year' -- a one-year lookback. Change the window to 1 (`RECENT_YEAR_WINDOW = 1`).
  (2) it is bounded on BOTH sides (`now.year - W <= y <= now.year`), so a FUTURE year is rejected. *arr catalogs routinely carry a next-year `year` for announced/pre-release titles, and those are the MOST recent things there are. The contract is `int(year) >= now.year - RECENT_YEAR_WINDOW` with NO upper bound.
  (3) `now` defaults to naive `datetime.now()` (local time). It must default to `datetime.now(timezone.utc)`, matching every other clock read in this module.
Contract after the change: returns a plain bool; True iff `year` is an int-or-int-like value and `int(year) >= now.year - RECENT_YEAR_WINDOW`; `None`, empty string, 0 and non-numeric garbage all return False WITHOUT raising; `now` still accepts an injected clock (any object with `.year`) so tests can pin the date. Nothing else in the module changes.
ADVERSARIAL CASES THE FIXTURE MUST CARRY (the landed helper fails these -- that is the point): a year two years before now.year must be NOT recent (the landed window=3 says recent); a year one year AFTER now.year must be recent (the landed upper bound says not); now.year and now.year - 1 are recent; now.year - 2 is not.

Behaviour that must NOT change:
- `None`, empty string, `0` and non-numeric garbage all return `False` without raising -- the `int(str(year).strip())` coercion with its `(TypeError, ValueError)` guard stays intact.
- An injected clock (any object exposing `.year`) still works when passed as `now`.
- The helper returns a plain bool in every case.
- Nothing else in the module changes: no other constant, function, route or import is touched; the file must still parse and import cleanly.

## Must contain

- `RECENT_YEAR_WINDOW = 1`
- `def _is_recent_year(year, now=None):`
- `datetime.now(timezone.utc)`
- `return y >= now.year - RECENT_YEAR_WINDOW`

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
