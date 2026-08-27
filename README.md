# plex-automation

Webhook-driven automation for Sonarr/Radarr + Deluge, deployed as a Docker container on Unraid.

## What it does

`arr-webhook.py` runs a Flask app (port `9876`) that Sonarr and Radarr call via their notification webhooks:

- **On Grab**: if the grab is a quality upgrade and the release is over 10GB, labels the new torrent in Deluge so it can be throttled/tracked separately instead of competing with normal downloads.
- **On Download (import) of an upgrade**: finds the old torrent that the new file replaced and either deletes it immediately (PROPER/REPACK) or labels it `superseded` and moves it to a seeding directory.

Background schedulers (run inside the same process):
- **Daily**: removes `superseded` torrents that have seeded past `SEED_DAYS` (default 21), and dedupes Radarr's and Sonarr's downloads (Radarr: highest custom-format-score entry per movie; Sonarr: best covering release per overlapping episode set — both across the *arr's own queue and the throttled `radarr-upgrade`/`sonarr-upgrade` lane, which that queue cannot see).
- **Hourly**: bumps normal `sonarr`/`radarr`-labeled torrents to the top of the Deluge queue.
- **Monthly (1st of month)**: purges stalled upgrade torrents, triggers a Radarr bulk search to catch missed upgrades, waits, then relabels/requeues anything that came in as a result.

`monthly_upgrade.py` is a standalone script duplicating the monthly cycle (purge → bulk search → wait 90 min → relabel/requeue), kept for manual or cron-triggered runs independent of the long-running webhook process.

`media_share.py` adds a friend-facing media upload portal (Flask blueprint, registered into the same app/port) at `/share`:
- Friends authenticate via Cloudflare Access (the app trusts the `Cf-Access-Authenticated-User-Email` header, so it must only be reachable through the Cloudflare Tunnel — never expose this port directly to the internet).
- Browse the read-only-mounted Movies/TV Shows/Music libraries and upload a file or whole folder to that friend's own SFTP server, looked up by their authenticated email in `FRIENDS_CONFIG`.
- Uploads run in a background thread, throttled to `UPLOAD_RATE_LIMIT_MBIT` (default 5 Mbit/s) so a large upload doesn't saturate the connection.
- Every upload is logged to a SQLite DB (`/data/share_uploads.db`); `/share/usage` shows each friend their own bandwidth usage over the last 7/30/60/90/182/365 days.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `DELUGE_PASSWORD`
   - `SONARR_API_KEY`
   - `RADARR_API_KEY`
   - `FRIENDS_CONFIG` (JSON map of Cloudflare Access email → SFTP destination) if using the media share portal
2. Adjust the hardcoded Deluge/Sonarr/Radarr URLs, labels, `SEED_DAYS`, `SEEDING_DIR`, and the `/mnt/Media/...` library volume mounts directly in `docker-compose.yml` if your setup differs.
3. Build and run:
   ```
   docker-compose build && docker-compose up -d
   ```
4. Point Sonarr/Radarr's webhook connections (Settings → Connect) at `http://<container-ip>:9876/webhook/sonarr` and `/webhook/radarr`, enabling the **On Grab** and **On Import** (upgrade) triggers.
5. If using the media share portal, point a Cloudflare Tunnel + Access application at `http://<container-ip>:9876/share` and restrict ingress so the container is only reachable through the tunnel.

## Deployment

Runs on Unraid at `/mnt/user/appdata/plex-automation`, on the `br0` macvlan network (external) at a static IP. `.env` lives only on the server and is gitignored — never commit real secrets.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR to `main`: syntax + `ruff` lint check on all Python scripts, then a Docker build to make sure the image actually builds.

## Changelog

### Unreleased
- Add the Sonarr counterpart of the queue dupe pass, which never existed: `cleanup_sonarr_queue_dupes()`. Sonarr has the identical blind spot (`Deluge.GetItems()` → `GetTorrentsByLabel(TvCategory)`), so a throttled `sonarr-upgrade` torrent leaves `/api/v3/queue` the moment `handle_grab()` relabels it — and unlike Radarr, nothing was policing that population from either side. Candidate set is the Sonarr queue plus the in-flight throttled lane, identity for lane torrents coming from `/api/v3/history?downloadId=`. Identity is where the two services differ: a movie is one `movieId`, but a TV grab covers a *set* of episodes (single episode, multi-episode file, or season pack), so `seriesId` is not an identity — two different episodes of one series are not duplicates. Sonarr also emits one queue record *per episode*, so a season pack's records are folded back into a single candidate before anything is compared. A candidate is dropped only when its episode set is a **subset** of a keeper's, so the keeper delivers everything the loser would; keepers are chosen by coverage first (dropping a 10-episode pack to keep one higher-scoring episode inside it would leave nine episodes with nothing in flight), then custom-format score, then bytes already pulled. Straddling packs (`S01E01-05` vs `S01E04-10`) are both kept. Same safety properties as the Radarr pass: finished/seeding torrents are never touched, queue-record losers go through the queue API with `blocklist: False` (bulk endpoint, since a pack is many records), lane losers are relabeled `superseded` for `cleanup_superseded`, and each removal is individually wrapped. Runs on the same daily and weekly schedulers as the Radarr pass. New `test_sonarr_queue_dupes.py`.
- Fix the duplicate-grab check being structurally blind to the exact downloads it polices. `cleanup_radarr_queue_dupes()` grouped `/api/v3/queue` records by `movieId`, but Radarr's Deluge client only reports torrents carrying its configured category label (`Deluge.GetItems()` → `GetTorrentsByLabel(MovieCategory)`), so the moment `handle_grab()` relabels a throttled upgrade from `radarr` to `radarr-upgrade` the torrent leaves the download-client item list and its queue record disappears. Every throttled upgrade was therefore invisible: the pass saw one entry per movie and removed nothing, while Deluge sat on two 0%-complete releases of the same film. (The same blind spot defeats Radarr's own `QueueSpecification`, which is why the second release gets grabbed at all.) The candidate set is now the queue *plus* in-flight torrents in the throttled lane, whose `movieId` and custom-format score come from Radarr's grab history (`/api/v3/history?downloadId=`). Losers with a queue record are removed via the queue API as before; losers without one are relabeled `superseded`, handing them to the existing `cleanup_superseded` path instead of deleting data out from under the tracker. Finished/seeding torrents are never touched (hit-and-run), score ties break toward whichever has already pulled bytes, and each removal is now individually wrapped so one failure can't abort the rest of the pass. New `test_queue_dupes.py` covers it with mocked Radarr/Deluge responses.
- Stalled-seed cleanup now unseeds instead of deleting, and covers labeled library-seed torrents. `remove_torrent()` gained a `remove_data` parameter (default `True`, unchanged for every existing caller); `cleanup_stalled_seeds` passes `remove_data=False` so a qualifying torrent stops being tracked/seeded by Deluge but its file(s) are left untouched on disk — required since a library-seed torrent's seeded file IS the live Plex media. Scope is now every *labeled* torrent (including `LIBRARY_SEED_LABEL`, the actual intended target); unlabeled/unmanaged torrents are skipped entirely.
- Stalled-seed cleanup: add a swarm-safety gate before removal. A torrent now also needs the tracker-reported `total_seeds` (swarm-wide, not our connected-peer count) to be greater than `STALL_MIN_SWARM_SEEDS` (env, default 5) on top of the existing staleness + negligible-upload checks, so removing our copy never drops availability. If `total_seeds` is unknown (tracker hasn't scraped yet — libtorrent reports `-1`, or the field is missing), the torrent is left alone rather than treated as safe to remove. `/run-stalled-seeds` preview output now includes `swarm_seeds` per candidate. For the "old library-seed torrents sitting around for months" use case, set `STALL_SEED_MIN_DAYS=180` (6 months) at deploy time — the code default (21) is unchanged and not overridden anywhere in `docker-compose.yml`/`.env.example`, so this is a manual deploy-time change.
- Add `scripts/set-min-seeders.sh`: idempotent bootstrap that sets Minimum Seeders (default 5) on every torrent indexer in both Radarr and Sonarr — stops grabbing 1-seeder releases that stall and feed the importBlocked/dupe pattern.
- importBlocked poller: send Sonarr's `includeUnknownSeriesItems` queue param alongside Radarr's `includeUnknownMovieItems` (each app ignores the other's), so unknown-series stuck items are visible to the Sonarr check too.
- `media_share.py` major overhaul: FTPS (FTP over TLS) support via `ftplib.FTP_TLS` in addition to SFTP; protocol dropdown in admin + self-service settings forms. Global rate limit now shared across all concurrent uploads (module-level token bucket) instead of per-friend. Poster grid UI for Movies and TV pulling metadata + posters from Radarr/Sonarr APIs instead of raw file listing; Music keeps file browser. Per-title access control: admin can restrict each friend to a specific subset of movies/shows via a searchable checkbox page at `/share/admin/friend/<id>/titles`. Self-service settings page at `/share/settings` so friends can update their own FTPS/SFTP destination without involving the admin. Usage page now shows an admin-wide breakdown table (all friends × all periods) for the admin, and per-user detail for regular friends. Admin friends table shows total data sent per friend. `Media Share` header links back to `/share`. Connection test handles `socket.timeout` explicitly with a clear "timed out after 10s" message.
- Queue all upgrade-labeled torrents (`sonarr-upgrade`/`radarr-upgrade`) to the bottom of the Deluge queue immediately on grab, and re-enforce top/bottom ordering every hour in `prioritize_normal_torrents()` — previously a newly-grabbed upgrade kept whatever queue position Deluge assigned it until the next monthly relabel cycle, so some upgrades queued ahead of others inconsistently.
- Add `media_share.py`: friend-facing media portal at `/share` — browse/download (resumable) from Movies/TV/Music libraries; push files/folders to a per-friend SFTP destination; admin panel at `/share/admin` (visible only to `ADMIN_EMAIL`) to manage friends, SFTP creds, per-library access, and per-friend rate limits entirely online with no restarts; SQLite-backed usage tracking; clean dark-theme UI.
- Run the Flask dev server with `threaded=True` so a slow synchronous webhook handler (e.g. `handle_upgrade_import` waiting on Deluge) can't briefly block other incoming Sonarr/Radarr webhooks.
- Fix potential `AttributeError` in `handle_grab()`/`handle_upgrade_import()` (`arr-webhook.py`) when Sonarr/Radarr sends `downloadId: null` — `.get('downloadId', '')` doesn't substitute the default for an explicit `null` value, only a missing key.
- Fix `NameError: name 'removed' is not defined` in `monthly_upgrade.py` when no torrents qualified for purging — `removed` was only initialized inside the `if to_remove:` block, crashing the script before it could reach the search/relabel steps.
- Fix Radarr bulk search (`radarr_bulk_search()` in `arr-webhook.py`, Step 2 of `monthly_upgrade.py`) sending `movieIds: []` to the `MoviesSearch` command — Radarr treats that as a no-op. Now fetches all monitored movie IDs first and passes them explicitly.
- Add `requirements.txt`, GitHub Actions CI (lint + Docker build), and this README.
- Initial public release: stripped a shared personal "monitor" stack down to just the Sonarr/Radarr/Deluge automation (`arr-webhook.py`, `monthly_upgrade.py`); the unrelated reddit/BTC/XMR monitors were split out to a separate project.
