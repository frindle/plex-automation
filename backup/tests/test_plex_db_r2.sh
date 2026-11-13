#!/bin/bash
# Tests for backup/plex-db-r2.sh with stub docker/rclone/notify and a fake tree.
# Run: bash backup/tests/test_plex_db_r2.sh   (in claude-sandbox or CI, never on the Mac)

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/plex-db-r2.sh"
PASS=0; FAIL=0; CUR=""
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

ok()  { PASS=$((PASS + 1)); }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL [$CUR] $*"; }
eq()  { [ "$2" = "$3" ] && ok || bad "$1: got '$2' want '$3'"; }
has() { grep -q -- "$2" "$3" 2>/dev/null && ok || bad "$1: '$2' not in $(basename "$3")"; }

setup() { # name
  CUR="$1"; echo "- $1"
  R="$WORK/$1"; rm -rf "$R"; mkdir -p "$R/bin" "$R/remote" "$R/usr/local/emhttp/webGui/scripts" "$R/var/run"
  DBD="$R/appdata/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases"
  mkdir -p "$DBD"
  echo "library-db" >"$DBD/com.plexapp.plugins.library.db"
  echo "blobs-db" >"$DBD/com.plexapp.plugins.library.blobs.db"
  echo "old" >"$DBD/com.plexapp.plugins.library.db-2026-11-10"      # Plex's own rolling backup: ignored
  echo '<Preferences PlexOnlineToken="x"/>' >"$R/appdata/plex/Library/Application Support/Plex Media Server/Preferences.xml"
  : >"$R/notify.log"; : >"$R/docker.log"
  printf '#!/bin/sh\necho "$@" >>"%s/notify.log"\n' "$R" >"$R/usr/local/emhttp/webGui/scripts/notify"
  chmod +x "$R/usr/local/emhttp/webGui/scripts/notify"
  cat >"$R/bin/docker" <<'EOF'
#!/bin/bash
echo "$*" >>"$R/docker.log"
map() { echo "$R/appdata/plex${1#/config}"; }
case "$1" in
  inspect)
    case "$3" in
      *Running*) echo "${RUNNING:-true}" ;;
      *Mounts*)  echo "/appdata/plex" ;;
    esac ;;
  exec)
    db=$(map "$4"); sql="$5"
    case "$sql" in
      VACUUM\ INTO*) [ "${FAIL_VACUUM:-0}" = 1 ] && exit 1
                     out=$(echo "$sql" | sed -E "s/^VACUUM INTO '([^']*)';$/\1/"); cp "$db" "$(map "$out")" ;;
      PRAGMA\ quick_check*) echo "${QC:-ok}" ;;
      *) exit 9 ;;
    esac ;;
  run)
    shift; L=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -v) case "$2" in *:/data:ro) L="${2%%:/data:ro}" ;; esac; shift 2 ;;
        --rm) shift ;;
        rclone/rclone) shift; break ;;
        *) shift ;;
      esac
    done
    [ "$1" = "--config" ] && shift 2
    case "$1" in
      listremotes) echo "r2:      s3"; [ -n "${CRYPT_TYPE-crypt}" ] && echo "r2crypt: ${CRYPT_TYPE-crypt}" ;;
      copyto) [ "${FAIL_UPLOAD:-0}" = 1 ] && exit 1; cp "$L/${2#/data/}" "$R/remote/${3#*:plex-db/}" ;;
      lsf) [ "${EMPTY_LIST:-0}" = 1 ] || ls -1 "$R/remote" ;;
      deletefile) rm -f "$R/remote/${2#*:plex-db/}" ;;
    esac ;;
esac
EOF
  chmod +x "$R/bin/docker"
  unset RUNNING FAIL_VACUUM QC CRYPT_TYPE FAIL_UPLOAD EMPTY_LIST
}

run() { # args...
  ( export R; PATH="$R/bin:$PATH" ROOT="$R" DOCKER="$R/bin/docker" STAMP="${STAMP:-20261113_0615}" \
      bash "$SCRIPT" "$@" ); RC=$?
}
stage_gone() { [ ! -e "$R/appdata/plex/.offsite-db-tmp" ] && ok || bad "stage folder left behind"; }
LOCAL_DIR() { echo "$R/mnt/user/appdata/homelab-scripts/plex-db"; }
LOGF() { echo "$R/mnt/user/appdata/homelab-scripts/plex-db-r2.log"; }

setup happy
run; eq "exit" "$RC" 0
eq "remote files" "$(ls "$R/remote")" "plex_20261113_0615.tar.gz"
eq "archive contents" "$(tar -tzf "$R/remote/plex_20261113_0615.tar.gz" | sed 's|^\./||' | grep . | sort | tr '\n' ' ')" \
  "Preferences.xml com.plexapp.plugins.library.blobs.db com.plexapp.plugins.library.db "
eq "db copied intact" "$(tar -xOzf "$R/remote/plex_20261113_0615.tar.gz" ./com.plexapp.plugins.library.db)" "library-db"
eq "local archive kept" "$(ls "$(LOCAL_DIR)")" "plex_20261113_0615.tar.gz"
eq "local folder private" "$(stat -c %a "$(LOCAL_DIR)")" "700"
eq "archive private" "$(stat -c %a "$(LOCAL_DIR)/plex_20261113_0615.tar.gz")" "600"
eq "no notification" "$(cat "$R/notify.log")" ""
has "vacuum used Plex SQLite" "Plex SQLite" "$R/docker.log"
has "logged ok" "=== ok" "$(LOGF)"
stage_gone

setup no_crypt_remote
export CRYPT_TYPE=""; run; eq "exit" "$RC" 2
eq "nothing uploaded" "$(ls "$R/remote")" ""
has "warned" "not set up" "$R/notify.log"
eq "no local archive" "$(ls "$(LOCAL_DIR)" 2>/dev/null)" ""

setup remote_not_crypt
export CRYPT_TYPE="s3"; run; eq "exit" "$RC" 2
eq "nothing uploaded" "$(ls "$R/remote")" ""
has "says type" "is s3, not crypt" "$(LOGF)"

setup plex_stopped
export RUNNING=false; run; eq "exit" "$RC" 1
has "alert" "not running" "$R/notify.log"
eq "nothing uploaded" "$(ls "$R/remote")" ""

setup quick_check_fails
export QC="*** in database main *** Page 7 is never used"; run; eq "exit" "$RC" 1
has "alert" "quick_check" "$R/notify.log"
eq "nothing uploaded" "$(ls "$R/remote")" ""
eq "no local archive" "$(ls "$(LOCAL_DIR)" 2>/dev/null)" ""
stage_gone

setup vacuum_fails
export FAIL_VACUUM=1; run; eq "exit" "$RC" 1
has "alert" "copy of com.plexapp.plugins.library.db failed" "$R/notify.log"
stage_gone

setup upload_fails
export FAIL_UPLOAD=1; run; eq "exit" "$RC" 1
has "alert" "not uploaded" "$R/notify.log"
eq "local archive still kept" "$(ls "$(LOCAL_DIR)")" "plex_20261113_0615.tar.gz"

setup dry_run
export CRYPT_TYPE=""; run --dry-run; eq "exit" "$RC" 0
eq "nothing uploaded" "$(ls "$R/remote")" ""
eq "local archive made" "$(ls "$(LOCAL_DIR)")" "plex_20261113_0615.tar.gz"
if grep -q listremotes "$R/docker.log"; then bad "dry run asked rclone"; else ok; fi
stage_gone

setup bad_arg
run --apply; eq "exit" "$RC" 64

setup prune_remote
for d in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15; do : >"$R/remote/plex_202611${d}_0615.tar.gz"; done
: >"$R/remote/notes.txt"
STAMP=20261116_0615 run; eq "exit" "$RC" 0
eq "remote count" "$(ls "$R/remote" | grep -c '^plex_')" 14
eq "oldest gone" "$(ls "$R/remote" | grep '^plex_' | head -1)" "plex_20261103_0615.tar.gz"
eq "newest kept" "$(ls "$R/remote" | grep '^plex_' | tail -1)" "plex_20261116_0615.tar.gz"
eq "other files untouched" "$(ls "$R/remote" | grep -c notes.txt)" 1

setup prune_skipped_on_bad_listing
for d in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15; do : >"$R/remote/plex_202611${d}_0615.tar.gz"; done
export EMPTY_LIST=1; STAMP=20261116_0615 run; eq "exit" "$RC" 0
eq "nothing pruned" "$(ls "$R/remote" | grep -c '^plex_')" 16
has "logged skip" "prune skipped" "$(LOGF)"

setup prune_local
mkdir -p "$(LOCAL_DIR)"; for d in 10 11 12; do : >"$(LOCAL_DIR)/plex_202611${d}_0615.tar.gz"; done
run; eq "exit" "$RC" 0
eq "local keeps 2" "$(ls "$(LOCAL_DIR)" | tr '\n' ' ')" "plex_20261112_0615.tar.gz plex_20261113_0615.tar.gz "

echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
