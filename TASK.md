# TASK: arr-webhook-yearly-upgrade-batches-s4-year-sort

## Confirmed defect (observed, not suspected)

`arr-webhook.py` has no way to order release items by release year for the
yearly upgrade batches. Reproduced directly: `python3 -c "import importlib.util as u; s=u.spec_from_file_location('t','arr-webhook.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(hasattr(m,'sort_ids_by_year_desc'))"` prints `False` -- the function does not exist, so any caller of it raises `AttributeError`.

## Entry point

arr-webhook.py:6473 (the new helper is added just above the `if __name__ == '__main__':` block; no existing call site yet)

## Required change

In arr-webhook.py, add a function sort_ids_by_year_desc that takes an items list of dicts and returns the same items sorted by release year in descending order: each item's effective year is its 'year' value when truthy, otherwise the first four characters of its 'firstAired' string parsed as an int when those are digits, otherwise 0; all zero-effective-year items sort LAST (after every real year), ties within the same effective year preserve original input order via a stable sort, without mutating the input list.

Behaviour that must NOT change:
- Everything already in arr-webhook.py keeps working exactly as before -- this is an additive change only; no existing import, function, or class may be deleted or rewritten.
- The module still parses and imports cleanly (verify.sh checks `ast.parse` first).
- Existing helpers such as `_extract_year` are untouched.

## Must contain

- `def sort_ids_by_year_desc(items):`
- `'firstAired'`
- `return sorted(items, key=_key)`

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
