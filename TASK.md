# TASK: asian-tv-router

## Confirmed defect (observed, not suspected)

confirmed: new Korean/Japanese/Chinese series added via Sonarr all land in the
default /data/Media/TV Shows root; nothing routes foreign-language series to the
separate /data/Media/TV Shows - Asian library (Sonarr rootfolder id=3 + Plex
'Asian TV' library exist and were built this session, but nothing moves series
there). Verified: the Asian root currently holds only manually-added series.

## Entry point

arr-webhook.py:3355 (the `sonarr_webhook()` handler). Two change sites:
1. A new module-level function `route_series_to_asian(data)` (add it directly
   above the `@app.route('/webhook/sonarr', ...)` decorator on line 3355).
2. A new `elif event == 'SeriesAdd':` branch inside `sonarr_webhook()` that
   calls it, alongside the existing `Grab`/`Download` branches.

## Required change

`route_series_to_asian(data)` -> str | None:
- Read the series id from `data['series']['id']`. If absent, return None (no HTTP).
- GET `{SONARR_URL}/api/v3/series/{id}` with header `X-Api-Key: SONARR_API_KEY`.
- Read the language from `series['originalLanguage']['name']` (case-insensitive).
  If it is NOT one of korean/japanese/chinese/cantonese/mandarin, return None.
- If `series['rootFolderPath']` is ALREADY the Asian root, return None (no move).
- Otherwise PUT `{SONARR_URL}/api/v3/series/editor` with json
  `{'seriesIds': [id], 'rootFolderPath': '/data/Media/TV Shows - Asian', 'moveFiles': True}`
  and return the Asian root string `/data/Media/TV Shows - Asian`.
- Any exception -> log and return None (never raise into the webhook thread).

Wire it into `sonarr_webhook()` as `elif event == 'SeriesAdd':` running the
call in a daemon thread, matching the existing branch style.

Behaviour that must NOT change:
- English / non-CJK series are NEVER moved (return None, no PUT).
- A CJK series already sitting in the Asian root is NEVER moved again (no PUT).
- A payload missing `originalLanguage` or `series.id` must NOT raise.
- The existing `Grab` and `Download` branches of `sonarr_webhook()` are untouched.

## Must contain

- `route_series_to_asian`
- `SeriesAdd`
- `originalLanguage`
- `series/editor`
- `moveFiles`
- `TV Shows - Asian`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

## Scope

Only edit `arr-webhook.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
