#!/bin/bash
# glacier-mover.sh -- nightly Unraid User Script (stock tools only).
# Two phases, sharing one nightly cap:
#   evict  moves what keeps disk1 awake (data of torrents that are not paused,
#          plus EVICT_HOT_ROOTS) from disk1 to disk2, pausing torrents around it;
#   fill   moves the least-recently-opened media from disk2/disk3 to disk1 until
#          disk1 is ~98% full, leaving seeding torrents where they are.
# Dry run unless called with --apply. See tiering/README.md.
#
# Safety: disk paths only (/mnt/diskN, never the user-share view). Every move is
# copy (rsync, keeps times/perms/owners/xattrs) -> verify (size, mtime, byte
# compare) -> re-check the source did not change -> delete the source. Nothing
# is deleted before it is verified. Ranking reads metadata (atime) only.
#
# Every path hangs off ROOT (default empty = the real host), so tests can run
# against a fake tree.

ROOT="${ROOT:-}"
MNT="${MNT:-$ROOT/mnt}"
GLACIER="${GLACIER:-disk1}"                       # fill target
SRC_DISKS="${SRC_DISKS:-disk2 disk3}"             # move from, in no particular order
# Allowlist: only media under these folders (relative to /mnt/diskN) is ever considered.
# Real library roots (docker-compose mounts data/Media/{Movies,TV Shows,Music,...}). The Plex share is NOT included.
MEDIA_ROOTS="${MEDIA_ROOTS:-data/Media}"
MAIN_EXT="${MAIN_EXT:-mkv mp4 avi m4v ts m2ts wmv mov mpg mpeg webm mp3 flac m4a m4b aac ogg opus wav wma alac ape}"
SIDE_EXT="${SIDE_EXT:-srt ass ssa sub idx sup vtt nfo}"
# Never moved, whatever the extension: any path part equal to one of these (case-insensitive).
# Frigate (decommissioned 2026-11): its leftover recordings stay put until deleted, never filling glacier...
EXCLUDE_NAMES="${EXCLUDE_NAMES:-Downloads Incomplete Frigate InfluxDB Docker Documents Sandbox ollama-models appdata system domains Vault bloom-ledger GC lost+found}"
# ...or containing one of these (Time Machine shares, the reselling app).
EXCLUDE_CONTAINS="${EXCLUDE_CONTAINS:-timemachine resell}"
TARGET_PCT="${TARGET_PCT:-98}"                    # stop filling disk1 at this % used
# Fill runs only for a source disk above FILL_START_PCT used, and moves from it
# until it is down to FILL_STOP_PCT (or the cap or TARGET_PCT stops it first), so
# most nights move nothing. FILL_START_PCT=0 = fill whenever GLACIER has room.
FILL_START_PCT="${FILL_START_PCT:-85}"
FILL_STOP_PCT="${FILL_STOP_PCT:-75}"
NIGHTLY_CAP_BYTES="${NIGHTLY_CAP_BYTES:-1500000000000}"   # 1.5 TB per run
# A live run copies only inside this window, in the host's local time (the zone
# Unraid's cron uses). It may wrap midnight. "" = any time. Dry runs ignore it.
MOVE_WINDOW="${MOVE_WINDOW-22:00-06:00}"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-30}"                # never move a file modified in the last N days
# Shared with the ollama-queue container's Plex watcher (bind mount). plex-busy:
# the watcher touches it while Plex streams; a live run waits before each unit
# while it is under PLEX_BUSY_MAX_AGE seconds old (an older one is ignored).
# glacier-status: one line "<epoch> <state> key=value...", state = start, moving,
# yield, done, stopped or failed. Live runs only. "" = neither file is used.
STATE_DIR="${STATE_DIR-$MNT/cache/appdata/ollama-queue/state/glacier}"   # the queue container mounts .../state
PLEX_BUSY_MAX_AGE="${PLEX_BUSY_MAX_AGE:-600}"
YIELD_POLL_SEC="${YIELD_POLL_SEC:-60}"
MOVER_PID_FILE="${MOVER_PID_FILE:-$ROOT/var/run/mover.pid}"   # written by Unraid's mover and Mover Tuning's age_mover
CHUNK_UNITS="${CHUNK_UNITS:-100}"                 # units per torrent-gate request
MAX_MISFITS="${MAX_MISFITS:-200}"                 # stop after this many consecutive no-fit files
FOLDER_DEPTH="${FOLDER_DEPTH:-4}"                 # path parts (below /mnt/diskN) per "folder" in the report
TIERING_API="${TIERING_API:-http://10.0.0.4:9876}"        # plex-automation container
API_TIMEOUT="${API_TIMEOUT:-120}"
NO_TORRENT_CHECK="${NO_TORRENT_CHECK:-0}"         # 1 = skip the Deluge gate (not for normal use)
PAUSE_SETTLE_SEC="${PAUSE_SETTLE_SEC:-3}"         # wait after pausing so Deluge closes its file handles
RSYNC_FLAGS="${RSYNC_FLAGS:--aHAXS}"              # -a keeps perms/times/owner; -A ACLs; -X xattrs; -H hardlinks
LOG_DIR="${LOG_DIR:-$ROOT/var/log/tiering}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/glacier.log}"
NOTIFY_CMD="${NOTIFY_CMD:-$ROOT/usr/local/emhttp/webGui/scripts/notify}"
NOTIFY_ON_DRY="${NOTIFY_ON_DRY:-0}"
LOCK_FILE="${LOCK_FILE:-$ROOT/var/run/glacier-mover.lock}"
PROC_MOUNTS="${PROC_MOUNTS:-$ROOT/proc/mounts}"
MDCMD="${MDCMD:-mdcmd}"
TURBO_WRITE="${TURBO_WRITE:-1}"                   # 1 = turbo (reconstruct) write while a live run copies, set back at the end
TURBO_MIN_BYTES="${TURBO_MIN_BYTES:-100000000000}" # turbo only for a phase that plans at least this much (100 GB): it spins every array disk
VAR_INI="${VAR_INI:-$ROOT/var/local/emhttp/var.ini}"   # Unraid's live settings (md_write_method: auto, 0 or 1)
TURBO_STATE="${TURBO_STATE:-$ROOT/var/run/glacier-turbo-write.restore}"   # RAM: a reboot resets the setting too
PHASES="${PHASES:-evict fill}"                    # evict first (it frees room on disk1 for fill)
EVICT_TO="${EVICT_TO:-disk2}"                     # the disk that stays awake (seeding, hot data)
EVICT_SHARE="${EVICT_SHARE:-data}"                # evict only inside this share...
EVICT_HOT_ROOTS="${EVICT_HOT_ROOTS:-data/Documents data/ollama-models}"   # ...: these folders + active torrent roots
EVICT_DEST_PCT="${EVICT_DEST_PCT:-95}"            # never fill EVICT_TO past this % used
# Top folders of the share (second path part, case-insensitive) evict never touches.
EVICT_NEVER_TOP="${EVICT_NEVER_TOP:-Sandbox Vault bloom-ledger Frigate InfluxDB Docker appdata system domains}"
# A live evict needs GLACIER excluded from the share (else new files land on it again).
SHARE_CFG="${SHARE_CFG:-$ROOT/boot/config/shares/$EVICT_SHARE.cfg}"
# Spindown: evict looks up paths on GLACIER, and that wakes it. It skips the
# look when Unraid reports GLACIER spun down AND its read counter has not moved
# since the last live run ended: nothing on it was read, so nothing on it is hot.
EVICT_IDLE_SKIP="${EVICT_IDLE_SKIP:-1}"           # 0 = evict looks every night
DISKS_INI="${DISKS_INI:-$ROOT/var/local/emhttp/disks.ini}"   # Unraid's live disk state (spundown, device)
DISKSTATS="${DISKSTATS:-$ROOT/proc/diskstats}"
READS_STATE="${READS_STATE:-$ROOT/var/run/glacier-reads}"    # RAM: resets at reboot, like the kernel counter

APPLY=0
PHASE=fill; DEST="$GLACIER"; GATE_KEEP=1          # set per phase in main
NL=$'\n'
TAB=$'\t'

log() {
  local line
  line="$(date '+%F %T') $*"
  printf '%s\n' "$line"
  [ -n "${LOG_FILE:-}" ] && printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null
  return 0
}

local_hhmm() { date +%H%M; }

# in_window [HHMM] -> 0 inside MOVE_WINDOW (start inclusive, end exclusive) or when none is set
in_window() {
  [ -z "$MOVE_WINDOW" ] && return 0
  local now s e
  now="${1:-$(local_hhmm)}"; s="${MOVE_WINDOW%-*}"; e="${MOVE_WINDOW#*-}"
  now=$((10#$now)); s=$((10#${s/:/})); e=$((10#${e/:/}))
  if [ "$s" -le "$e" ]; then [ "$now" -ge "$s" ] && [ "$now" -lt "$e" ]
  else [ "$now" -ge "$s" ] || [ "$now" -lt "$e" ]; fi
}

# --- Plex yield and run status (shared with the queue container) -------------

STATE_OK=0; YIELD_SEC=0; EXIT_STATE=failed; EXIT_REASON=""

# state_dir_ok -> 0 when STATE_DIR is set and exists (created if only it is missing)
state_dir_ok() {
  [ -n "$STATE_DIR" ] || return 1
  [ -d "$STATE_DIR" ] && return 0
  [ -d "${STATE_DIR%/*}" ] && mkdir "$STATE_DIR" 2>/dev/null
  [ -d "$STATE_DIR" ]
}

status_detail() { printf 'phase=%s moved_bytes=%s yielded_sec=%s' "${PHASE:--}" "${B_MOVED:-0}" "$YIELD_SEC"; }

# write_status STATE [EXTRA] -- atomic one-line status, live runs with a STATE_DIR only
write_status() {
  [ "$STATE_OK" = 1 ] || return 0
  local f="$STATE_DIR/glacier-status"
  printf '%s %s %s%s\n' "$(date +%s)" "$1" "$(status_detail)" "${2:+ $2}" >"$f.tmp" 2>/dev/null &&
    mv -f "$f.tmp" "$f" 2>/dev/null
  return 0
}

# mover_running -> 0 while Unraid's mover (or Mover Tuning's) runs: the pid in its pid
# file is alive and is a mover (the same test Mover Tuning uses), or a process is named mover.
mover_running() {
  local pid
  pid="$(cat "$MOVER_PID_FILE" 2>/dev/null)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o args= 2>/dev/null | grep -q mover; then return 0; fi
  command -v pgrep >/dev/null 2>&1 && pgrep -x mover >/dev/null 2>&1
}

# wait_mover: a live run waits for the mover instead of copying alongside it, so the
# disks wake once for both. Gives up when the move window ends (WINDOW_CLOSED=1).
# A dry run only lists files, so it goes ahead with a warning.
wait_mover() {
  mover_running || return 0
  if [ "$APPLY" != 1 ]; then log "WARN: the mover is running: the dry run goes ahead (a live run would wait)"; return 0; fi
  local w=0
  log "WAIT  the mover is running: waiting for it to finish"
  while mover_running; do
    write_status yield reason=mover
    sleep "$YIELD_POLL_SEC"; w=$((w + YIELD_POLL_SEC))
    if ! in_window; then
      WINDOW_CLOSED=1
      log "STOP  the mover was still running at the end of the move window $MOVE_WINDOW (local time now $(date '+%H:%M %Z')): the rest waits for the next run"
      return 0
    fi
  done
  log "RESUME  the mover finished after ${w}s"
}

# plex_busy -> 0 while the watcher's plex-busy file is fresh
plex_busy() {
  [ "$STATE_OK" = 1 ] || return 1
  local m
  m="$(stat -c %Y "$STATE_DIR/plex-busy" 2>/dev/null)" || return 1
  [ $(( $(date +%s) - m )) -lt "$PLEX_BUSY_MAX_AGE" ]
}

hr() { # bytes -> human
  awk -v b="$1" 'BEGIN{split("B KB MB GB TB PB",u," ");i=1;while(b>=1000&&i<6){b/=1000;i++}printf (i==1?"%d %s":"%.2f %s"),b,u[i]}'
}

# --- overridable probes (tests redefine these after sourcing) ---------------

# fs_stats DISK -> "total_bytes used_bytes"
fs_stats() {
  df -B1 --output=used,avail "$MNT/$1" 2>/dev/null | awk 'NR==2{printf "%.0f %.0f\n", $1+$2, $1}'
}

# is_open FILE -> exit 0 when some process has the file open
is_open() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -s "$1" >/dev/null 2>&1
  elif command -v lsof >/dev/null 2>&1; then
    lsof "$1" >/dev/null 2>&1
  else
    return 1
  fi
}

# files_match A B -> exit 0 when the contents are identical (sha256sum, stock on Unraid)
files_match() {
  local a b
  a="$(sha256sum <"$1" 2>/dev/null | cut -d' ' -f1)"; b="$(sha256sum <"$2" 2>/dev/null | cut -d' ' -f1)"
  [ -n "$a" ] && [ "$a" = "$b" ]
}

# any_open FILE... -> exit 0 when any is open right now
any_open() { local p; for p in "$@"; do is_open "$p" && return 0; done; return 1; }

# gate_call: stdin = Deluge-namespace paths, stdout = status<TAB>reason<TAB>path (free/pause/wait).
# No side effects: pausing is a separate call per unit. GATE_KEEP=1 (fill phase): paths
# of seeding/queued torrents wait, so seeding data never lands on disk1.
gate_call() {
  curl -sS -f --max-time "$API_TIMEOUT" -X POST -H 'Content-Type: text/plain' --data-binary @- \
    "$TIERING_API/tiering/torrent-gate?format=tsv${GATE_KEEP:+&keep_seeding=1}"
}

# pause_call: stdin = one unit's Deluge-namespace paths. stdout = `paused<TAB>hash` (ours,
# resume it later) or `foreign<TAB>hash` (already paused by someone else, leave alone).
# Non-zero exit = could not pause (treat the unit as wait).
pause_call() {
  curl -sS -f --max-time "$API_TIMEOUT" -X POST -H 'Content-Type: text/plain' --data-binary @- \
    "$TIERING_API/tiering/torrent-pause${GATE_KEEP:+?keep_seeding=1}"
}

# active_roots_call: stdout = state<TAB>root (Deluge namespace) for every torrent that is
# not paused. Read-only. Non-zero exit = no answer (evict skips torrent data this run).
active_roots_call() {
  curl -sS -f --max-time "$API_TIMEOUT" "$TIERING_API/tiering/active-roots?format=tsv"
}

# resume_call: stdin = hashes this run paused. Non-zero exit = could not resume.
resume_call() {
  curl -sS -f --max-time "$API_TIMEOUT" -X POST -H 'Content-Type: text/plain' --data-binary @- \
    "$TIERING_API/tiering/torrent-resume"
}

# --- turbo write -------------------------------------------------------------
# By default Unraid writes parity by read/modify/write (md_write_method auto or 0):
# each block written reads the old data and parity first, so array copies run at
# roughly a third to a half of disk speed. Reconstruct write (1, "turbo") reads the
# other data disks instead and writes much faster, but every array disk
# spins while it copies. A live run turns it on just before its first copy and
# sets it back to read/modify/write when the run ends (Unraid's help text: Auto
# selects read/modify/write). Already on (Penn set it) = left alone both ways.
# The value to restore waits in TURBO_STATE, so a run that was killed before it
# could set it back is undone by the next run.
TURBO_TRIED=0; TURBO_ON=0

write_method() { # -> auto | 0 | 1 | "" (unreadable)
  sed -n 's/^md_write_method="\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' "$VAR_INI" 2>/dev/null | head -1
}

turbo_on() {
  [ "$APPLY" = 1 ] && [ "$TURBO_WRITE" = 1 ] && [ "$TURBO_TRIED" = 0 ] || return 0
  if [ "$PLAN_BYTES" -lt "$TURBO_MIN_BYTES" ]; then   # small move: only the disks in the copy and parity spin
    [ "$TURBO_SMALL_LOGGED" = "$PHASE" ] && return 0
    TURBO_SMALL_LOGGED="$PHASE"
    log "turbo write: left off for $PHASE, about $(hr "$PLAN_BYTES") planned is under $(hr "$TURBO_MIN_BYTES")"
    return 0
  fi
  TURBO_TRIED=1
  local was
  was="$(write_method)"
  case "$was" in
    auto|0) ;;
    1) log "turbo write: already on, left as is"; return 0 ;;
    *) log "WARN  turbo write: cannot read md_write_method from $VAR_INI, left as is"; return 0 ;;
  esac
  mkdir -p "$(dirname "$TURBO_STATE")" 2>/dev/null
  printf '%s\n' "$was" >"$TURBO_STATE" 2>/dev/null
  if "$MDCMD" set md_write_method 1 >/dev/null 2>&1; then
    TURBO_ON=1; log "turbo write: on for this run (was $was)"
  else
    rm -f "$TURBO_STATE"; log "WARN  turbo write: '$MDCMD set md_write_method 1' failed, copying at normal speed"
  fi
}

turbo_off() {
  [ "$TURBO_ON" = 1 ] || return 0
  if "$MDCMD" set md_write_method 0 >/dev/null 2>&1; then
    TURBO_ON=0; rm -f "$TURBO_STATE"; log "turbo write: off again (read/modify/write)"
  else
    log "WARN  turbo write: could not set md_write_method back to 0; the next run retries (or Settings -> Disk Settings)"
  fi
}

# A run that did not get to set it back (killed, crashed): undo before anything else.
turbo_restore_stale() {
  local was
  was="$(cat "$TURBO_STATE" 2>/dev/null)"
  [ -n "$was" ] || return 0
  log "turbo write: an earlier run left it on, setting it back"
  TURBO_ON=1; turbo_off
}

# --- spindown ---------------------------------------------------------------
# All of these read RAM only (emhttp's state file, the kernel's counters), never a disk.
ini_disk_val() { # DISK KEY -> value from disks.ini
  awk -v want="[\"$1\"]" -v key="$2" '$0 == want { d = 1; next } /^\[/ { d = 0 }
    d && index($0, key "=") == 1 { v = substr($0, length(key) + 2); gsub(/"/, "", v); print v; exit }' "$DISKS_INI" 2>/dev/null
}
sectors_read() { # DEV -> sectors read since boot
  awk -v dev="$1" '$3 == dev { print $6; exit }' "$DISKSTATS" 2>/dev/null
}
glacier_reads() { # -> "DEV SECTORS" for GLACIER, empty when unknown
  local dev n
  dev="$(ini_disk_val "$GLACIER" device)"; [ -n "$dev" ] || return 0
  n="$(sectors_read "$dev")"; [ -n "$n" ] && printf '%s %s\n' "$dev" "$n"
}
# glacier_idle -> 0 when GLACIER is spun down and was not read since the last live run ended.
# Unknown (no state file, a reboot since, unreadable) = not idle, so evict looks as before.
glacier_idle() {
  [ "$EVICT_IDLE_SKIP" = 1 ] || return 1
  [ "$(ini_disk_val "$GLACIER" spundown)" = 1 ] || return 1
  local now was
  now="$(glacier_reads)"; was="$(cat "$READS_STATE" 2>/dev/null)"
  [ -n "$now" ] && [ "$now" = "$was" ]
}
# note_reads: after a live run whose evict finished, remember GLACIER's read counter,
# so the run's own reads (evict's look, fill's read/modify/write) never count as use.
note_reads() {
  [ "$APPLY" = 1 ] && [ "$EVICT_OK" = 1 ] || return 0
  local now; now="$(glacier_reads)"
  if [ -n "$now" ]; then printf '%s\n' "$now" >"$READS_STATE" 2>/dev/null; else rm -f "$READS_STATE"; fi
}

# --- helpers ---------------------------------------------------------------

ere_escape() { printf '%s' "$1" | sed 's/[][\.*^$+?(){}|]/\\&/g'; }  # no \/ : GNU grep 3.8+ warns on it

preflight() {
  local d
  mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")" 2>/dev/null
  if [ -n "$MOVE_WINDOW" ] && ! [[ "$MOVE_WINDOW" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    log "ABORT: MOVE_WINDOW must look like 22:00-06:00 (got '$MOVE_WINDOW')"; return 2
  fi
  if ! [[ "$FILL_START_PCT" =~ ^[0-9]+$ && "$FILL_STOP_PCT" =~ ^[0-9]+$ ]] || [ "$FILL_START_PCT" -gt 100 ] \
      || { [ "$FILL_START_PCT" -gt 0 ] && [ "$FILL_STOP_PCT" -gt "$FILL_START_PCT" ]; }; then
    log "ABORT: FILL_START_PCT/FILL_STOP_PCT must be whole percents with stop <= start (got '$FILL_START_PCT'/'$FILL_STOP_PCT')"; return 2
  fi
  if ! [[ "$TURBO_MIN_BYTES" =~ ^[0-9]+$ ]]; then log "ABORT: TURBO_MIN_BYTES must be a whole number of bytes (got '$TURBO_MIN_BYTES')"; return 2; fi
  local disks="$GLACIER $SRC_DISKS"
  case " $PHASES " in *" evict "*) case " $disks " in *" $EVICT_TO "*) ;; *) disks="$disks $EVICT_TO" ;; esac ;; esac
  for d in $disks; do
    if [ ! -d "$MNT/$d" ]; then log "ABORT: $MNT/$d is not a directory (array stopped?)"; return 2; fi
    if [ -z "$ROOT" ] && ! mountpoint -q "$MNT/$d" 2>/dev/null; then
      log "ABORT: $MNT/$d is not a mount point"; return 2
    fi
    if [ -r "$PROC_MOUNTS" ] && grep -E " $(ere_escape "$MNT/$d") " "$PROC_MOUNTS" | grep -qw noatime; then
      log "WARN: $MNT/$d is mounted noatime: access times are only the date each file was added, so ranking is by date added until relatime is on"
      [ "${REQUIRE_ATIME:-0}" = 1 ] && return 2
    fi
  done
  if [ "$APPLY" = 1 ] && ! command -v fuser >/dev/null 2>&1 && ! command -v lsof >/dev/null 2>&1 \
      && [ "${OPEN_CHECK_OPTIONAL:-0}" != 1 ]; then
    log "ABORT: neither fuser nor lsof exists, cannot skip open files"; return 2
  fi
  if command -v "$MDCMD" >/dev/null 2>&1 && "$MDCMD" status 2>/dev/null | grep -Eq '^mdResync=[1-9]'; then
    # A dry run only lists files (metadata reads), so it may run; a live move waits.
    if [ "$APPLY" = 1 ]; then log "ABORT: a parity check or rebuild is running"; return 2; fi
    log "WARN: a parity check or rebuild is running: the dry run goes ahead (a live run would stop here)"
  fi
  return 0
}

# --- candidate selection (allowlist) ------------------------------------------
# A file is movable only if ALL hold: it lives under a MEDIA_ROOTS folder, has a
# media/sidecar extension, no path part is hidden or on the never-move list, it
# is not a database / VM disk / sparsebundle, and its mtime is older than
# MIN_AGE_DAYS. Sidecars (subtitles, .nfo) move only together with a main file
# whose name they start with. Open files are checked later, right before moving.

# awk helpers shared by the scans. Variables: xnames xcontains mainext sideext now mindays rejf roots_s
AWK_RULES='
function setup(   n, a, i) {
  n = split(tolower(xnames), a, " "); for (i = 1; i <= n; i++) xn[a[i]] = 1
  nxc = split(tolower(xcontains), xc, " ")
  n = split(tolower(mainext), a, " "); for (i = 1; i <= n; i++) me[a[i]] = 1
  n = split(tolower(sideext), a, " "); for (i = 1; i <= n; i++) se[a[i]] = 1
  n = split("db sqlite sqlite3 tsm wal shm img qcow qcow2 vdisk vmdk vhd vhdx", a, " "); for (i = 1; i <= n; i++) bad[a[i]] = 1
  nroots = split(roots_s, roots, " ")
}
function extof(b,   e) { if (index(b, ".") == 0) return ""; e = b; sub(/^.*\./, "", e); return e }
function why(rel, mt,   low, n, a, i, c, j, b, e) {
  low = tolower(rel); n = split(low, a, "/")
  for (i = 1; i <= n; i++) {
    c = a[i]
    if (substr(c, 1, 1) == ".") return "hidden folder or file"
    if (c in xn) return "never-move folder"
    for (j = 1; j <= nxc; j++) if (index(c, xc[j])) return "never-move folder"
    if (c ~ /\.sparsebundle$/) return "sparsebundle"
  }
  b = a[n]; e = extof(b)
  if ((e in bad) || index(b, ".sqlite") > 0 || index(b, ".db-") > 0) return "database or VM disk file"
  if (!((e in me) || (e in se))) return "not a media file"
  if (mindays + 0 > 0 && mt + 0 > now - mindays * 86400) return "modified within " mindays " days"
  return ""
}
function underroot(rel,   i) {
  for (i = 1; i <= nroots; i++) if (index(rel, roots[i] "/") == 1) return 1
  return 0
}'

build_candidates() {
  local out="$1" d r now
  : >"$out"; touch "$TMP/skipped-groups" "$TMP/reject"   # main empties these once per run
  now="$(date +%s)"
  local awkv=(-v "xnames=$EXCLUDE_NAMES" -v "xcontains=$EXCLUDE_CONTAINS" -v "mainext=$MAIN_EXT" -v "sideext=$SIDE_EXT"
              -v "now=$now" -v "mindays=$MIN_AGE_DAYS" -v "rejf=$TMP/reject" -v "roots_s=$MEDIA_ROOTS")
  for d in $SRC_DISKS; do
    for r in $MEDIA_ROOTS; do
      [ -d "$MNT/$d/$r" ] || continue
      find "$MNT/$d/$r" -type f -links 1 ! -name "*$NL*" -printf '%A@\t%s\t%T@\t%p\n' 2>/dev/null |
        awk -F'\t' "${awkv[@]}" -v disk="$d" -v pre="$MNT/$d/" "$AWK_RULES"'
          BEGIN { setup() }
          {
            p = $0; for (i = 0; i < 3; i++) p = substr(p, index(p, "\t") + 1)
            w = why(substr(p, length(pre) + 1), $3)
            if (w != "") { print w >> rejf; next }
            print $1 "\t" $2 "\t-\t" disk "\t" p
          }' >>"$out"
    done
    # Hardlinked files: scan the whole disk so a group with a link outside the
    # allowed area (Downloads, another share) is seen and skipped as a whole.
    find "$MNT/$d" -type f -links +1 ! -name "*$NL*" -printf '%i\t%A@\t%s\t%n\t%T@\t%p\n' 2>/dev/null |
      awk -F'\t' "${awkv[@]}" -v disk="$d" -v pre="$MNT/$d/" -v skipf="$TMP/skipped-groups" "$AWK_RULES"'
        BEGIN { setup() }
        {
          p = $0; for (i = 0; i < 5; i++) p = substr(p, index(p, "\t") + 1)
          k = $1; c = ++cnt[k]
          if ($2 + 0 > at[k] + 0) at[k] = $2
          sz[k] = $3; nl[k] = $4
          rel = substr(p, length(pre) + 1)
          if (underroot(rel)) { any[k] = 1; w = why(rel, $5); if (w != "" && blocked[k] == "") blocked[k] = w }
          else outside[k] = 1
          path[k, c] = p
        }
        END {
          for (k in cnt) {
            if (!any[k]) continue
            m = ""
            if (outside[k]) m = "a link is outside the media roots"
            else if (blocked[k] != "") m = "a link is not movable (" blocked[k] ")"
            else if (cnt[k] != nl[k]) m = "link count does not match members found"
            if (m != "") { print m "\t" path[k, 1] >> skipf; print "hardlink group blocked" >> rejf; continue }
            for (j = 1; j <= cnt[k]; j++) print at[k] "\t" sz[k] "\tG" disk ":" k "\t" disk "\t" path[k, j]
          }
        }' >>"$out"
  done
  # Attach sidecars to their main file (same folder, name starts with the main
  # file stem + "."); a sidecar with no accepted main file is rejected.
  awk -F'\t' "${awkv[@]}" "$AWK_RULES"'
    BEGIN { setup() }
    function dirof(p) { sub(/\/[^\/]*$/, "", p); return p }
    function baseof(p) { sub(/^.*\//, "", p); return p }
    {
      n++; at[n] = $1; sz[n] = $2; ky[n] = $3; dk[n] = $4
      p = $0; for (i = 0; i < 4; i++) p = substr(p, index(p, "\t") + 1); pa[n] = p
      b = baseof(p); e = tolower(extof(b))
      if ((e in se) && !(e in me)) { isside[n] = 1; next }
      if (ky[n] == "-") ky[n] = "P" dk[n] ":" p
      d = dirof(p); c = ++mc[d]; mi[d, c] = n; st = b; sub(/\.[^.]*$/, "", st); mst[d, c] = st
      if (!(ky[n] in usz)) usz[ky[n]] = sz[n]
    }
    END {
      for (s = 1; s <= n; s++) {
        if (!isside[s]) continue
        d = dirof(pa[s]); b = baseof(pa[s]); best = 0; bl = -1
        for (i = 1; i <= mc[d]; i++) { st = mst[d, i]; l = length(st)
          if (l > bl && substr(b, 1, l + 1) == st ".") { best = mi[d, i]; bl = l } }
        if (best == 0) { print "sidecar without a main file" >> rejf; isside[s] = 0; dead[s] = 1; continue }
        if (ky[s] != "-") { print "sidecar is hardlinked" >> rejf; isside[s] = 0; dead[s] = 1; continue }
        parent[s] = best; add[ky[best]] += sz[s]
      }
      for (k = 1; k <= n; k++) {
        if (dead[k]) continue
        m = isside[k] ? parent[k] : k
        print at[m] "\t" (usz[ky[m]] + add[ky[m]]) "\t" ky[m] "\t" dk[k] "\t" pa[k]
      }
    }' "$out" >"$out.assembled"
  LC_ALL=C sort -t "$TAB" -k1,1n -k3,3 -k5,5 -o "$out" "$out.assembled"
}

# --- evict candidates (what keeps disk1 awake) --------------------------------
# Every file on GLACIER under the root of a torrent that is not paused, plus every
# file under EVICT_HOT_ROOTS. Any extension, hidden files too, no age limit: a
# torrent's data moves whole or not at all. Hardlinks are closed over from one
# scan of the disk: all links of an inode move together, and torrents sharing an
# inode (cross-seeds, a pack hardlinked into the library) become one unit, so
# each unit means one pause. A unit with a link in a never-touch place, or whose
# link count does not add up, stays and is reported.

# too_broad REL -> 0 when a torrent root would sweep a whole library folder
too_broad() {
  local rel="$1" r rest
  case "$rel" in */*/*) ;; *) return 0 ;; esac
  for r in $MEDIA_ROOTS; do
    [ "$rel" = "$r" ] && return 0
    case "$rel" in "$r"/*) rest="${rel#"$r"/}"; case "$rest" in */*) ;; *) return 0 ;; esac ;; esac
  done
  return 1
}

# share_excludes_glacier -> 0 when SHARE_CFG keeps new files off GLACIER
share_excludes_glacier() {
  local inc exc
  [ -r "$SHARE_CFG" ] || return 1
  exc="$(sed -n 's/^shareExclude="\(.*\)"[[:space:]]*$/\1/p' "$SHARE_CFG" | head -1)"
  inc="$(sed -n 's/^shareInclude="\(.*\)"[[:space:]]*$/\1/p' "$SHARE_CFG" | head -1)"
  case ",$exc," in *",$GLACIER,"*) return 0 ;; esac
  if [ -n "$inc" ]; then case ",$inc," in *",$GLACIER,"*) return 1 ;; *) return 0 ;; esac; fi
  return 1
}

build_evict_candidates() {
  local out="$1" from="$MNT/$GLACIER" resp st root rel r key p n=0
  : >"$out"; : >"$TMP/evict-roots"; : >"$TMP/evict-files"; : >"$TMP/evict-links"; : >"$TMP/evict-blocked"
  if [ "$NO_TORRENT_CHECK" != 1 ]; then
    if resp="$(active_roots_call 2>&1)"; then
      while IFS="$TAB" read -r st root; do
        [ -n "$root" ] || continue
        rel="${root#/}"
        case "$rel" in "$EVICT_SHARE"/*) ;; *) continue ;; esac
        [ -e "$from/$rel" ] || continue
        if too_broad "$rel"; then log "WARN  evict: torrent root too broad, left alone: $root"; continue; fi
        n=$((n + 1)); printf 'T%s\t%s\t%s\n' "$n" "$st" "$from/$rel" >>"$TMP/evict-roots"
      done <<<"$resp"
    else
      log "WARN  evict: no answer from the torrent list, torrent data stays this run: $(printf '%s' "$resp" | head -c 160 | tr '\n' ' ')"
    fi
  fi
  if [ "$n" -gt 0 ]; then
    log "evict: $n torrent root(s) not paused have data on $GLACIER: $(cut -f2 "$TMP/evict-roots" | sort | uniq -c | awk '{printf "%s%s %s", (NR > 1 ? ", " : ""), $2, $1}')"
  fi
  for r in $EVICT_HOT_ROOTS; do
    case "$r" in "$EVICT_SHARE"/*) ;; *) log "WARN  evict: $r is outside the $EVICT_SHARE share, ignored"; continue ;; esac
    [ -d "$from/$r" ] && printf 'H\t-\t%s\n' "$from/$r" >>"$TMP/evict-roots"
  done
  while IFS="$TAB" read -r key st p; do
    find "$p" -type f ! -name "*$NL*" -printf '%i\t%n\t%s\t%A@\t%p\n' 2>/dev/null |
      awk -v k="$key" -v s="$st" '{ print k "\t" s "\t" $0 }'
  done <"$TMP/evict-roots" >"$TMP/evict-files"
  [ -s "$TMP/evict-files" ] || return 0
  if awk -F'\t' '$4 + 0 > 1 { f = 1; exit } END { exit !f }' "$TMP/evict-files"; then
    find "$from" -xdev -type f -links +1 ! -name "*$NL*" -printf '%i\t%n\t%p\n' 2>/dev/null >"$TMP/evict-links"
  fi
  awk -F'\t' -v pre="$from/" -v disk="$GLACIER" -v never="$EVICT_NEVER_TOP" -v xcontains="$EXCLUDE_CONTAINS" \
      -v rejf="$TMP/reject" -v blkf="$TMP/evict-blocked" '
    function top(x) { while (par[x] != x) { par[x] = par[par[x]]; x = par[x] } return x }
    function mk(x) { if (!(x in par)) par[x] = x }
    function join(a, b) { mk(a); mk(b); a = top(a); b = top(b); if (a != b) par[b] = a }
    function rest(n,   p, i) { p = $0; for (i = 0; i < n; i++) p = substr(p, index(p, "\t") + 1); return p }
    function extof(b,   e) { if (index(b, ".") == 0) return ""; e = b; sub(/^.*\./, "", e); return e }
    function why(rel, hot,   n, a, i, j, b, e) {
      n = split(tolower(rel), a, "/")
      if (n >= 2 && (a[2] in nv)) return "never-touch folder"
      for (i = 1; i <= n; i++) {
        for (j = 1; j <= nxc; j++) if (index(a[i], xc[j])) return "never-touch folder"
        if (a[i] ~ /\.sparsebundle$/) return "sparsebundle"
      }
      if (hot) { b = a[n]; e = extof(b); if ((e in bad) || index(b, ".sqlite") > 0 || index(b, ".db-") > 0) return "database or VM disk file" }
      return ""
    }
    BEGIN {
      n = split(tolower(never), a, " "); for (i = 1; i <= n; i++) nv[a[i]] = 1
      nxc = split(tolower(xcontains), xc, " ")
      n = split("db sqlite sqlite3 tsm wal shm img qcow qcow2 vdisk vmdk vhd vhdx", a, " "); for (i = 1; i <= n; i++) bad[a[i]] = 1
    }
    FILENAME == ARGV[1] { c = ++hn[$1]; hp[$1, c] = rest(2); next }   # all multi-link files on the disk
    {
      k = $1; p = rest(6)
      if (k == "H") { k = "H" p; hot[k] = 1 }
      mk(k)
      if (p in own) { join(own[p], k); next }                       # same file under two torrent roots
      own[p] = k; sz[p] = $5; at[p] = $6; ino[p] = $3; np++; pl[np] = p
      if ($4 + 0 > 1) {
        nl[$3] = $4; isz[$3] = $5; iat[$3] = $6
        if ($3 in ik) join(ik[$3], k); else ik[$3] = k
      }
    }
    END {
      for (i in ik) {                                                # pull in every link of each inode
        if (hn[i] + 0 != nl[i] + 0) blk[ik[i]] = "link count does not match members found"
        for (j = 1; j <= hn[i]; j++) {
          q = hp[i, j]
          if (q in own) { join(own[q], ik[i]); continue }
          own[q] = ik[i]; sz[q] = isz[i]; at[q] = iat[i]; ino[q] = i; np++; pl[np] = q
        }
      }
      for (m = 1; m <= np; m++) {
        p = pl[m]; g = top(own[p])
        w = why(substr(p, length(pre) + 1), hot[own[p]])
        if (w != "" && !(g in gb)) gb[g] = w
        if (!((g, ino[p]) in gi)) { gi[g, ino[p]] = 1; gsz[g] += sz[p] }   # each inode counted once
        if (at[p] + 0 > gat[g] + 0) gat[g] = at[p]
        c = ++cnt[g]; mem[g, c] = p
      }
      for (k in blk) { g = top(k); if (!(g in gb)) gb[g] = blk[k] }
      for (g in cnt) {
        if (g in gb) {
          print gb[g] "\t" mem[g, 1] >> blkf
          for (j = 1; j <= cnt[g]; j++) print "evict: unit left on " disk " (" gb[g] ")" >> rejf
          continue
        }
        id++
        for (j = 1; j <= cnt[g]; j++) printf "%s\t%.0f\tE%d\t%s\t%s\n", gat[g], gsz[g], id, disk, mem[g, j]
      }
    }' "$TMP/evict-links" "$TMP/evict-files" >"$out.raw"
  # hottest first: those are the units keeping the disk awake
  LC_ALL=C sort -t "$TAB" -k1,1nr -k3,3 -k5,5 -o "$out" "$out.raw"
}

# --- move one unit (a single file, or all links of one inode) ---------------

# move_unit SRC_DISK DEST_DISK PATH...  -> 0 moved, 1 failed (source untouched), 3 skipped
move_unit() {
  local dk="$1" to="$2"; shift 2
  turbo_on
  local p rel dest pre sz mt ino i
  local -a src=() dst=() before=() rsrc=() atimes=()
  pre="$MNT/$dk/"
  for p in "$@"; do
    rel="${p#"$pre"}"
    dest="$MNT/$to/$rel"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
      SKIP_REASON="destination already exists"; return 3
    fi
    if [ ! -f "$p" ]; then SKIP_REASON="source vanished"; return 3; fi
    src+=("$p"); dst+=("$dest"); rsrc+=("$pre./$rel")
    before+=("$(stat -c '%s %Y %i' "$p")"); atimes+=("$(stat -c '%X' "$p")")   # before anything reads it
  done
  if any_open "${src[@]}"; then SKIP_REASON="file is open (re-checked before copy)"; return 3; fi
  if ! rsync $RSYNC_FLAGS -R "${rsrc[@]}" "$MNT/$to/" 2>"$TMP/rsync.err"; then
    FAIL_REASON="rsync copy failed: $(head -c 200 "$TMP/rsync.err" | tr '\n' ' ')"; rm -f -- "${dst[@]}"; return 1
  fi
  for i in "${!src[@]}"; do
    read -r sz mt ino <<<"${before[$i]}"
    if [ "$(stat -c '%s' "${dst[$i]}" 2>/dev/null)" != "$sz" ]; then
      FAIL_REASON="size mismatch after copy"; rm -f -- "${dst[@]}"; return 1
    fi
    if [ "$(stat -c '%Y' "${dst[$i]}" 2>/dev/null)" != "$mt" ]; then
      FAIL_REASON="mtime mismatch after copy"; rm -f -- "${dst[@]}"; return 1
    fi
    if ! files_match "${src[$i]}" "${dst[$i]}"; then
      FAIL_REASON="checksum (sha256) mismatch"; rm -f -- "${dst[@]}"; return 1
    fi
  done
  # the source must not have changed while we copied
  for i in "${!src[@]}"; do
    if [ "$(stat -c '%s %Y %i' "${src[$i]}" 2>/dev/null)" != "${before[$i]}" ]; then
      FAIL_REASON="source changed during copy"; rm -f -- "${dst[@]}"; return 1
    fi
  done
  if any_open "${src[@]}"; then
    SKIP_REASON="file opened during copy (re-checked before delete)"; rm -f -- "${dst[@]}"; return 3
  fi
  for i in "${!dst[@]}"; do touch -a -d "@${atimes[$i]}" -- "${dst[$i]}" 2>/dev/null; done  # keep each access time
  for i in "${!src[@]}"; do
    rm -f -- "${src[$i]}" || { FAIL_REASON="could not delete source after verify"; return 1; }
  done
  return 0
}

# prune_dirs SRC_DISK DEST_DISK PATH... : after an evict, remove the folders the move
# emptied on the source disk, bottom up, while the same folder exists on the
# destination. Never the share itself or its top folders (data, data/Downloads).
prune_dirs() {
  local dk="$1" to="$2" p d rel; shift 2
  for p in "$@"; do
    d="$(dirname "$p")"
    while :; do
      rel="${d#"$MNT/$dk/"}"
      [ "$rel" != "$d" ] || break
      case "$rel" in */*/*) ;; *) break ;; esac
      [ -d "$MNT/$to/$rel" ] || break
      rmdir -- "$d" 2>/dev/null || break
      d="$(dirname "$d")"
    done
  done
  return 0
}

# --- planning loop ----------------------------------------------------------

CH_AT=(); CH_SZ=(); CH_DK=(); CH_PATHS=()
ROOM_LEFT=0; CAP_LEFT=0; MISFITS=0; STOP=0; WINDOW_CLOSED=0
declare -A SRC_LEFT=()   # fill: bytes each source disk may still give before FILL_STOP_PCT
N_MOVED=0; B_MOVED=0; N_FAILED=0; N_SKIPPED=0; N_PAUSED=0; N_UNITS=0
PLAN_BYTES=0; TURBO_SMALL_LOGGED=""; EVICT_OK=0
EV_RAN=0; EV_N=0; EV_B=0; EV_WAIT_N=0; EV_WAIT_B=0; EV_NOFIT=0   # evict-phase share of the above
PAUSED_HASHES=()   # hashes this run paused and has not resumed yet (the exit trap resumes them)

note_skip() { # reason path...
  local r="$1" p; shift
  for p in "$@"; do log "SKIP  $r  $p"; done
  N_SKIPPED=$((N_SKIPPED + $#))
}

gate_path() { # host path -> Deluge-namespace path (/mnt/diskN/data/x -> /data/x)
  local p="$1" d
  for d in $SRC_DISKS $GLACIER $EVICT_TO; do p="${p#"$MNT/$d"}"; done
  printf '%s' "$p"
}

# pause_unit PATH... -> 0 paused (PAUSED_HASHES set), 1 pause failed/no answer (nothing left paused)
pause_unit() {
  local resp st h hs=() p
  PAUSED_HASHES=()
  resp="$(for p in "$@"; do gate_path "$p"; printf '\n'; done | pause_call 2>&1)" || return 1
  while IFS="$TAB" read -r st h; do
    [ "$st" = paused ] && [ -n "$h" ] && hs+=("$h")
  done <<<"$resp"
  PAUSED_HASHES=("${hs[@]}")
  return 0
}

# resume_unit: resume what pause_unit paused; keep the hashes for the exit trap if that fails
resume_unit() {
  [ "${#PAUSED_HASHES[@]}" -eq 0 ] && return 0
  if printf '%s\n' "${PAUSED_HASHES[@]}" | resume_call >/dev/null 2>&1; then
    PAUSED_HASHES=()
  else
    log "WARN  could not resume ${#PAUSED_HASHES[@]} torrent(s) (the server resumes them after its pause limit): ${PAUSED_HASHES[*]}"
  fi
  return 0
}

count_moved() { # files bytes
  N_MOVED=$((N_MOVED + $1)); B_MOVED=$((B_MOVED + $2))
  if [ "$PHASE" = evict ]; then EV_N=$((EV_N + $1)); EV_B=$((EV_B + $2)); fi
  return 0
}

# refund I -- give chunk unit I's bytes back to the room, the cap and its source budget
refund() {
  ROOM_LEFT=$((ROOM_LEFT + CH_SZ[$1])); CAP_LEFT=$((CAP_LEFT + CH_SZ[$1]))
  [ -n "${SRC_LEFT[${CH_DK[$1]}]+x}" ] && SRC_LEFT[${CH_DK[$1]}]=$((SRC_LEFT[${CH_DK[$1]}] + CH_SZ[$1]))
  return 0
}

process_chunk() {
  local i j n=${#CH_AT[@]} p psz unitstat reason line st rs path
  [ "$n" -eq 0 ] && return 0
  if [ "$WINDOW_CLOSED" = 1 ]; then CH_AT=(); CH_SZ=(); CH_DK=(); CH_PATHS=(); return 0; fi
  local -A G=()
  if [ "$NO_TORRENT_CHECK" != 1 ]; then
    local resp
    resp="$(for i in "${!CH_AT[@]}"; do
              while IFS= read -r p; do gate_path "$p"; printf '\n'; done <<<"${CH_PATHS[$i]}"
            done | gate_call 2>&1)" || resp=""
    while IFS="$TAB" read -r st rs path; do
      [ -n "$path" ] && G["$path"]="$st$TAB$rs"
    done <<<"$resp"
  fi
  for i in "${!CH_AT[@]}"; do
    local -a paths=(); mapfile -t paths <<<"${CH_PATHS[$i]}"
    unitstat=free; reason=""
    if [ "$NO_TORRENT_CHECK" != 1 ]; then
      for p in "${paths[@]}"; do
        line="${G[$(gate_path "$p")]:-wait${TAB}no answer from the torrent gate}"
        st="${line%%"$TAB"*}"; rs="${line#*"$TAB"}"
        if [ "$st" = wait ]; then unitstat=wait; reason="$rs"; break; fi
        [ "$st" = pause ] && unitstat=pause
      done
    fi
    if [ "$unitstat" = wait ]; then
      refund "$i"
      if [ "$PHASE" = evict ]; then
        EV_WAIT_N=$((EV_WAIT_N + 1)); EV_WAIT_B=$((EV_WAIT_B + CH_SZ[i]))
        note_skip "evict: torrent: $reason" "${paths[@]}"; echo "evict: torrent busy (downloading, checking or erroring)" >>"$TMP/reject"
      else
        note_skip "torrent: $reason" "${paths[@]}"; echo "torrent still seeding" >>"$TMP/reject"
      fi
      continue
    fi
    [ "$unitstat" = pause ] && N_PAUSED=$((N_PAUSED + 1))
    if [ "$APPLY" != 1 ]; then
      psz=${CH_SZ[$i]}   # the unit's bytes go on its first file only, so the per-disk and folder sums match the total
      for p in "${paths[@]}"; do
        printf '%s -> %s\t%s\t%s\n' "${CH_DK[$i]}" "$DEST" "$psz" "${p#"$MNT/${CH_DK[$i]}/"}" >>"$TMP/plan"
        psz=0
      done
      log "WOULD MOVE  ${CH_DK[$i]} -> $DEST  $(hr "${CH_SZ[$i]}")  ${#paths[@]} file(s)  ${paths[0]}$([ "$unitstat" = pause ] && echo '  [torrent: pause]')"
      count_moved "${#paths[@]}" "${CH_SZ[$i]}"
      continue
    fi
    if ! in_window; then
      WINDOW_CLOSED=1; STOP=1
      log "STOP  outside the move window $MOVE_WINDOW (local time now $(date '+%H:%M %Z')): the rest waits for the next run"
      break
    fi
    if plex_busy; then
      local y0=$YIELD_SEC
      log "YIELD  plex streaming: waiting before the next unit"
      while plex_busy; do
        write_status yield
        sleep "$YIELD_POLL_SEC"; YIELD_SEC=$((YIELD_SEC + YIELD_POLL_SEC))
        if ! in_window; then
          WINDOW_CLOSED=1; STOP=1
          log "STOP  outside the move window $MOVE_WINDOW while waiting for Plex (local time now $(date '+%H:%M %Z')): the rest waits for the next run"
          break 2
        fi
      done
      log "RESUME  plex idle after $((YIELD_SEC - y0))s"
    fi
    wait_mover                      # a mover that started mid-run: let it finish first
    if [ "$WINDOW_CLOSED" = 1 ]; then STOP=1; break; fi
    write_status moving
    SKIP_REASON=""; FAIL_REASON=""
    if [ "$unitstat" = pause ]; then
      if ! pause_unit "${paths[@]}"; then
        refund "$i"
        N_PAUSED=$((N_PAUSED - 1))
        note_skip "torrent: could not pause (no answer or refused)" "${paths[@]}"
        if [ "$PHASE" = evict ]; then
          EV_WAIT_N=$((EV_WAIT_N + 1)); EV_WAIT_B=$((EV_WAIT_B + CH_SZ[i])); echo "evict: torrent could not be paused" >>"$TMP/reject"
        else
          echo "torrent still seeding" >>"$TMP/reject"
        fi
        continue
      fi
      [ "${#PAUSED_HASHES[@]}" -gt 0 ] && [ "$PAUSE_SETTLE_SEC" != 0 ] && sleep "$PAUSE_SETTLE_SEC"
    fi
    move_unit "${CH_DK[$i]}" "$DEST" "${paths[@]}"
    local mrc=$?
    resume_unit
    case $mrc in
      0) count_moved "${#paths[@]}" "${CH_SZ[$i]}"
         psz=${CH_SZ[$i]}
         for p in "${paths[@]}"; do
           printf '%s -> %s\t%s\t%s\n' "${CH_DK[$i]}" "$DEST" "$psz" "${p#"$MNT/${CH_DK[$i]}/"}" >>"$TMP/plan"
           log "MOVED  ${CH_DK[$i]} -> $DEST  $psz bytes  $p"
           psz=0
         done
         [ "$PHASE" = evict ] && prune_dirs "${CH_DK[$i]}" "$DEST" "${paths[@]}" ;;
      3) refund "$i"
         note_skip "$SKIP_REASON" "${paths[@]}" ;;
      *) refund "$i"
         N_FAILED=$((N_FAILED + 1)); log "FAILED  $FAIL_REASON  (source kept)  ${paths[*]}" ;;
    esac
  done
  CH_AT=(); CH_SZ=(); CH_DK=(); CH_PATHS=()
}

# flush_unit DISK ATIME SIZE PATH...
flush_unit() {
  local dk="$1" at="$2" sz="$3"; shift 3
  local p
  N_UNITS=$((N_UNITS + 1))
  if [ "$sz" -gt "$ROOM_LEFT" ] || [ "$sz" -gt "$CAP_LEFT" ]; then
    MISFITS=$((MISFITS + 1)); [ "$PHASE" = evict ] && EV_NOFIT=$((EV_NOFIT + 1))
    [ "$MISFITS" -ge "$MAX_MISFITS" ] && STOP=1
    return 0
  fi
  if [ -n "${SRC_LEFT[$dk]+x}" ] && [ "${SRC_LEFT[$dk]}" -le 0 ]; then return 0; fi   # source at FILL_STOP_PCT
  MISFITS=0
  # Evict skips this early check: Deluge holds seeding files open until the pause,
  # and move_unit checks again after it.
  if [ "$PHASE" != evict ]; then
    for p in "$@"; do
      if is_open "$p"; then note_skip "file is open" "$@"; echo "open file" >>"$TMP/reject"; return 0; fi
    done
  fi
  ROOM_LEFT=$((ROOM_LEFT - sz)); CAP_LEFT=$((CAP_LEFT - sz))
  if [ -n "${SRC_LEFT[$dk]+x}" ]; then
    SRC_LEFT[$dk]=$((SRC_LEFT[$dk] - sz))
    local d left=0; for d in "${!SRC_LEFT[@]}"; do [ "${SRC_LEFT[$d]}" -gt 0 ] && left=1; done
    [ "$left" = 0 ] && STOP=1   # every source is down to FILL_STOP_PCT
  fi
  CH_AT+=("$at"); CH_SZ+=("$sz"); CH_DK+=("$dk")
  local joined="" ; for p in "$@"; do joined="${joined:+$joined$NL}$p"; done
  CH_PATHS+=("$joined")
  [ "${#CH_AT[@]}" -ge "$CHUNK_UNITS" ] && process_chunk
  return 0
}

plan_and_run() {
  local at sz key dk p ckey="" cdk="" cat="" csz=""
  local -a cps=()
  # Rough size of this phase's move (for the turbo decision): candidates, capped by room and the nightly cap.
  PLAN_BYTES="$(awk -F'\t' -v room="$ROOM_LEFT" -v cap="$CAP_LEFT" '{ s += $2 }
    END { if (room < s) s = room; if (cap < s) s = cap; if (s < 0) s = 0; printf "%.0f\n", s }' "$TMP/cands")"
  while IFS="$TAB" read -r at sz key dk p; do
    [ "$STOP" = 1 ] && break
    if [ "$key" != "-" ] && [ "$key" = "$ckey" ]; then cps+=("$p"); continue; fi
    if [ "${#cps[@]}" -gt 0 ]; then flush_unit "$cdk" "$cat" "$csz" "${cps[@]}"; fi
    [ "$STOP" = 1 ] && break
    ckey="$key"; cdk="$dk"; cat="$at"; csz="$sz"; cps=("$p")
  done <"$TMP/cands"
  if [ "$STOP" != 1 ] && [ "${#cps[@]}" -gt 0 ]; then flush_unit "$cdk" "$cat" "$csz" "${cps[@]}"; fi
  process_chunk
}

# --- reporting --------------------------------------------------------------

report() {
  local mode="DRY RUN"; [ "$APPLY" = 1 ] && mode="APPLY"
  log "=== glacier $mode summary ==="
  log "allowlist roots: $MEDIA_ROOTS (on: $SRC_DISKS); extensions: $MAIN_EXT + sidecars $SIDE_EXT; never-move names: $EXCLUDE_NAMES, *$(echo "$EXCLUDE_CONTAINS" | sed 's/ /*, */g')*; min mtime age: ${MIN_AGE_DAYS}d"
  [ "$EV_RAN" = 1 ] && log "evict scope: torrents not paused + $EVICT_HOT_ROOTS, inside $EVICT_SHARE on $GLACIER; never: $EVICT_NEVER_TOP"
  if [ -s "$TMP/reject" ]; then
    log "rejected files by rule:"
    sort "$TMP/reject" | uniq -c | sort -rn | while read -r c r; do log "  $c  $r"; done
  fi
  if [ -s "$TMP/plan" ]; then
    awk -F'\t' '{c[$1]++; b[$1]+=$2} END{for(d in c) printf "%s\t%d\t%.0f\n", d, c[d], b[d]}' "$TMP/plan" | sort |
      while IFS="$TAB" read -r d c b; do log "per-disk  $d: $c file(s), $(hr "$b")"; done
    log "top folders by bytes:"
    awk -F'\t' -v depth="$FOLDER_DEPTH" '
      { n = split($3, a, "/"); k = ""; m = (n - 1 < depth) ? n - 1 : depth
        for (i = 1; i <= m; i++) k = k (i > 1 ? "/" : "") a[i]
        if (k == "") k = "(top)"; bytes[k] += $2; files[k]++ }
      END { for (k in bytes) printf "%.0f\t%d\t%s\n", bytes[k], files[k], k }' "$TMP/plan" |
      sort -t "$TAB" -k1,1nr 2>/dev/null | head -20 |
      while IFS="$TAB" read -r b c k; do log "  $(hr "$b")  $c file(s)  $k"; done
  else
    log "nothing to move"
  fi
  log "totals: moved=$N_MOVED ($(hr "$B_MOVED")) failed=$N_FAILED skipped=$N_SKIPPED torrents-paused=$N_PAUSED"
  [ "$YIELD_SEC" -gt 0 ] && log "waited ${YIELD_SEC}s for Plex streams"
  if [ "$EV_RAN" = 1 ]; then
    log "evict $GLACIER -> $EVICT_TO: moved=$EV_N ($(hr "$EV_B")) waiting-on-torrent=$EV_WAIT_N unit(s) ($(hr "$EV_WAIT_B")) no-room-or-cap=$EV_NOFIT unit(s)"
    if [ -s "$TMP/evict-blocked" ]; then
      log "evict left $(wc -l <"$TMP/evict-blocked") unit(s) on $GLACIER, e.g.: $(head -3 "$TMP/evict-blocked" | tr '\t' ' ' | tr '\n' ';')"
    fi
  fi
  if [ -s "$TMP/skipped-groups" ]; then
    local g; g="$(wc -l <"$TMP/skipped-groups")"
    log "skipped $g hardlink group(s), e.g.: $(head -3 "$TMP/skipped-groups" | tr '\t' ' ' | tr '\n' ';')"
  fi
  if [ "$APPLY" = 1 ] || [ "$NOTIFY_ON_DRY" = 1 ]; then
    local imp=normal; [ "$N_FAILED" -gt 0 ] && imp=warning
    local subj="Glacier: moved $N_MOVED, failed $N_FAILED, skipped $N_SKIPPED"
    [ "$APPLY" = 1 ] || subj="Glacier dry run: would move $N_MOVED, skipped $N_SKIPPED"
    [ "$WINDOW_CLOSED" = 1 ] && subj="$subj, stopped: outside $MOVE_WINDOW"
    [ "$YIELD_SEC" -gt 0 ] && subj="$subj, waited $(( (YIELD_SEC + 59) / 60 )) min for Plex"
    if [ -x "$NOTIFY_CMD" ]; then
      "$NOTIFY_CMD" -e "Glacier tiering" -s "$subj" \
        -d "$(hr "$((B_MOVED - EV_B))") to $GLACIER, $(hr "$EV_B") off it to $EVICT_TO; torrents paused and resumed $N_PAUSED. Log: $LOG_FILE" -i "$imp" >/dev/null 2>&1
    else
      log "notify script not found at $NOTIFY_CMD"
    fi
  fi
}

# phase_evict: move what keeps GLACIER awake to EVICT_TO
phase_evict() {
  local total used
  PHASE=evict; DEST="$EVICT_TO"; GATE_KEEP=""; SRC_LEFT=()
  if [ "$EVICT_TO" = "$GLACIER" ]; then log "SKIP evict: EVICT_TO is the glacier disk"; return 0; fi
  if glacier_idle; then
    EVICT_OK=1
    log "evict: $GLACIER is spun down and nothing read it since the last run: nothing on it is hot, skipped (looking would wake it)"
    return 0
  fi
  if ! share_excludes_glacier; then
    if [ "$APPLY" = 1 ]; then
      log "SKIP evict: $GLACIER is not excluded from the $EVICT_SHARE share ($SHARE_CFG), so new files would land on it again"
      return 0
    fi
    log "WARN  evict: $GLACIER is not excluded from the $EVICT_SHARE share ($SHARE_CFG): a live run would skip evict"
  fi
  read -r total used <<<"$(fs_stats "$EVICT_TO")"
  if [ -z "${total:-}" ] || [ "${total:-0}" -le 0 ]; then log "SKIP evict: cannot read free space of $EVICT_TO"; return 0; fi
  EV_RAN=1
  ROOM_LEFT=$((total * EVICT_DEST_PCT / 100 - used))
  log "evict $GLACIER -> $EVICT_TO: $EVICT_TO used=$(hr "$used") of $(hr "$total") limit=${EVICT_DEST_PCT}% room=$(hr "$((ROOM_LEFT > 0 ? ROOM_LEFT : 0))") cap left=$(hr "$((CAP_LEFT > 0 ? CAP_LEFT : 0))")"
  if [ "$ROOM_LEFT" -le 0 ]; then log "evict: $EVICT_TO is at or above ${EVICT_DEST_PCT}%: nothing to evict"; return 0; fi
  build_evict_candidates "$TMP/cands"
  plan_and_run
  [ "$STOP" = 1 ] || EVICT_OK=1
}

# phase_fill: move the coldest media from SRC_DISKS to GLACIER
phase_fill() {
  local total used target
  PHASE=fill; DEST="$GLACIER"; GATE_KEEP=1
  read -r total used <<<"$(fs_stats "$GLACIER")"
  if [ -z "${total:-}" ] || [ "${total:-0}" -le 0 ]; then log "SKIP fill: cannot read free space of $GLACIER"; return 0; fi
  if [ "$APPLY" != 1 ] && [ "$EV_B" -gt 0 ]; then
    used=$((used - EV_B))   # a dry run moved nothing: count the room evict would free
    log "fill: counting the $(hr "$EV_B") evict would free on $GLACIER"
  fi
  target=$((total * TARGET_PCT / 100))
  ROOM_LEFT=$((target - used))
  log "fill -> $GLACIER: used=$(hr "$used") of $(hr "$total") target=${TARGET_PCT}% room=$(hr "$((ROOM_LEFT > 0 ? ROOM_LEFT : 0))") cap left=$(hr "$((CAP_LEFT > 0 ? CAP_LEFT : 0))")"
  if [ "$ROOM_LEFT" -le 0 ]; then log "$GLACIER is already at or above ${TARGET_PCT}%: nothing to do"; return 0; fi
  if [ "$CAP_LEFT" -le 0 ]; then log "fill: tonight's cap is used up"; return 0; fi
  local srcs="$SRC_DISKS" d dt du
  SRC_LEFT=()
  if [ "$FILL_START_PCT" -gt 0 ]; then
    srcs=""
    for d in $SRC_DISKS; do
      read -r dt du <<<"$(fs_stats "$d")"
      if [ -z "${dt:-}" ] || [ "${dt:-0}" -le 0 ]; then log "fill: cannot read free space of $d, left out"; continue; fi
      [ "$APPLY" != 1 ] && [ "$d" = "$EVICT_TO" ] && du=$((du + EV_B))   # a dry run's evict would land here
      if [ $((du * 100)) -gt $((dt * FILL_START_PCT)) ]; then
        srcs="$srcs $d"; SRC_LEFT[$d]=$((du - dt * FILL_STOP_PCT / 100))
        log "fill: $d is at $((du * 100 / dt))% (over the ${FILL_START_PCT}% start line): moving up to $(hr "${SRC_LEFT[$d]}") off it, down to ${FILL_STOP_PCT}%"
      else
        log "fill: $d is at $((du * 100 / dt))%, under the ${FILL_START_PCT}% start line: left as is"
      fi
    done
    if [ -z "$srcs" ]; then log "fill: no source disk is over ${FILL_START_PCT}%: nothing to fill tonight"; return 0; fi
  fi
  SRC_DISKS="$srcs" build_candidates "$TMP/cands"
  plan_and_run
}

main() {
  local a ph total used
  for a in "$@"; do
    case "$a" in
      --apply) APPLY=1 ;;
      --dry-run) APPLY=0 ;;
      -h|--help) printf 'usage: %s [--apply]\nDry run unless --apply. See tiering/README.md.\n' "${0##*/}"; return 0 ;;
      *) printf 'unknown argument: %s\n' "$a" >&2; return 2 ;;
    esac
  done
  preflight || return 2
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then log "ABORT: another glacier run holds the lock"; return 2; fi
  TMP="$(mktemp -d)"
  STATE_OK=0; [ "$APPLY" = 1 ] && state_dir_ok && STATE_OK=1
  trap 'resume_unit; turbo_off; write_status "$EXIT_STATE" "${EXIT_REASON:+reason=$EXIT_REASON}"; rm -rf "$TMP"' EXIT
  turbo_restore_stale
  trap 'EXIT_STATE=stopped EXIT_REASON=signal; exit 130' INT; trap 'EXIT_STATE=stopped EXIT_REASON=signal; exit 143' TERM
  write_status start
  : >"$TMP/plan"; : >"$TMP/reject"; : >"$TMP/skipped-groups"; : >"$TMP/evict-blocked"
  read -r total used <<<"$(fs_stats "$GLACIER")"
  if [ -z "${total:-}" ] || [ "${total:-0}" -le 0 ]; then log "ABORT: cannot read free space of $GLACIER"; return 2; fi
  CAP_LEFT=$NIGHTLY_CAP_BYTES
  log "start mode=$([ "$APPLY" = 1 ] && echo apply || echo dry-run) phases=$PHASES $GLACIER used=$(hr "$used") of $(hr "$total") cap=$(hr "$CAP_LEFT") window=${MOVE_WINDOW:-any}"
  if [ "$APPLY" = 1 ] && ! in_window; then
    WINDOW_CLOSED=1; log "outside the move window $MOVE_WINDOW (local time now $(date '+%H:%M %Z')): nothing moves this run"
  fi
  [ "$WINDOW_CLOSED" = 1 ] || wait_mover
  for ph in $PHASES; do
    [ "$WINDOW_CLOSED" = 1 ] && break
    STOP=0; MISFITS=0
    case "$ph" in
      evict) phase_evict ;;
      fill) phase_fill ;;
      *) log "WARN  unknown phase '$ph', ignored" ;;
    esac
  done
  note_reads
  report
  if [ "$N_FAILED" -gt 0 ]; then EXIT_STATE=failed; EXIT_REASON=copy
  elif [ "$WINDOW_CLOSED" = 1 ]; then EXIT_STATE=stopped; EXIT_REASON=window
  else EXIT_STATE=done; fi
  [ "$N_FAILED" -gt 0 ] && return 1
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
  exit $?
fi
