# TASK: arr-webhook-yearly-upgrade-batches-s6-batched-search

## Confirmed defect (observed, not suspected)

`radarr_bulk_search()` and `sonarr_bulk_search()` chunk the ENTIRE monitored
catalog into batches (`BULK_SEARCH_BATCH` / `SONARR_BULK_SEARCH_BATCH`) and fire
every batch in one run. Verified by reading arr-webhook.py: radarr builds
`batches = [movie_ids[n:n + BULK_SEARCH_BATCH] for n in range(0, len(movie_ids),
BULK_SEARCH_BATCH)]` over all monitored ids and loops them; sonarr does the same
with `SONARR_BULK_SEARCH_BATCH`. One full-catalog pass makes Radarr/Sonarr push
every accepted release to Deluge back-to-back, which trips private-tracker
announce rate limits. The bounded-pass machinery already exists in this file
(`UPGRADE_BATCH_SIZE`, `advance_upgrade_cursor()`, `_load_upgrade_state()` /
`_save_upgrade_state()`, `sort_ids_by_year_desc()`) but the two bulk-search
functions do not use it.

## Entry point

arr-webhook.py:3098 (`radarr_bulk_search`) and arr-webhook.py:3190
(`sonarr_bulk_search`).

## Required change

In arr-webhook.py, change radarr_bulk_search and sonarr_bulk_search so that
instead of building batches over the ENTIRE monitored catalog (the current
pattern at lines 3074 and 3177 that chunks all ids by BULK_SEARCH_BATCH), each
function: reads its per-service entry from _load_upgrade_state -- a dict keyed
by service name 'radarr' or 'sonarr', each entry holding 'cursor' and
'last_run' keys; sorts the monitored IDs with sort_ids_by_year_desc; computes
this pass's index list and next cursor via advance_upgrade_cursor using the
stored cursor (defaulting to 0 when absent) and the sorted list length; issues
the existing MoviesSearch or SeriesSearch API calls only for that index list,
reusing the existing per-item search call logic rather than duplicating it;
then persists an entry with 'cursor' set to the next cursor and 'last_run' set
to a fresh ISO-8601 timestamp back through _save_upgrade_state -- so each pass
searches at most UPGRADE_BATCH_SIZE items and advances the persisted cursor,
wrapping to index 0 after the end.

Contract details:
- `radarr_bulk_search()`: fetch monitored movies from `{RADARR_URL}/api/v3/movie`,
  keep only entries with truthy `monitored`; if none, log a warning and return
  without posting any command or touching state. Otherwise sort the monitored
  movie dicts newest-year-first via `sort_ids_by_year_desc` (it reads each item's
  `year` / `firstAired`, so pass the dicts, not bare ids), take their `id`s in
  that order, compute `(indices, next_cursor) = advance_upgrade_cursor(cursor, len(ids))`
  where cursor is `_load_upgrade_state().get('radarr', {}).get('cursor', 0)` (an
  absent or non-dict entry must default to 0), and post ONE `MoviesSearch`
  command with `'movieIds': [ids[i] for i in indices]`. Then persist
  `state['radarr'] = {'cursor': next_cursor, 'last_run': <fresh ISO-8601 timestamp>}`
  via `_save_upgrade_state(state)`.
- `sonarr_bulk_search()`: same shape over `{SONARR_URL}/api/v3/series` with the
  `'sonarr'` state key; Sonarr's SeriesSearch takes a single seriesId, so issue
  one command per id in this pass's slice (the existing per-item call), each
  `{'name': 'SeriesSearch', 'seriesId': <id>}`.
- A synchronous route `POST /run-bulk-search?service=radarr|sonarr` runs exactly
  one pass inline and returns JSON with the service, the cursor before/after,
  and the new last_run; an unknown service value is a 400 with
  `{'ok': False, 'error': 'service must be radarr or sonarr'}`.

Behaviour that must NOT change:
- Unmonitored items are still excluded from searches.
- The per-item API call shapes stay identical: Radarr posts one `MoviesSearch`
  command with a `movieIds` list; Sonarr posts one `SeriesSearch` command per
  series id.
- Errors from the catalog GET or the search POST are logged, not raised (the
  functions keep their existing try/except-and-log behaviour).
- All other routes/functions in arr-webhook.py (`relabel_radarr_upgrades`,
  `monthly_upgrade_cycle`, `/run-monthly-upgrade`, etc.) are untouched.

## Must contain

- `def radarr_bulk_search():`
- `def sonarr_bulk_search():`
- `_load_upgrade_state()`
- `_save_upgrade_state(state)`
- `sort_ids_by_year_desc(monitored)`
- `advance_upgrade_cursor(entry.get('cursor', 0), len(movie_ids))`
- `advance_upgrade_cursor(entry.get('cursor', 0), len(series_ids))`
- `'name': 'MoviesSearch'`
- `'name': 'SeriesSearch'`
- `state['radarr'] = {'cursor': next_cursor, 'last_run': datetime.now(timezone.utc).isoformat()}`
- `state['sonarr'] = {'cursor': next_cursor, 'last_run': datetime.now(timezone.utc).isoformat()}`
- `@app.route('/run-bulk-search', methods=['POST'])`
- `'service must be radarr or sonarr'`

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
