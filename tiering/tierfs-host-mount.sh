#!/bin/bash
# Makes the tierfs mount directory a shared mount point on the host, so the FUSE
# mount the tiering container makes inside it shows up on the host and, from there,
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
  # A bind made inside a shared mount joins its parent's peer group, and the FUSE mount
  # would then reach the host twice: on the bind, and tucked under it on the parent.
  # Private first, so the shared step below gives it a peer group of its own.
  mount --make-private "$M"
fi
mount --make-rshared "$M"
findmnt -o TARGET,PROPAGATION "$M"
