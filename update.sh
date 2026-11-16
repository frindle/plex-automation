#!/usr/bin/env bash
# One-shot update for the plex-automation container on Unraid.
#
# Usage:  ./update.sh
#         ./update.sh --no-cache    # rebuild from scratch (slower; use when
#                                     a dependency changed and you suspect
#                                     a stale layer)
#         ./update.sh --force       # build even if nothing in the image changed
#
# The image is rebuilt only when a file that goes into it changed since the
# last successful build. `docker-compose up -d` always runs, so .env edits are
# still picked up.

set -e
cd "$(dirname "$0")"

EXTRA_BUILD_ARGS=""
FORCE_BUILD=""
case "${1:-}" in
  --no-cache) EXTRA_BUILD_ARGS="--no-cache"; FORCE_BUILD="--no-cache" ;;
  --force) FORCE_BUILD="--force" ;;
esac

# --- build only what changed -------------------------------------------------
# $BUILD_MARKER holds the commit the image was last built from. It is
# gitignored, so `git reset --hard` leaves it alone. It is written only after a
# build succeeded (or after a range of commits that cannot change the image).
BUILD_MARKER=.last-built-sha

# is_build_input <path>: succeeds when a change to <path> can change the image.
# These are the files Dockerfile COPYs (plus the Dockerfile, the compose file and
# .dockerignore). Keep in step with the Dockerfile. A path not listed here
# cannot reach the image.
is_build_input() {
  case "$1" in
    Dockerfile|docker-compose.yml|.dockerignore|requirements.txt) return 0 ;;
    arr-webhook.py|monthly_upgrade.py|media_share.py|llm_decide.py|llm_advisor.py) return 0 ;;
    library_audit.py|downloads_audit.py|arr_health.py|deluge_trim.py) return 0 ;;
    episode_retention.py|rar_extract.py|torrent_breakdown.py|tiering_gate.py|speed_cap.py) return 0 ;;
  esac
  return 1
}

# decide_build: sets DO_BUILD (1 or 0) and BUILD_WHY. When in doubt: build.
decide_build() {
  DO_BUILD=1
  local new old changed f n=0
  new="$(git rev-parse HEAD)"
  old=""
  if [ -r "$BUILD_MARKER" ]; then old="$(tr -d '[:space:]' < "$BUILD_MARKER" 2>/dev/null || true)"; fi
  if [ -n "$FORCE_BUILD" ]; then BUILD_WHY="$FORCE_BUILD given"; return; fi
  if [ -z "$old" ]; then BUILD_WHY="no build marker yet (first run)"; return; fi
  if ! git cat-file -e "${old}^{commit}" 2>/dev/null; then BUILD_WHY="build marker is not a known commit"; return; fi
  if [ "$old" = "$new" ]; then DO_BUILD=0; BUILD_WHY="no new commits since the last build (${new:0:7})"; return; fi
  if ! changed="$(git diff --relative --no-renames --name-only "$old" "$new" -- 2>/dev/null)"; then
    BUILD_WHY="cannot diff ${old:0:7}..${new:0:7}"; return
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if is_build_input "$f"; then BUILD_WHY="image input changed: $f"; return; fi
    n=$((n + 1))
  done <<< "$changed"
  DO_BUILD=0
  BUILD_WHY="${n} file(s) changed since ${old:0:7}, none of them go into the image"
}

# Hard-reset to origin's main instead of `git pull`: this deploy clone has
# no local commits, and a plain pull fails with "divergent branches" whenever
# upstream history is force-pushed (e.g. a history cleanup). fetch+reset is
# deterministic and always lands exactly on origin/main.
echo "=== fetching + hard reset to origin/main ==="
git fetch origin
git reset --hard origin/main

echo "=== last commit ==="
git --no-pager log -1 --pretty='%h %s'

decide_build
if [ "$DO_BUILD" = 1 ]; then
  echo "=== building${EXTRA_BUILD_ARGS:+ (no cache)}: $BUILD_WHY ==="
  # shellcheck disable=SC2086
  docker-compose build $EXTRA_BUILD_ARGS
else
  echo "=== build skipped: $BUILD_WHY ==="
fi
# Reached only when the build succeeded (set -e) or was not needed.
git rev-parse HEAD > "$BUILD_MARKER"

echo "=== restart ==="
docker-compose up -d

echo "=== done ==="
docker-compose ps
