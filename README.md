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

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `DELUGE_PASSWORD`
   - `SONARR_API_KEY`
   - `RADARR_API_KEY`
2. Adjust the hardcoded Deluge/Sonarr/Radarr URLs, labels, `SEED_DAYS`, and `SEEDING_DIR` directly in `docker-compose.yml` if your setup differs.
3. Build and run:
   ```
   docker-compose build && docker-compose up -d
   ```
4. Point Sonarr/Radarr's webhook connections (Settings → Connect) at `http://<container-ip>:9876/webhook/sonarr` and `/webhook/radarr`, enabling the **On Grab** and **On Import** (upgrade) triggers.

## Deployment

Runs on Unraid at `/mnt/user/appdata/plex-automation`, on the `br0` macvlan network (external) at a static IP. `.env` lives only on the server and is gitignored — never commit real secrets.

## Changelog

### Unreleased
- Initial public release: stripped a shared personal "monitor" stack down to just the Sonarr/Radarr/Deluge automation (`arr-webhook.py`, `monthly_upgrade.py`); the unrelated reddit/BTC/XMR monitors were split out to a separate project.
