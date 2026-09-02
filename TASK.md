# TASK: radarr-sourcetitle-fallback

## Confirmed defect (observed, not suspected)

confirmed in code + live log: dedup_via_radarr lacks the sourceTitle fallback dedup_via_sonarr got in fix #2. When a movie's newest Radarr import has a blank downloadId, _radarr_last_imported_download_id (arr-webhook.py:907) returns None, keepers stays empty, and the movie is skipped forever ('no keeper identified' — live for movie 955 Now You See Me 2, 2 torrents matched).

## Entry point

arr-webhook.py:1004

## Required change

A Radarr movie whose newest import has a blank downloadId is resolved by matching
that import's sourceTitle to exactly ONE candidate torrent name.

Implement with these EXACT names (the test asserts them):
1. Add a helper named exactly `_radarr_last_imported_source_title(movie_id)` — a
   mirror of `_radarr_last_imported_download_id` (arr-webhook.py:907) that returns
   the newest DownloadFolderImported event's `sourceTitle` (a str) REGARDLESS of a
   blank downloadId, or None (no events / no usable sourceTitle / on any error).
2. In `dedup_via_radarr`, AFTER the filename-match and the downloadId-history
   fallback both leave `keepers` empty and BEFORE the "no keeper identified" skip,
   call the EXISTING release-name-generic `select_episode_keeper_by_source_title`
   with (matched_hashes, {hash: torrent release name}, the sourceTitle from step 1).
   If it returns a hash (non-None), that hash is the keeper. It returns None on 0
   or >1 name matches (fall back, never guess), so a genuinely-unresolvable movie
   still skips.

Behaviour that must NOT change:
- The existing keeper resolution is unchanged: filename-match first, then the
  Radarr-history downloadId fallback (_radarr_last_imported_download_id). The
  sourceTitle fallback is a THIRD path that fires ONLY when both of those leave
  `keepers` empty — it must not override a keeper the earlier paths already found.
- A movie with 2+ matched torrents and NO resolvable keeper (no filename match,
  no downloadId history, no sourceTitle match) still skips and relabels nothing —
  never invent a keeper to force a relabel.
- The reused select_episode_keeper_by_source_title still returns exactly one hash
  on a single match and None on 0 or >1 (it must not start guessing for movies).

## Must contain

- `_radarr_last_imported_source_title`
- `select_episode_keeper_by_source_title`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

## Scope

Only edit `arr-webhook.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
