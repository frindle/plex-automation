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
# build succeeded; a skipped run leaves it, so the next diff still starts at the
# last real build.
BUILD_MARKER=.last-built-sha

# build_inputs: the build-context paths the Dockerfile COPYs or ADDs, one per
# line. Read from the Dockerfile itself, so a new COPY line needs no edit here.
# COPY --from=<stage> copies from another stage, not the context, so it is skipped.
build_inputs() {
  awk 'toupper($1) == "COPY" || toupper($1) == "ADD" {
         from = 0; prev = ""; out = ""
         for (i = 2; i <= NF; i++) {
           if ($i ~ /^--from=/) { from = 1; continue }
           if ($i ~ /^--/) continue
           if (prev != "") out = out prev "\n"
           prev = $i                      # the last word is the destination
         }
         if (!from) printf "%s", out
       }' Dockerfile 2>/dev/null
}

# dockerfile_unparseable: succeeds when a COPY/ADD line is in a form build_inputs
# cannot read: continued with a trailing backslash, or the JSON form COPY [...].
# Such a Dockerfile always means "build".
dockerfile_unparseable() {
  awk '(toupper($1) == "COPY" || toupper($1) == "ADD") {
         if ($0 ~ /\\[ \t]*$/) bad = 1
         for (i = 2; i <= NF; i++) if ($i !~ /^--/) { if ($i ~ /^\[/) bad = 1; break }
       }
       END { exit bad ? 0 : 1 }' Dockerfile 2>/dev/null
}

# is_build_input <path>: succeeds when a change to <path> can change the image:
# the Dockerfile, the compose file, .dockerignore, or anything a COPY/ADD line
# takes from the context (a file, a directory, `.`, or a glob such as *.py).
is_build_input() {
  local f="$1" src
  case "$f" in Dockerfile|docker-compose.yml|.dockerignore) return 0 ;; esac
  while IFS= read -r src; do
    src="${src#./}"; src="${src%/}"
    [ -n "$src" ] && [ "$src" != . ] || return 0      # COPY . copies the whole context
    [ "$f" = "$src" ] && return 0
    case "$f" in "$src"/*) return 0 ;; esac            # inside a copied directory
    # shellcheck disable=SC2254
    case "$f" in $src) return 0 ;; esac                # a glob source (may over-match: that only builds more)
  done <<< "$(build_inputs)"
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
  if dockerfile_unparseable; then BUILD_WHY="Dockerfile has a COPY/ADD line this script cannot parse (line continuation or JSON form)"; return; fi
  if [ -z "$(build_inputs)" ]; then BUILD_WHY="no COPY or ADD lines found in the Dockerfile"; return; fi
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
  # Reached only when the build succeeded (set -e).
  git rev-parse HEAD > "$BUILD_MARKER"
else
  echo "=== build skipped: $BUILD_WHY ==="
fi

echo "=== restart ==="
docker-compose up -d

echo "=== done ==="
docker-compose ps
