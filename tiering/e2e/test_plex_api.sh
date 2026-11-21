#!/usr/bin/env bash
# Full synthetic library in a live Plex: TV scan, real play calls, plex_watch,
# live session visibility and hotset. No offload parts needed.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
WORK=$(realpath "$(mktemp -d)")
LIB=$WORK/lib; MNT=$WORK/mnt
NAME=${PLEX_CONTAINER:-tiering-e2e-plex}
LOGDIR="$WORK/plexconfig/Library/Application Support/Plex Media Server/Logs"

cleanup() {
  rc=$?
  if [ $rc -ne 0 ]; then
    docker logs --tail 30 "$NAME" 2>&1 | cut -c1-300 || true
    tail -n 40 "$LOGDIR/Plex Media Server.log" 2>/dev/null | cut -c1-300 || true
  fi
  "$here/plex_up.sh" down || true
  sudo umount "$MNT" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT

mkdir -p "$LIB" "$MNT"
"$here/make_media.sh" "$LIB" >/dev/null
sudo mount --bind "$MNT" "$MNT"
sudo mount --make-rshared "$MNT"
"$here/plex_up.sh" up "$WORK" "$LIB" "$MNT"
python3 "$here/plexctl.py" create-sections --movies "$LIB/Movies" --tv "$LIB/TV" >/dev/null
python3 "$here/plexctl.py" scan >/dev/null
python3 "$here/test_plex_api.py"
