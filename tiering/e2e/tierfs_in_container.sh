#!/usr/bin/env bash
# E2E_TIERFS_BIN stand-in that runs tierfs inside the `tiering` image, under its supervisor, with the flags and
# mount layout of docker-compose.tiering.yml: pid host, /dev/fuse, SYS_ADMIN + SYS_PTRACE, apparmor unconfined,
# the folder around the mount bound rslave and the mount point itself bound rshared. The e2e stack starts it
# like the binary (TIERFS_* in the environment) and waits for the FUSE mount to show up on the host, so the
# real-mount suites and the Plex test then read through a mount made inside the container. The runner is
# replaced by `sleep infinity`: the stack runs the real runner itself, on the host.
# Needs root and docker, and the image built: docker build -f tiering/Dockerfile -t tiering:e2e .
set -uo pipefail

: "${TIERFS_MOUNT:?TIERFS_MOUNT is not set}"
M=$TIERFS_MOUNT
ROOT=$(dirname "$M")
IMAGE=${E2E_TIERING_IMAGE:-tiering:e2e}
NAME=tiering-e2e-$$
ENVF=$(mktemp)
env | grep -E '^TIERFS_[A-Z0-9_]*=' > "$ENVF"

# A bind of the mount point onto itself, made shared: Docker refuses an rshared volume whose source is not on a
# shared mount, and this is what tierfs-host-mount.sh does on the host. Same file system, so it does not count as
# a mount for os.path.ismount: the stack still waits for tierfs.
mount --bind "$M" "$M" && mount --make-rshared "$M" || { echo "tierfs_in_container: cannot share $M" >&2; exit 1; }

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -f "$ENVF"
  for _ in 1 2 3 4; do umount -l "$M" 2>/dev/null || break; done  # anything left, then the self-bind
}

docker run --rm --name "$NAME" --network host --pid host \
  --device /dev/fuse --cap-add SYS_ADMIN --cap-add SYS_PTRACE --security-opt apparmor=unconfined \
  --env-file "$ENVF" -e SUPERVISE_RUNNER='["sleep","infinity"]' \
  -v "$ROOT:$ROOT:rslave" -v "$M:$M:rshared" \
  "$IMAGE" &
pid=$!
# The stack stops tierfs with SIGTERM and kills it after 10 s: stop the container well inside that.
trap 'docker stop -t 6 "$NAME" >/dev/null 2>&1' TERM INT
wait "$pid"
rc=$?
if kill -0 "$pid" 2>/dev/null; then wait "$pid"; rc=$?; fi  # wait was cut short by the signal
cleanup
exit "$rc"
