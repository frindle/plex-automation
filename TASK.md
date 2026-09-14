# Task: run handle_upgrade_import off the request thread + make it idempotent per downloadId

## The defect, CONFIRMED
Read directly in `arr-webhook.py`. In `radarr_webhook` (line 3426) and `sonarr_webhook`
(line 3487), the `Download`+isUpgrade branch calls `handle_upgrade_import(data, 'Radarr')` /
`handle_upgrade_import(data, 'Sonarr')` INLINE — blocking the HTTP response through the entire
Deluge login → get_all_torrents → supersede loop → purge cycle. The neighbouring handlers
(`handle_grab`, `handle_import_relabel`, `refresh_metadata_on_grab`) are all dispatched to
`threading.Thread(..., daemon=True).start()`. Because the upgrade handler blocks, a slow Deluge
makes the webhook exceed Radarr/Sonarr's timeout; *arr marks it failed and RETRIES the same
`Download` event, and the second run re-enters the supersede + post-import purge. There is no
dedupe on downloadId, so the purge can act twice.

## The property to satisfy
1. Both webhook sites dispatch `handle_upgrade_import` to a daemon thread (return 200 at once),
   exactly like the `handle_import_relabel` calls beside them.
2. `handle_upgrade_import` is idempotent per `downloadId`: a second invocation with a downloadId
   already processed returns immediately without re-running its body. Use a module-level set
   guarded by a lock. A blank/absent downloadId is NOT deduped (each still runs).

## Entry points (edit ONLY `arr-webhook.py`)
Only edit `arr-webhook.py`.
- Add module-level state near the other globals (right after `session = requests.Session()`):
  `_recent_upgrade_download_ids` (a set) and `_upgrade_dedupe_lock` (a `threading.Lock()`).
- `arr-webhook.py:2519` — at the very TOP of `def handle_upgrade_import(data, source):`, before the
  `if source == 'Sonarr':` branch, add the dedupe guard.
- `arr-webhook.py:3426` and `arr-webhook.py:3487` — replace the inline call with a daemon Thread.
Do not edit `verify.sh` or `fixture_upgrade.py`.

## Exactly what to change
1. After `session = requests.Session()` add:
    _recent_upgrade_download_ids = set()
    _upgrade_dedupe_lock = threading.Lock()
2. As the FIRST statements inside `handle_upgrade_import`:
    _dedup_id = (data.get('downloadId') or '').lower()
    if _dedup_id:
        with _upgrade_dedupe_lock:
            if _dedup_id in _recent_upgrade_download_ids:
                log.info(f'{source}: duplicate upgrade-import for {_dedup_id}, skipping')
                return
            _recent_upgrade_download_ids.add(_dedup_id)
3. At line 3426 replace `handle_upgrade_import(data, 'Radarr')` with
   `threading.Thread(target=handle_upgrade_import, args=(data, 'Radarr'), daemon=True).start()`
   and at line 3487 the same for `'Sonarr'`.

## Must contain
- `_recent_upgrade_download_ids`
- `threading.Thread(target=handle_upgrade_import`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints `VERIFY_OK`.
Only edit `arr-webhook.py`; do not edit `verify.sh`.
