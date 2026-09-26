# TASK: arr-webhook-recent-upgrade-priority-s1-is-recent-year

## Confirmed defect (observed, not suspected)

The yearly upgrade-priority work needs a shared notion of "recent release
year", but `arr-webhook.py` has no such helper -- year comparisons are ad-hoc
(`abs(t_year - movie_year) > 1`, `_extract_year`, etc.) and there is no single
place that decides whether a catalog entry's year is recent relative to the
current date. Verified: `grep -n "_is_recent_year" arr-webhook.py` returns
nothing, so any code path that needs "is this release from within the last few
years?" cannot exist yet.

## Entry point

arr-webhook.py -- add the helper in the yearly-upgrade area of the file, next
to `sort_ids_by_year_desc` (near the end of the module).

## Required change

In arr-webhook.py: def _is_recent_year(year, now=None) -> bool.

Contract:
- Returns a plain `bool`.
- `year` is the release year from *arr catalog data -- treat it as untrusted:
  int, numeric string, `None`, empty string, or garbage must all be handled
  WITHOUT raising; anything that is not an integer year returns `False`.
- `now` defaults to `datetime.now()` but MUST accept an injected clock (any
  object with a `.year`) so callers and tests can pin the date.
- A year is "recent" iff it falls within a small whole-year window of `now`:
  from `now.year - WINDOW` up to and including `now.year`. The exact boundary
  (`now.year - WINDOW`) IS recent; one year older than that is NOT. Future
  years are NOT recent (the check must be bounded on both sides).
- Declare the window as a module-level constant `RECENT_YEAR_WINDOW = 3` so it
  can be tuned without touching the helper, and have `_is_recent_year` use it.

Behaviour that must NOT change:
- Everything already in arr-webhook.py keeps working -- this is an additive
  change; do not delete or rewrite existing imports/functions/routes (e.g.
  `sort_ids_by_year_desc`, `_extract_year`, the Flask routes).
- The module still parses and imports cleanly (`python3 -c "import ast; ast.parse(open('arr-webhook.py').read())"`).

## Must contain

- `def _is_recent_year(year, now=None):`
- `RECENT_YEAR_WINDOW = 3`

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
