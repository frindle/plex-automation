#!/bin/bash
# Makes the tierfs mount directory a shared mount point on the host, so the FUSE
# mount the tierfs container makes inside it shows up on the host and, from there,
# in Plex and the arrs (which map the same path with slave propagation).
#
# Run on the host at array start, before the containers (User Scripts plugin,
# "At Startup of Array"). Safe to run again.
#
#   TIERFS_MOUNT=/mnt/addons/tierfs bash tierfs-host-mount.sh
set -eu
M="${TIERFS_MOUNT:-/mnt/addons/tierfs}"
case "$M" in /*) ;; *) echo "TIERFS_MOUNT must be an absolute path" >&2; exit 2 ;; esac
mkdir -p "$M"
if ! mountpoint -q "$M"; then
  mount --bind "$M" "$M"
fi
mount --make-rshared "$M"
findmnt -o TARGET,PROPAGATION "$M"
