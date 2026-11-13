# Plex database offsite backup

`plex-db-r2.sh` is a nightly Unraid User Script that copies Plex's library database offsite, encrypted, without stopping Plex.

**Why:** CA Appdata Backup skips `Plex-Media-Server` (it would stop Plex mid-stream), so the weekly offsite copy in `r2:homelab-backup/appdata-backup` has no Plex at all. Without the database, every library, watch history and play state is gone.

## What each run does
1. Checks that `Plex-Media-Server` is running and finds its database folder (works with the pms-docker, linuxserver and binhex layouts).
2. Copies `com.plexapp.plugins.library.db` and `com.plexapp.plugins.library.blobs.db` with Plex's own SQLite: `VACUUM INTO` reads one consistent snapshot while Plex keeps running.
3. Runs `PRAGMA quick_check` on each copy and stops if either is not `ok`.
4. Tars the copies with `Preferences.xml` into `/mnt/user/appdata/homelab-scripts/plex-db/plex_YYYYMMDD_HHMM.tar.gz` (folder 700, file 600, last 2 kept).
5. Uploads to `r2crypt:plex-db` and keeps the newest 14 there. It never prunes when the listing looks wrong.

Skipped: metadata, posters, cache, codecs and logs. Plex rebuilds them.

**It only uploads to a crypt remote.** `Preferences.xml` holds the server's Plex token, so if `r2crypt` is missing or isn't type `crypt`, the run uploads nothing, sends one Unraid warning and exits 2.

Failures send an Unraid alert. A good run is silent. The log is `/mnt/user/appdata/homelab-scripts/plex-db-r2.log`.

## One-time setup (Unraid terminal)
1. **Make the crypt remote.** It wraps the existing `r2` remote, so no new Cloudflare key is needed. This prints two passwords once. Save both in Vaultwarden **and** on paper: without them the backups can't be read.
   ```
   P1=$(openssl rand -base64 32); P2=$(openssl rand -base64 32)
   echo "SAVE THESE: password=$P1  password2=$P2"
   docker run --rm -v /mnt/user/appdata/rclone:/config/rclone rclone/rclone \
     --config /config/rclone/rclone.conf config create r2crypt crypt \
     remote=r2:homelab-backup/crypt filename_encryption=standard \
     directory_name_encryption=true password="$P1" password2="$P2" --obscure >/dev/null
   unset P1 P2
   ```
2. **Install the script** as a User Script:
   ```
   mkdir -p /boot/config/plugins/user.scripts/scripts/plex-db-r2
   cp backup/plex-db-r2.sh /boot/config/plugins/user.scripts/scripts/plex-db-r2/script
   ```
3. **Dry run** ("Run Script" with `--dry-run` as the argument, or `bash .../script --dry-run`). It makes and checks the local archive and uploads nothing. Read the log.
4. **Schedule** it: Settings → User Scripts → Custom, cron `30 5 * * *` (05:30, after Plex's 02:00–05:00 maintenance and the 02:00 Nextcloud dump).
5. After the first live run, check both views of the bucket:
   ```
   RC="docker run --rm -v /mnt/user/appdata/rclone:/config/rclone rclone/rclone --config /config/rclone/rclone.conf"
   $RC lsf r2crypt:plex-db          # readable names
   $RC lsf r2:homelab-backup/crypt  # scrambled names: proof it's encrypted
   ```

Settings are environment variables at the top of the script (`CONTAINER`, `REMOTE`, `KEEP_REMOTE`, `KEEP_LOCAL`, `STEP_TIMEOUT`).

## Restore
1. Download the newest archive:
   ```
   RC="docker run --rm -v /mnt/user/appdata/rclone:/config/rclone -v /mnt/user/appdata/homelab-scripts/plex-restore:/out rclone/rclone --config /config/rclone/rclone.conf"
   $RC lsf r2crypt:plex-db | sort | tail -3
   $RC copyto r2crypt:plex-db/plex_YYYYMMDD_HHMM.tar.gz /out/plex.tar.gz
   mkdir -p /tmp/plex-restore && tar -xzf /mnt/user/appdata/homelab-scripts/plex-restore/plex.tar.gz -C /tmp/plex-restore
   ```
2. `docker stop Plex-Media-Server`.
3. In the `Plug-in Support/Databases` folder, move the current `com.plexapp.plugins.library.db*` and `com.plexapp.plugins.library.blobs.db*` files (including any `-wal` and `-shm`) into a subfolder, then copy the two restored files in.
4. Match the owner of the files around them: `chown --reference=<a neighbouring file> <restored file>`.
5. Copy `Preferences.xml` back only for a rebuilt server. It carries the server's identity and token.
6. `docker start Plex-Media-Server`. Posters and metadata re-download over the next hours.

## Cost
R2 bills $0.015 per GB-month above the free 10 GB, and `appdata-backup` already uses ~9.5 GB. Fourteen nightly Plex copies add roughly 14 × the compressed database size (`du -sh` of the `Databases` folder is the upper bound per copy): a few cents to under a dollar a month.

## Tests
`bash backup/tests/test_plex_db_r2.sh` runs against stub `docker`/`rclone`/`notify` commands and a fake tree (CI runs it). Never run it on the Mac.
