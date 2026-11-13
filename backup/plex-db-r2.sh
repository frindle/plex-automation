#!/bin/bash
# plex-db-r2.sh -- nightly Unraid User Script: Plex's library database -> encrypted R2.
#
# Why: CA Appdata Backup skips Plex-Media-Server (stopping Plex mid-stream is not
# OK), so the offsite copy in r2:homelab-backup/appdata-backup has no Plex at all.
# Losing the database loses every library, watch history and play state.
#
# What: an online copy with Plex's own SQLite (VACUUM INTO: one read transaction,
# a consistent snapshot, Plex keeps running), checked with PRAGMA quick_check,
# tarred with Preferences.xml, and uploaded to an rclone *crypt* remote over R2.
# Metadata, cache, codecs and logs are skipped (Plex rebuilds them).
#
# Safety: Preferences.xml holds the server's Plex token, so the script refuses to
# upload unless REMOTE is a crypt remote. Until that remote exists it does nothing
# offsite (it notifies once per run and exits 2). --dry-run makes and checks the
# local copy but uploads and prunes nothing. See backup/README.md.
#
# Every host path hangs off ROOT (default empty = the real host) so tests can run
# against a fake tree with stub docker/notify commands.

set -uo pipefail

ROOT="${ROOT:-}"
CONTAINER="${CONTAINER:-Plex-Media-Server}"
PLEX_SQLITE="${PLEX_SQLITE:-/usr/lib/plexmediaserver/Plex SQLite}"
CONFIG_MOUNT="${CONFIG_MOUNT:-/config}"            # Plex's config folder inside the container
DBS="${DBS:-com.plexapp.plugins.library.db com.plexapp.plugins.library.blobs.db}"
LOCAL="${LOCAL:-$ROOT/mnt/user/appdata/homelab-scripts/plex-db}"
RCLONE_CONFIG_DIR="${RCLONE_CONFIG_DIR:-$ROOT/mnt/user/appdata/rclone}"
RCLONE_IMAGE="${RCLONE_IMAGE:-rclone/rclone}"
REMOTE="${REMOTE:-r2crypt:plex-db}"                # must be a crypt remote (checked)
KEEP_REMOTE="${KEEP_REMOTE:-14}"
KEEP_LOCAL="${KEEP_LOCAL:-2}"
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}"               # seconds per copy/check/upload step
LOG="${LOG:-$ROOT/mnt/user/appdata/homelab-scripts/plex-db-r2.log}"
LOCK_FILE="${LOCK_FILE:-$ROOT/var/run/plex-db-r2.lock}"
NOTIFY_CMD="${NOTIFY_CMD:-$ROOT/usr/local/emhttp/webGui/scripts/notify}"
DOCKER="${DOCKER:-docker}"
STAMP="${STAMP:-$(date +%Y%m%d_%H%M)}"

DRY=0
STAGE=""
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 64 ;;
esac

log()    { echo "$(date -u +%FT%TZ) $*"; }
notify() { [ -x "$NOTIFY_CMD" ] && "$NOTIFY_CMD" -e "Plex DB backup" -s "$1" -d "$2" -i "$3"; return 0; }
fail()   { log "FAIL: $1"; notify "Plex DB backup failed" "$1" alert; exit 1; }

rc() { # rclone in a throwaway container; LOCAL is mounted read-only at /data
  timeout "$STEP_TIMEOUT" "$DOCKER" run --rm \
    -v "$RCLONE_CONFIG_DIR:/config/rclone" -v "$LOCAL:/data:ro" \
    "$RCLONE_IMAGE" --config /config/rclone/rclone.conf "$@"
}
plexsql() { # db sql  (runs Plex's SQLite inside the Plex container)
  timeout "$STEP_TIMEOUT" "$DOCKER" exec "$CONTAINER" "$PLEX_SQLITE" "$1" "$2"
}

main() {
  log "=== start (dry=$DRY)"

  [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ] \
    || fail "container $CONTAINER is not running"

  # Host folder behind the container's config mount.
  local src
  src=$("$DOCKER" inspect -f "{{range .Mounts}}{{if eq .Destination \"$CONFIG_MOUNT\"}}{{.Source}}{{end}}{{end}}" "$CONTAINER" 2>/dev/null)
  src="$ROOT$src"
  [ -n "$src" ] && [ -d "$src" ] || fail "no host folder for $CONFIG_MOUNT in $CONTAINER"

  # Find the live database (images differ: pms-docker, linuxserver, binhex).
  local first="${DBS%% *}" dbfile dbdir base
  dbfile=$(find "$src" -maxdepth 6 -type f -path "*/Plug-in Support/Databases/$first" -print -quit 2>/dev/null)
  [ -n "$dbfile" ] || fail "$first not found under the Plex config folder"
  dbdir=$(dirname "$dbfile")
  base=$(dirname "$(dirname "$dbdir")")              # .../Plex Media Server
  local cdbdir="$CONFIG_MOUNT${dbdir#"$src"}"

  # Remote must be crypt before anything leaves the box.
  local rname="${REMOTE%%:*}" rtype=""
  if [ "$DRY" -eq 0 ]; then
    rtype=$(rc listremotes --long 2>/dev/null | awk -v n="$rname:" '$1==n {print $2}')
    if [ "$rtype" != "crypt" ]; then
      log "remote $rname is ${rtype:-missing}, not crypt: nothing uploaded (see backup/README.md)"
      notify "Plex DB backup not set up" "rclone remote $rname is ${rtype:-missing}; it must be a crypt remote" warning
      exit 2
    fi
  fi

  # Stage inside the config mount so both the container and the host can see it.
  STAGE="$src/.offsite-db-tmp"; local stage="$STAGE" cstage="$CONFIG_MOUNT/.offsite-db-tmp"
  rm -rf "$stage"; mkdir -p "$stage" || fail "cannot create $stage"
  trap 'rm -rf "$STAGE"' EXIT

  local db out
  for db in $DBS; do
    [ -f "$dbdir/$db" ] || fail "$db missing"
    plexsql "$cdbdir/$db" "VACUUM INTO '$cstage/$db';" || fail "copy of $db failed"
    [ -s "$stage/$db" ] || fail "copy of $db is empty"
    out=$(plexsql "$cstage/$db" "PRAGMA quick_check;" 2>&1)
    [ "$out" = "ok" ] || fail "copy of $db failed quick_check: $(echo "$out" | head -c 200)"
    log "copied $db $(du -h "$stage/$db" | cut -f1)"
  done
  if [ -f "$base/Preferences.xml" ]; then
    cp "$base/Preferences.xml" "$stage/" || fail "cannot copy Preferences.xml"
  else
    log "warning: no Preferences.xml next to the databases"
  fi

  mkdir -p "$LOCAL" && chmod 700 "$LOCAL" || fail "cannot create $LOCAL"
  local f="plex_${STAMP}.tar.gz"
  ( umask 077; tar -C "$stage" -czf "$LOCAL/$f.part" . ) || { rm -f "$LOCAL/$f.part"; fail "tar failed"; }
  gzip -t "$LOCAL/$f.part" || { rm -f "$LOCAL/$f.part"; fail "archive check failed"; }
  mv "$LOCAL/$f.part" "$LOCAL/$f"
  log "archived $f $(du -h "$LOCAL/$f" | cut -f1)"

  # Local: keep the newest KEEP_LOCAL archives.
  ls -1 "$LOCAL"/plex_*.tar.gz 2>/dev/null | sort | head -n -"$KEEP_LOCAL" | xargs -r rm -f

  if [ "$DRY" -eq 1 ]; then
    log "dry run: would upload $f to $REMOTE and keep $KEEP_REMOTE remote copies"
    log "=== done (dry)"; return 0
  fi

  rc copyto "/data/$f" "$REMOTE/$f" --s3-no-check-bucket --log-level INFO \
    || fail "$f not uploaded to $REMOTE"

  # Remote: keep the newest KEEP_REMOTE. Never prune when the listing is empty
  # or does not contain the file just uploaded (a bad listing must not delete).
  local listing old o
  listing=$(rc lsf "$REMOTE" --files-only 2>/dev/null | grep -E '^plex_[0-9]{8}_[0-9]{4}\.tar\.gz$' | sort)
  if ! grep -qx "$f" <<<"$listing"; then
    log "prune skipped: $f not in the remote listing"
  else
    old=$(head -n -"$KEEP_REMOTE" <<<"$listing")
    for o in $old; do log "prune $o"; rc deletefile "$REMOTE/$o" || log "prune of $o failed"; done
  fi
  log "=== ok"
}

mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "already running" >&2; exit 0; }
main >>"$LOG" 2>&1
