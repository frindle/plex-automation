#!/usr/bin/env bash
# Start (or stop) the throwaway Plex Media Server used by the tiering e2e tests.
#   plex_up.sh up   WORK LIB MNT     start Plex, wait until it answers
#   plex_up.sh down WORK             remove the container
#
# No account, no claim token, no secrets:
#  - the server is never claimed (PLEX_CLAIM is not set), so it has no
#    plex.tv token and accepts API calls without X-Plex-Token from an allowed
#    network;
#  - it runs on the host network and allowedNetworks is limited to loopback,
#    so only local calls are accepted;
#  - Preferences.xml is seeded with discovery (GDM), remote access, plex.tv
#    publishing and update checks switched off, and debug logging on.
# LIB is bind-mounted read-only. MNT is bind-mounted with rslave, so a FUSE
# mount made on the host after this container starts shows up inside it. Both
# are mounted at the SAME absolute path inside the container as on the host,
# so a symlink in LIB that points into MNT resolves the same way for Plex.
set -euo pipefail

# Pinned by digest: plexinc/pms-docker 1.43.5.11079-b3b13e506 (multi-arch index).
IMAGE=${PLEX_IMAGE:-plexinc/pms-docker@sha256:6d778d41daae68f89115aa87a44ae14a96b23e71a2093da173b5a9aac4091336}
NAME=${PLEX_CONTAINER:-tiering-e2e-plex}

case "${1:-}" in
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    ;;
  up)
    work=$(realpath "${2:?WORK}"); lib=$(realpath "${3:?LIB}"); mnt=$(realpath "${4:?MNT}")
    prefdir="$work/plexconfig/Library/Application Support/Plex Media Server"
    mkdir -p "$prefdir" "$work/plextranscode"
    cat > "$prefdir/Preferences.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<Preferences FriendlyName="tiering-e2e" AcceptedEULA="1" GdmEnabled="0" PublishServerOnPlexOnlineKey="0" ManualPortMappingMode="0" EnableIPv6="0" allowedNetworks="127.0.0.1/255.255.255.255" LogDebug="1" ScheduledLibraryUpdatesEnabled="0" ButlerTaskBackupDatabase="0" ButlerTaskOptimizeDatabase="0" ButlerTaskCleanOldBandwidthStats="0" ButlerTaskRefreshLocalMedia="0" ButlerTaskRefreshLibraries="0" ButlerTaskUpgradeMediaAnalysis="0" ButlerTaskDeepMediaAnalysis="0" GenerateBIFBehavior="never" GenerateChapterThumbBehavior="never" disableRemoteSecurity="0" FirstRun="0"/>
XML
    if [ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ]; then
      chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$work/plexconfig" "$work/plextranscode"
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # Under sudo, Plex runs as the calling user, not root.
    docker run -d --name "$NAME" --network host \
      -e TZ=UTC -e PLEX_UID="${PLEX_UID:-${SUDO_UID:-$(id -u)}}" \
      -e PLEX_GID="${PLEX_GID:-${SUDO_GID:-$(id -g)}}" \
      -e ALLOWED_NETWORKS=127.0.0.0/8 \
      -v "$work/plexconfig:/config" -v "$work/plextranscode:/transcode" \
      -v "$lib:$lib:ro" \
      -v "$mnt:$mnt:${PLEX_MNT_OPT:-rslave}" \
      "$IMAGE" >/dev/null
    here=$(cd "$(dirname "$0")" && pwd)
    python3 "$here/plexctl.py" wait-ready >/dev/null
    echo "plex up: $(docker inspect -f '{{.Config.Image}}' "$NAME")"
    ;;
  *)
    echo "usage: plex_up.sh up WORK LIB MNT | down" >&2; exit 2 ;;
esac
