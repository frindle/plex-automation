# plex-automation

Webhook-driven automation for Sonarr/Radarr + Deluge, deployed as a Docker container on Unraid.

## What it does

`arr-webhook.py` runs a Flask app (port `9876`) that Sonarr and Radarr call via their notification webhooks:

- **On Grab**: if the grab is a quality upgrade and the release is over 10GB, labels the new torrent in Deluge so it can be throttled/tracked separately instead of competing with normal downloads.
- **On Download (import) of an upgrade**: finds the old torrent that the new file replaced and either deletes it immediately (PROPER/REPACK) or labels it `superseded` and moves it to a seeding directory.

Background schedulers (run inside the same process):
- **Daily**: removes `superseded` torrents that have seeded past `SEED_DAYS` (default 21), and dedupes Radarr's download queue (keeps the highest custom-format-score entry per movie).
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
- `media_share.py` major overhaul: FTPS (FTP over TLS) support via `ftplib.FTP_TLS` in addition to SFTP; protocol dropdown in admin + self-service settings forms. Global rate limit now shared across all concurrent uploads (module-level token bucket) instead of per-friend. Poster grid UI for Movies and TV pulling metadata + posters from Radarr/Sonarr APIs instead of raw file listing; Music keeps file browser. Per-title access control: admin can restrict each friend to a specific subset of movies/shows via a searchable checkbox page at `/share/admin/friend/<id>/titles`. Self-service settings page at `/share/settings` so friends can update their own FTPS/SFTP destination without involving the admin. Usage page now shows an admin-wide breakdown table (all friends × all periods) for the admin, and per-user detail for regular friends. Admin friends table shows total data sent per friend. `Media Share` header links back to `/share`. Connection test handles `socket.timeout` explicitly with a clear "timed out after 10s" message.
- Queue all upgrade-labeled torrents (`sonarr-upgrade`/`radarr-upgrade`) to the bottom of the Deluge queue immediately on grab, and re-enforce top/bottom ordering every hour in `prioritize_normal_torrents()` — previously a newly-grabbed upgrade kept whatever queue position Deluge assigned it until the next monthly relabel cycle, so some upgrades queued ahead of others inconsistently.
- Add `media_share.py`: friend-facing media portal at `/share` — browse/download (resumable) from Movies/TV/Music libraries; push files/folders to a per-friend SFTP destination; admin panel at `/share/admin` (visible only to `ADMIN_EMAIL`) to manage friends, SFTP creds, per-library access, and per-friend rate limits entirely online with no restarts; SQLite-backed usage tracking; clean dark-theme UI.
- Run the Flask dev server with `threaded=True` so a slow synchronous webhook handler (e.g. `handle_upgrade_import` waiting on Deluge) can't briefly block other incoming Sonarr/Radarr webhooks.
- Fix potential `AttributeError` in `handle_grab()`/`handle_upgrade_import()` (`arr-webhook.py`) when Sonarr/Radarr sends `downloadId: null` — `.get('downloadId', '')` doesn't substitute the default for an explicit `null` value, only a missing key.
- Fix `NameError: name 'removed' is not defined` in `monthly_upgrade.py` when no torrents qualified for purging — `removed` was only initialized inside the `if to_remove:` block, crashing the script before it could reach the search/relabel steps.
- Fix Radarr bulk search (`radarr_bulk_search()` in `arr-webhook.py`, Step 2 of `monthly_upgrade.py`) sending `movieIds: []` to the `MoviesSearch` command — Radarr treats that as a no-op. Now fetches all monitored movie IDs first and passes them explicitly.
- Add `requirements.txt`, GitHub Actions CI (lint + Docker build), and this README.
- Initial public release: stripped a shared personal "monitor" stack down to just the Sonarr/Radarr/Deluge automation (`arr-webhook.py`, `monthly_upgrade.py`); the unrelated reddit/BTC/XMR monitors were split out to a separate project.
