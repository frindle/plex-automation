#!/bin/bash
# Makes the tierfs directory a shared mount point on the host, so the FUSE mount the
# tiering container makes in its fs/ subfolder shows up on the host and, from there,
# in Plex and the arrs (which map the directory, not fs/, with slave propagation).
# The mount sits one level down so a container mapped while tierfs is up still maps
# this shared directory, never the FUSE mount itself, and follows every remount.
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
# tierfs creates it too; made here so the layout is in place before any container maps it.
[ -d "$M/fs" ] || mkdir "$M/fs" \
  || echo "warning: could not create $M/fs (is an old-layout tierfs still mounted at $M?)" >&2
findmnt -o TARGET,PROPAGATION "$M"
