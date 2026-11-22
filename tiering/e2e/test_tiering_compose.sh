#!/usr/bin/env bash
# The `tiering` container as it is deployed: docker-compose.tiering.yml (with tiering/e2e/compose.e2e.yml for the
# network) on a /mnt layout like the host's, after tiering/tierfs-host-mount.sh. Checks that
#   - the container becomes healthy, the FUSE mount reaches the host and a container started BEFORE it with
#     ro,rslave (as Plex and the arrs map it);
#   - a killed runner is started again by the supervisor while the mount stays the same mount;
#   - the pause file stops the runner, the container stays healthy, and deleting it resumes;
#   - a killed tierfs takes the container down and Docker brings it back with one live mount, no dead one;
#   - a stop exits 0 within the grace period and leaves no mount behind.
# Runs in CI (tiering-e2e.yml): needs root, docker compose and FUSE. The runner is a dry run with dummy URLs.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
BASE=/mnt/tiering-e2e
M=$BASE/addons/tierfs
STATE=$BASE/state
DISK=$BASE/disk1
PROBE=tiering-e2e-probe
ENVF=$(mktemp)
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}

dc() { docker compose --env-file "$ENVF" -p tiering-e2e -f "$repo/docker-compose.tiering.yml" \
         -f "$here/compose.e2e.yml" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; echo "- $*" >> "$SUMMARY"; }
now() { date +%s; }
# FUSE mounts at $M in a mountinfo on stdin, as "<mount id>" lines.
fuse_ids() { awk -v m="$M" '$5 == m { for (i = 7; i <= NF; i++) if ($i == "-") { if ($(i+1) ~ /^fuse/) print $1; break } }'; }
host_fuse() { fuse_ids < /proc/self/mountinfo; }
probe_fuse() { docker exec "$PROBE" cat /proc/self/mountinfo | fuse_ids; }
health() { docker inspect -f '{{.State.Health.Status}}' tiering 2>/dev/null || echo none; }
restarts() { docker inspect -f '{{.RestartCount}}' tiering; }
runner_pid() { pgrep -f 'm offload[.]runner' || true; }
tierfs_pid() { pgrep -x tierfs || true; }
until_() {  # until_ <seconds> <what> <command...>
  local t=$1 what=$2; shift 2
  local end=$(( $(now) + t ))
  until "$@"; do [ "$(now)" -lt "$end" ] || fail "timed out after ${t}s waiting for $what"; sleep 1; done
}
is_healthy() { [ "$(health)" = healthy ]; }
one_live_mount() { [ "$(host_fuse | wc -l)" -eq 1 ] && timeout 10 ls "$M" >/dev/null; }
# Conditions for until_: re-run on every try, so they are functions, never "$(...)" arguments.
runner_up() { [ -n "$(runner_pid)" ]; }
runner_gone() { [ -z "$(runner_pid)" ]; }
new_runner() { local p; p=$(runner_pid); [ -n "$p" ] && [ "$p" != "$old" ]; }
restarted() { [ "$(restarts)" -gt "$r0" ]; }
probe_has_mount() { [ -n "$(probe_fuse)" ]; }
probe_one_mount() { [ "$(probe_fuse | wc -l)" -eq 1 ]; }

cleanup() {
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "--- docker logs tiering (tail)"; docker logs --tail 80 tiering 2>&1 || true
    echo "--- mounts at $M"; grep -F " $M " /proc/self/mountinfo || true
  fi
  dc down -t 60 >/dev/null 2>&1 || true
  docker rm -f "$PROBE" >/dev/null 2>&1 || true
  for _ in 1 2 3 4; do umount -l "$M" 2>/dev/null || break; done
  rm -rf --one-file-system "$BASE" "$ENVF"
  exit $rc
}
trap cleanup EXIT

echo "## The tiering container under compose" >> "$SUMMARY"
mkdir -p "$STATE"/{manifests,events,nzb} "$DISK/.stubs" "$DISK/media" "$BASE/user/media"
cat > "$ENVF" <<EOF
TIERING_IP=172.31.250.10
TIERING_TZ=UTC
TIERING_APPLY=0
TIERING_STATE_DIR=$STATE
TIERFS_MOUNT=$M
TIERFS_STUB_ROOTS=$DISK/.stubs
TIERING_SHARE_HOST=$BASE/user/media
TIERING_SHARE_PLEX=/media
TIERING_PATH_MAP=/media=$DISK/media
TIERING_GATE_PATH_MAP=$DISK/media=/data/media
TIERING_API=http://127.0.0.1:9
TIERING_PLEX_URL=http://127.0.0.1:9
TIERING_SONARR_URL=http://127.0.0.1:9
TIERING_RADARR_URL=http://127.0.0.1:9
TIERING_ARR_ALLOW_HTTP=1
TIERING_ALTMOUNT_ALLOW_HTTP=1
TIERFS_REMOTE_BASE=http://127.0.0.1:9
PLEX_TOKEN=e2e
SONARR_API_KEY=e2e
RADARR_API_KEY=e2e
EOF

# 1. The host side, as at array start.
TIERFS_MOUNT=$M bash "$repo/tiering/tierfs-host-mount.sh"
findmnt -n -o PROPAGATION "$M" | grep -q shared || fail "$M is not a shared mount after tierfs-host-mount.sh"

# 2. A container that maps the mount before tiering runs, the way Plex does.
docker run -d --name "$PROBE" -v "$M:$M:ro,rslave" busybox sleep 3600 >/dev/null

# 3. Up.
dc up -d --build tiering
until_ 150 "the container to be healthy" is_healthy
one_live_mount || fail "the host does not see one live FUSE mount at $M: $(host_fuse | wc -l)"
until_ 10 "the mount in the probe container" probe_has_mount
docker exec "$PROBE" ls "$M" >/dev/null || fail "the probe container cannot list $M"
[ "$(docker exec tiering python -m offload.supervise --health)" = ok ] || fail "--health is not ok"
docker logs tiering 2>&1 | grep -q "runner starting (dry run)" || fail "the runner did not start as a dry run"
ok "healthy; the mount reaches the host and a container started before it"

# 4. A killed runner is restarted; the mount and the container are untouched.
mount_id=$(host_fuse); r0=$(restarts); old=$(runner_pid)
[ -n "$old" ] || fail "no runner process"
kill -9 "$old"
until_ 60 "a new runner" new_runner
[ "$(host_fuse)" = "$mount_id" ] || fail "the mount changed when the runner died"
[ "$(restarts)" = "$r0" ] || fail "the container restarted when the runner died"
until_ 30 "healthy after the runner restart" is_healthy
ok "a killed runner is started again; same mount, no container restart"

# 5. Pause and resume.
touch "$STATE/runner.paused"
until_ 70 "the runner to stop" runner_gone
[ "$(docker exec tiering python -m offload.supervise --health)" = "ok (runner paused)" ] || fail "paused health"
sleep 8
is_healthy || fail "the container is not healthy while paused"
[ -z "$(runner_pid)" ] || fail "the runner came back while paused"
[ "$(host_fuse)" = "$mount_id" ] || fail "the mount changed while paused"
rm "$STATE/runner.paused"
until_ 10 "the runner to resume" runner_up
ok "the pause file stops the runner and keeps the mount; deleting it resumes"

# 6. A killed tierfs: the container exits, Docker restarts it, one live mount and no dead one.
r0=$(restarts); t=$(tierfs_pid)
[ -n "$t" ] || fail "no tierfs process"
kill -9 "$t"
until_ 60 "Docker to restart the container" restarted
until_ 150 "healthy after the restart" is_healthy
one_live_mount || fail "after the restart the host has $(host_fuse | wc -l) FUSE mounts at $M, or a dead one"
# Not the mount id: the kernel hands out the lowest free one, so the new mount often gets
# the old id back. The dead mount fails the ls in one_live_mount; this checks the server.
[ "$(tierfs_pid)" != "$t" ] || fail "tierfs is the old process"
until_ 10 "one mount in the probe container" probe_one_mount
docker exec "$PROBE" ls "$M" >/dev/null || fail "the probe container cannot list $M after the restart"
ok "a killed tierfs restarts the container; one live mount on the host and in the probe"

# 7. A clean stop.
t0=$(now)
dc stop tiering >/dev/null
took=$(( $(now) - t0 ))
code=$(docker inspect -f '{{.State.ExitCode}}' tiering)
[ "$code" = 0 ] || fail "stop: exit code $code"
[ "$took" -lt 85 ] || fail "stop took ${took}s (grace period 90s)"
[ -z "$(host_fuse)" ] || fail "a FUSE mount is left at $M after the stop"
[ -z "$(probe_fuse)" ] || fail "the probe container still has a FUSE mount after the stop"
ok "stop: exit 0 in ${took}s, no mount left"
echo "PASS"
