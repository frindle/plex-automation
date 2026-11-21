#!/usr/bin/env bash
# Tests for update.sh: it must rebuild only when a file that goes into the image
# changed since the last successful build.
#
# update.sh runs `git reset --hard` and then Docker, so it is never run in this
# checkout. Each case runs a copy of it inside a throwaway clone that this test
# creates under a temp directory, with fake `docker` and `docker-compose` first
# on PATH that only log their arguments. The test refuses to start if the real
# docker could be reached.
#
# Run it anywhere with bash and git:  bash tests/update_sh_test.sh
set -u

# --- per-repo configuration ---------------------------------------------------
DOCKERFILE="Dockerfile"
COMPOSE_FILE="docker-compose.yml"
INPUT_FILE="speed_cap.py"                 # a file the Dockerfile COPYs
LOCKFILE="requirements.txt"               # a dependency file the Dockerfile COPYs ("" if none)
NONINPUT_FILES="README.md test_speed_cap.py"   # tracked files that never reach the image
NO_CACHE_FLAG=1                           # 1 when update.sh accepts --no-cache
# -------------------------------------------------------------------------------

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com

SHIMS="$WORK/shims"
FAKE_LOG="$WORK/docker.log"
export FAKE_LOG
mkdir -p "$SHIMS"
cat > "$SHIMS/docker-compose" <<'EOF'
#!/bin/sh
echo "dc $*" >> "$FAKE_LOG"
if [ "${1:-}" = build ] && [ -n "${FAKE_BUILD_FAIL:-}" ]; then exit 1; fi
exit 0
EOF
cat > "$SHIMS/docker" <<'EOF'
#!/bin/sh
if [ "${1:-}" = compose ]; then
  shift
  [ "${1:-}" = version ] && exit 0
  echo "dc $*" >> "$FAKE_LOG"
  if [ "${1:-}" = build ] && [ -n "${FAKE_BUILD_FAIL:-}" ]; then exit 1; fi
  exit 0
fi
echo "docker $*" >> "$FAKE_LOG"
exit 0
EOF
chmod +x "$SHIMS/docker" "$SHIMS/docker-compose"
export PATH="$SHIMS:$PATH"
if [ "$(command -v docker)" != "$SHIMS/docker" ] || [ "$(command -v docker-compose)" != "$SHIMS/docker-compose" ]; then
  echo "refusing to run: the real docker could be reached" >&2
  exit 2
fi

ORIGIN="$WORK/origin.git"
DEV="$WORK/dev"
DEPLOY="$WORK/deploy"

git init -q --bare -b main "$ORIGIN" || exit 2
git init -q -b main "$DEV" || exit 2
tar -C "$REPO" --exclude=.git --exclude=node_modules --exclude=.last-built-sha -cf - . | tar -C "$DEV" -xf -
rm -f "$DEV/.last-built-sha"
git -C "$DEV" add -A
git -C "$DEV" commit -q -m seed
git -C "$DEV" remote add origin "$ORIGIN"
git -C "$DEV" push -q origin main
git clone -q "$ORIGIN" "$DEPLOY" || exit 2

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (wanted '$3', got '$2')"; fi; }

# upstream <file> <how>: commit a change to <file> on origin/main.
upstream() {
  case "$2" in
    append) echo "# change $RANDOM$RANDOM" >> "$DEV/$1" ;;
  esac
  git -C "$DEV" add -A
  git -C "$DEV" commit -q -m "change $1"
  git -C "$DEV" push -q origin main
}

# run_update [args]: run update.sh from the deploy clone, only ever there.
RC=0
run_update() {
  : > "$FAKE_LOG"
  case "$DEPLOY" in "$WORK"/*) ;; *) echo "bug: deploy dir outside temp" >&2; exit 2 ;; esac
  (cd "$DEPLOY" && ./update.sh "$@" > "$WORK/out.txt" 2>&1)
  RC=$?
}
builds() { grep -c '^dc build' "$FAKE_LOG"; }
ups() { grep -c '^dc up -d' "$FAKE_LOG"; }
marker() { cat "$DEPLOY/.last-built-sha" 2>/dev/null || echo none; }
head_of_origin() { git -C "$ORIGIN" rev-parse main; }
reset_state() { run_update > /dev/null 2>&1; }   # settle: builds if needed, marker = origin/main

echo "== update.sh build-only-what-changed =="

# 1. first run builds
run_update
check "first run: exit 0" "$RC" 0
check "first run builds" "$(builds)" 1
check "first run runs up -d" "$(ups)" 1
check "first run writes the marker" "$(marker)" "$(head_of_origin)"
grep -q 'building' "$WORK/out.txt" && grep -q 'first run' "$WORK/out.txt" && ok "first run prints one line saying why" || bad "first run prints one line saying why"

# 2. nothing new
run_update
check "rerun, no change: no build" "$(builds)" 0
check "rerun, no change: up -d still runs" "$(ups)" 1
grep -q 'build skipped' "$WORK/out.txt" && ok "rerun prints the skip line" || bad "rerun prints the skip line"

# 3. only files that never reach the image
LAST_BUILT="$(marker)"
for f in $NONINPUT_FILES; do
  upstream "$f" append
  run_update
  check "$f only: no build" "$(builds)" 0
  check "$f only: up -d still runs" "$(ups)" 1
  check "$f only: marker stays at the last build" "$(marker)" "$LAST_BUILT"
done

# 4. an image input
upstream "$INPUT_FILE" append
run_update
check "$INPUT_FILE changed: builds" "$(builds)" 1
check "$INPUT_FILE changed: marker moves" "$(marker)" "$(head_of_origin)"
run_update
check "after that build, rerun does not build" "$(builds)" 0

# 5. Dockerfile, compose file, dependency file
for f in $DOCKERFILE $COMPOSE_FILE $LOCKFILE; do
  [ -n "$f" ] || continue
  upstream "$f" append
  run_update
  check "$f changed: builds" "$(builds)" 1
done

# 5b. the offload tier's images: its compose file, tierfs/ and tiering/offload/ build;
# the rest of tiering/ does not
for f in docker-compose.tiering.yml tierfs/config.go tiering/Dockerfile tiering/offload/state.py; do
  upstream "$f" append
  run_update
  check "$f changed: builds" "$(builds)" 1
done
upstream tiering/README.md append
run_update
check "tiering/README.md only: no build" "$(builds)" 0

# 6. a commit that touches both a doc and an input
set -- $NONINPUT_FILES
echo "# both $RANDOM" >> "$DEV/$1"
echo "# both $RANDOM" >> "$DEV/$INPUT_FILE"
git -C "$DEV" add -A; git -C "$DEV" commit -q -m both; git -C "$DEV" push -q origin main
run_update
check "doc + input in one commit: builds" "$(builds)" 1

# 7. a docs commit, then an input commit, before the next run: one build
upstream "$1" append
upstream "$INPUT_FILE" append
run_update
check "docs then input before one run: builds" "$(builds)" 1

# 8. --force, --no-cache
run_update --force
check "--force builds with nothing changed" "$(builds)" 1
if [ "$NO_CACHE_FLAG" = 1 ]; then
  run_update --no-cache
  check "--no-cache builds with nothing changed" "$(builds)" 1
  grep -q '^dc build --no-cache' "$FAKE_LOG" && ok "--no-cache is passed to the build" || bad "--no-cache is passed to the build"
fi

# 9. a failed build: non-zero exit, marker untouched, next run builds again
before="$(marker)"
upstream "$INPUT_FILE" append
FAKE_BUILD_FAIL=1 run_update
[ "$RC" != 0 ] && ok "failed build exits non-zero" || bad "failed build exits non-zero"
check "failed build leaves the marker as it was" "$(marker)" "$before"
check "failed build does not go on to up -d" "$(ups)" 0
run_update
check "run after a failed build builds" "$(builds)" 1
check "...and then writes the marker" "$(marker)" "$(head_of_origin)"

# 10. marker missing, unreadable content, or a commit this clone does not have
rm -f "$DEPLOY/.last-built-sha"
run_update
check "marker missing: builds" "$(builds)" 1
echo "not a sha" > "$DEPLOY/.last-built-sha"
run_update
check "marker garbage: builds" "$(builds)" 1
echo "0123456789abcdef0123456789abcdef01234567" > "$DEPLOY/.last-built-sha"
run_update
check "marker is an unknown commit (history rewritten): builds" "$(builds)" 1

# 12. inputs come from the Dockerfile: a newly COPYed file builds with no edit to update.sh
run_update > /dev/null 2>&1
echo "# new module" > "$DEV/zz_new_module.py"
git -C "$DEV" add -A; git -C "$DEV" commit -q -m "add module, not copied yet"; git -C "$DEV" push -q origin main
run_update
check "new .py not COPYed yet: no build" "$(builds)" 0
echo "COPY zz_new_module.py ." >> "$DEV/$DOCKERFILE"
upstream "$DOCKERFILE" none
run_update
check "COPY line added: builds" "$(builds)" 1
upstream zz_new_module.py append
run_update
check "newly COPYed file changed: builds" "$(builds)" 1

# 12b. a COPY this script cannot parse (continuation, JSON form) means build
cp "$DEV/$DOCKERFILE" "$WORK/df.bak"
printf 'COPY zz_new_module.py \\\n    ./\n' >> "$DEV/$DOCKERFILE"
upstream "$DOCKERFILE" none
run_update > /dev/null 2>&1
set -- $NONINPUT_FILES
upstream "$1" append
run_update
check "COPY continued with a backslash: a doc change builds" "$(builds)" 1
cp "$WORK/df.bak" "$DEV/$DOCKERFILE"
printf 'COPY ["zz_new_module.py", "./"]\n' >> "$DEV/$DOCKERFILE"
upstream "$DOCKERFILE" none
run_update > /dev/null 2>&1
upstream "$1" append
run_update
check "JSON-form COPY: a doc change builds" "$(builds)" 1
cp "$WORK/df.bak" "$DEV/$DOCKERFILE"
upstream "$DOCKERFILE" none
run_update > /dev/null 2>&1
upstream "$1" append
run_update
check "parseable Dockerfile again: a doc change does not build" "$(builds)" 0

# 13. COPY . copies the whole context, so any change builds
echo "COPY . ./ctx/" >> "$DEV/$DOCKERFILE"
upstream "$DOCKERFILE" none
run_update > /dev/null 2>&1
set -- $NONINPUT_FILES
upstream "$1" append
run_update
check "with COPY . in the Dockerfile, a doc change builds" "$(builds)" 1

# 11. the marker survives git reset --hard and is ignored by git
git -C "$DEPLOY" status --porcelain | grep -q 'last-built-sha' && bad "marker is gitignored" || ok "marker is gitignored"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
