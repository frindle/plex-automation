#!/bin/bash
# Tests for tiering/glacier-mover.sh against a fake /mnt tree (cache, disk1..3).
# Run: bash tiering/tests/test_glacier.sh    (run it in claude-sandbox, never on the Mac)
# Nothing here touches real shares: every path lives under a mktemp ROOT.

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/glacier-mover.sh"
PASS=0; FAIL=0; CUR=""
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT
NOW=$(date +%s); DAY=86400

ok()   { PASS=$((PASS + 1)); }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL [$CUR] $*"; }
check() { # desc cmd...
  local d="$1"; shift
  if "$@" >/dev/null 2>&1; then ok; else bad "$d"; fi
}
eq() { [ "$2" = "$3" ] && ok || bad "$1: got '$2' want '$3'"; }

setup() { # name
  CUR="$1"; echo "- $1"
  R="$WORK/$1"; rm -rf "$R"; mkdir -p "$R/mnt"/{cache,disk1,disk2,disk3} "$R/bin"
  M="$R/mnt"; CAP=10000; export R M
  cat >"$R/ovr.sh" <<'EOF'
fs_stats() { echo "$CAP $(find "$M/$1" -type f -printf '%s\n' | awk '{s+=$1}END{print s+0}')"; }
gate_call() { cat >/dev/null; : >"$R/gate.flag.$1"; }          # default: gate answers nothing -> overridden per test
EOF
  # default gate: every path is free
  cat >>"$R/ovr.sh" <<'EOF'
gate_call() { echo gate >>"$R/gate.calls"; while IFS= read -r p; do echo "$p" >>"$R/gate.paths"; printf 'free\tno torrent\t%s\n' "$p"; done; }
pause_call() { local n=0 p; while IFS= read -r p; do [ -n "$p" ] || continue; echo "$p" >>"$R/pause.paths"; n=$((n+1)); printf 'paused\th%s\n' "$(basename "$p")"; done; echo pause >>"$R/pause.calls"; }
resume_call() { while IFS= read -r h; do echo "$h" >>"$R/resume.hashes"; done; echo resume >>"$R/resume.calls"; }
EOF
  : >"$R/notify.log"
  printf '#!/bin/sh\necho "$@" >>"%s/notify.log"\n' "$R" >"$R/bin/notify"; chmod +x "$R/bin/notify"
}

# mkf DISK REL BYTES ATIME_DAYS_AGO [MODE]  -> file, mtime 2 days old, atime N days ago
mkf() {
  local f="$M/$1/$2"; mkdir -p "$(dirname "$f")"
  head -c "$3" /dev/urandom >"$f"; chmod "${5:-644}" "$f"
  touch -m -d "@$((NOW - 2 * DAY))" "$f"; touch -a -d "@$((NOW - $4 * DAY))" "$f"
}

run() { # [extra extras] ; env: EXTRA_OVR (code), flags in $FLAGS
  env SCRIPT="$SCRIPT" ROOT="$R" CAP="$CAP" R="$R" M="$M" LOG_FILE="$R/glacier.log" LOG_DIR="$R" LOCK_FILE="$R/lock" \
      NOTIFY_CMD="$R/bin/notify" MIN_AGE_DAYS="${MIN_AGE_DAYS:-0}" PAUSE_SETTLE_SEC=0 ${ENVX:-} \
      bash -c 'source "$SCRIPT"; source "$R/ovr.sh"; eval "$EXTRA_OVR"; main "$@"' harness "$@" 2>&1
}
EXTRA_OVR=""; export EXTRA_OVR

onglacier() { [ -f "$M/disk1/$1" ]; }
gone()      { [ ! -e "$M/disk1/$1" ]; }
src()       { [ -f "$M/$1/$2" ]; }

# ---------------------------------------------------------------------------
setup atime_ranking_and_fill
for i in 01 02 03 04 05 06 07 08 09 10 11 12; do          # 01 = oldest atime, 12 = newest
  mkf disk$((10#$i % 2 + 2)) "data/Media/f$i.mkv" 1000 $((200 - 10#$i))
done
out="$(run --apply)"; rc=$?
eq "exit code" "$rc" 0
for i in 01 02 03 04 05 06 07 08 09; do check "oldest f$i moved" onglacier "data/Media/f$i.mkv"; done
for i in 10 11 12; do check "newest f$i stays" gone "data/Media/f$i.mkv"; done
used=$(find "$M/disk1" -type f -printf '%s\n' | awk '{s+=$1}END{print s}')
check "disk1 stops at or below 98% (9800)" test "$used" -le 9800
echo "$out" | grep -q 'moved=9 ' && ok || bad "summary says moved=9: $out"
src disk2 data/Media/f10.mkv && ok || bad "f10 on disk2 remains"

setup fill_stop_uses_existing_usage
mkf disk1 data/Media/existing.mkv 8000 1000
mkf disk2 data/Media/a.mkv 1000 100
mkf disk2 data/Media/b.mkv 1000 90
mkf disk3 data/Media/c.mkv 500 80
run --apply >/dev/null
check "a fits (oldest)" onglacier data/Media/a.mkv
check "b does not fit the 98% line" gone data/Media/b.mkv
check "smaller c fits the remaining room" onglacier data/Media/c.mkv
used=$(find "$M/disk1" -type f -printf '%s\n' | awk '{s+=$1}END{print s}')
eq "disk1 used 9500" "$used" 9500

setup already_full
mkf disk1 data/Media/existing.mkv 9900 1000
mkf disk2 data/Media/a.mkv 100 100
out="$(run --apply)"
check "nothing moved when glacier is past 98%" gone data/Media/a.mkv
echo "$out" | grep -q 'nothing to do' && ok || bad "says nothing to do"

setup nightly_cap
for i in 1 2 3 4 5; do mkf disk2 "data/Media/f$i.mkv" 1000 $((100 - i)); done
ENVX="NIGHTLY_CAP_BYTES=3000" run --apply >/dev/null
n=$(find "$M/disk1" -type f | wc -l); eq "cap 3000 bytes -> 3 files" "$n" 3
check "oldest three went" onglacier data/Media/f1.mkv
check "fourth stayed" gone data/Media/f4.mkv

setup hardlink_groups
mkf disk2 data/Media/h/a.mkv 1000 100
ln "$M/disk2/data/Media/h/a.mkv" "$M/disk2/data/Media/h/b.mkv"
# group with a member in Downloads: must stay whole
mkf disk2 data/Media/g/x.mkv 1000 90
mkdir -p "$M/disk2/data/Downloads/Complete"; ln "$M/disk2/data/Media/g/x.mkv" "$M/disk2/data/Downloads/Complete/x.mkv"
# group with a member in another share
mkf disk2 data/Media/k/y.mkv 1000 80
mkdir -p "$M/disk2/other"; ln "$M/disk2/data/Media/k/y.mkv" "$M/disk2/other/y.mkv"
run --apply >/dev/null
check "group a/b moved" onglacier data/Media/h/a.mkv
check "group a/b moved (b)" onglacier data/Media/h/b.mkv
ia=$(stat -c %i "$M/disk1/data/Media/h/a.mkv"); ib=$(stat -c %i "$M/disk1/data/Media/h/b.mkv")
eq "still hardlinked on glacier" "$ia" "$ib"
check "a/b removed from disk2" test ! -e "$M/disk2/data/Media/h/a.mkv" -a ! -e "$M/disk2/data/Media/h/b.mkv"
check "downloads-linked group untouched" src disk2 data/Media/g/x.mkv
check "downloads link untouched" src disk2 data/Downloads/Complete/x.mkv
check "other-share-linked group untouched" src disk2 data/Media/k/y.mkv
check "no half group on glacier" gone data/Media/g/x.mkv

setup open_file_skipped
mkf disk2 data/Media/open.mkv 1000 100
mkf disk2 data/Media/closed.mkv 1000 90
sleep 60 <"$M/disk2/data/Media/open.mkv" & holder=$!
sleep 0.3
out="$(run --apply)"
kill $holder 2>/dev/null
check "open file stays" src disk2 data/Media/open.mkv
check "open file not on glacier" gone data/Media/open.mkv
check "closed file moved" onglacier data/Media/closed.mkv
echo "$out" | grep -q 'file is open' && ok || bad "logs the open-file skip"

setup verify_failure_keeps_source
mkf disk2 data/Media/a.mkv 1000 100
mkf disk2 data/Media/b.mkv 1000 90
EXTRA_OVR='files_match() { return 1; }'
out="$(run --apply)"; rc=$?
unset EXTRA_OVR; EXTRA_OVR=""; export EXTRA_OVR
eq "exit code 1 on failure" "$rc" 1
check "source a intact" src disk2 data/Media/a.mkv
check "source b intact" src disk2 data/Media/b.mkv
check "no bad copy left on glacier (a)" gone data/Media/a.mkv
check "no bad copy left on glacier (b)" gone data/Media/b.mkv
echo "$out" | grep -q 'failed=2' && ok || bad "summary says failed=2: $out"
grep -q 'warning' "$R/notify.log" && ok || bad "notify uses warning importance on failures"

setup rsync_failure_keeps_source
mkf disk2 data/Media/a.mkv 1000 100
EXTRA_OVR='rsync() { return 23; }'
run --apply >/dev/null; rc=$?
EXTRA_OVR=""; export EXTRA_OVR
eq "exit code 1" "$rc" 1
check "source intact" src disk2 data/Media/a.mkv

setup size_mismatch_keeps_source
mkf disk2 data/Media/a.mkv 1000 100
EXTRA_OVR='rsync() { command rsync "$@"; local d="${@: -1}"; truncate -s 10 "$d/data/Media/a.mkv"; }'
run --apply >/dev/null; rc=$?
EXTRA_OVR=""; export EXTRA_OVR
eq "exit code 1" "$rc" 1
check "source intact after short copy" src disk2 data/Media/a.mkv
check "short copy removed" gone data/Media/a.mkv

setup skip_list
NEVER="Downloads Incomplete InfluxDB Docker Documents Sandbox ollama-models appdata system domains Vault bloom-ledger GC timemachine-penn resell-tracker .hidden"
for d in $NEVER; do
  mkf disk2 "data/$d/x.mkv" 1000 300
  mkf disk2 "data/Media/deep/$d/y.mkv" 1000 300
done
mkf disk2 appdata/plex/z.mkv 1000 300
mkf disk2 data/Media/ok.mkv 1000 100
run --apply >/dev/null
check "ordinary file moved" onglacier data/Media/ok.mkv
for d in $NEVER; do
  check "$d top-level not moved" src disk2 "data/$d/x.mkv"
  check "$d under media root not moved" src disk2 "data/Media/deep/$d/y.mkv"
done
check "other share not moved" src disk2 appdata/plex/z.mkv
n=$(find "$M/disk1" -type f | wc -l); eq "only one file on glacier" "$n" 1

setup allowlist_extensions_and_roots
mkf disk2 data/Media/Movies/Film/film.mkv 1000 100
mkf disk2 data/Media/Music/Album/t.flac 500 100
mkf disk2 data/Media/Books/b.m4b 500 100
mkf disk2 data/Media/Movies/Film/notes.txt 100 100
mkf disk2 data/Media/Movies/Film/cover.jpg 100 100
mkf disk2 data/Media/Movies/Film/run.sh 100 100
mkf disk2 data/Other/loose.mkv 1000 100                   # media ext but outside the roots
mkf disk3 Plex/lib/p.mkv 1000 100                         # Plex share is not a default root
out="$(run --apply)"
check "video moved" onglacier data/Media/Movies/Film/film.mkv
check "audio moved" onglacier data/Media/Music/Album/t.flac
check "audiobook moved" onglacier data/Media/Books/b.m4b
for f in notes.txt cover.jpg run.sh; do check "$f (not media) stays" src disk2 data/Media/Movies/Film/$f; done
check "media outside roots stays" src disk2 data/Other/loose.mkv
check "Plex share stays" src disk3 Plex/lib/p.mkv
echo "$out" | grep -q 'rejected files by rule' && ok || bad "prints reject counts"
echo "$out" | grep -Eq '3 +not a media file' && ok || bad "counts 3 non-media rejects: $out"

setup custom_roots_list
mkf disk2 data/Media/a.mkv 1000 100
mkf disk3 Plex/lib/p.mkv 1000 100
ENVX="MEDIA_ROOTS=Plex/lib" run --apply >/dev/null
check "Plex root now allowed" onglacier Plex/lib/p.mkv
check "data/Media no longer a root" src disk2 data/Media/a.mkv

setup sidecars_move_with_main
mkf disk2 data/Media/Show/ep1.mkv 1000 100
mkf disk2 data/Media/Show/ep1.en.srt 50 100
mkf disk2 data/Media/Show/ep1.nfo 20 100
mkf disk2 data/Media/Show/ep2.mkv 1000 90
mkf disk2 data/Media/Show/orphan.srt 10 100
mkf disk2 data/Media/Show/tvshow.nfo 10 100
out="$(run --apply)"
for f in ep1.mkv ep1.en.srt ep1.nfo ep2.mkv; do check "$f moved" onglacier data/Media/Show/$f; done
check "orphan sidecar stays" src disk2 data/Media/Show/orphan.srt
check "show-level nfo stays" src disk2 data/Media/Show/tvshow.nfo
echo "$out" | grep -Eq '2 +sidecar without a main file' && ok || bad "counts orphan sidecars"

setup sidecars_follow_main_when_main_blocked
mkf disk2 data/Media/Show/ep1.mkv 1000 100
mkf disk2 data/Media/Show/ep1.en.srt 50 100
touch -m -d "@$((NOW - 2 * DAY))" "$M/disk2/data/Media/Show/ep1.mkv"
sleep 60 <"$M/disk2/data/Media/Show/ep1.mkv" & holder=$!
sleep 0.3
run --apply >/dev/null
kill $holder 2>/dev/null
check "open main stays" src disk2 data/Media/Show/ep1.mkv
check "its sidecar stays too" src disk2 data/Media/Show/ep1.en.srt

setup never_move_classes
mkf disk2 data/Media/lib/Backup.sparsebundle/band.mkv 1000 300
mkf disk2 data/Media/lib/a.db 1000 300
mkf disk2 data/Media/lib/a.sqlite 1000 300
mkf disk2 data/Media/lib/a.sqlite3 1000 300
mkf disk2 data/Media/lib/a.tsm 1000 300
mkf disk2 data/Media/lib/a.wal 1000 300
mkf disk2 data/Media/lib/a.db-wal 1000 300
mkf disk2 data/Media/lib/vm.img 1000 300
mkf disk2 data/Media/lib/vm.qcow2 1000 300
mkf disk2 data/Media/lib/vm.vdisk 1000 300
mkf disk2 data/Media/lib/.hiddenfile.mkv 1000 300
mkf disk2 data/Media/lib/.cache/h.mkv 1000 300
mkf disk2 data/Media/lib/ok.mkv 1000 300
out="$(run --apply)"
check "plain media moved" onglacier data/Media/lib/ok.mkv
n=$(find "$M/disk1" -type f | wc -l); eq "only the plain file moved" "$n" 1
echo "$out" | grep -Eq '1 +sparsebundle' && ok || bad "counts sparsebundle"
echo "$out" | grep -Eq '[0-9]+ +database or VM disk file' && ok || bad "counts db/vm"
echo "$out" | grep -Eq '2 +hidden folder or file' && ok || bad "counts hidden"

setup recent_mtime_30_days
mkf disk2 data/Media/old.mkv 1000 100
mkf disk2 data/Media/month.mkv 1000 100
mkf disk2 data/Media/new.mkv 1000 100
touch -m -d "@$((NOW - 40 * DAY))" "$M/disk2/data/Media/old.mkv"
touch -m -d "@$((NOW - 29 * DAY))" "$M/disk2/data/Media/month.mkv"
touch -m -d "@$((NOW - 60))" "$M/disk2/data/Media/new.mkv"
out="$(MIN_AGE_DAYS=30 run --apply)"
check "40 day old file moved" onglacier data/Media/old.mkv
check "29 day old file stays" src disk2 data/Media/month.mkv
check "a minute old file stays" src disk2 data/Media/new.mkv
echo "$out" | grep -Eq '2 +modified within 30 days' && ok || bad "counts recent mtime: $out"
grep -q 'MIN_AGE_DAYS:-30' "$SCRIPT" && ok || bad "default is 30 days"

setup open_recheck_before_copy
mkf disk2 data/Media/a.mkv 1000 100
EXTRA_OVR='is_open() { local n; n=$(cat "$R/open.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n >"$R/open.n"; [ "$n" -ge 2 ]; }'
out="$(run --apply)"; EXTRA_OVR=""; export EXTRA_OVR
check "opened after planning: stays" src disk2 data/Media/a.mkv
check "never copied" gone data/Media/a.mkv
echo "$out" | grep -q 're-checked before copy' && ok || bad "logs the pre-copy recheck"

setup open_recheck_before_delete
mkf disk2 data/Media/a.mkv 1000 100
EXTRA_OVR='is_open() { local n; n=$(cat "$R/open.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n >"$R/open.n"; [ "$n" -ge 3 ]; }'
out="$(run --apply)"; EXTRA_OVR=""; export EXTRA_OVR
check "opened during copy: source kept" src disk2 data/Media/a.mkv
check "copy removed" gone data/Media/a.mkv
echo "$out" | grep -q 're-checked before delete' && ok || bad "logs the pre-delete recheck"

setup dry_run_vs_apply
mkf disk2 data/Media/Show/S01/a.mkv 3000 100
mkf disk3 data/Media/Movie/b.mkv 2000 90
out="$(run)"; rc=$?                                 # no flag = dry run
eq "dry exit" "$rc" 0
check "dry run moved nothing (a)" src disk2 data/Media/Show/S01/a.mkv
check "dry run moved nothing (b)" src disk3 data/Media/Movie/b.mkv
n=$(find "$M/disk1" -type f | wc -l); eq "glacier empty after dry run" "$n" 0
echo "$out" | grep -q 'WOULD MOVE' && ok || bad "prints move list"
echo "$out" | grep -q 'per-disk  disk2 -> disk1: 1 file(s)' && ok || bad "per-disk count disk2: $out"
echo "$out" | grep -q 'per-disk  disk3 -> disk1: 1 file(s)' && ok || bad "per-disk count disk3"
echo "$out" | grep -q 'top folders by bytes' && ok || bad "top folders heading"
echo "$out" | grep -q 'data/Media/Show' && ok || bad "top folders lists a folder"
check "dry run sent no notification" test ! -s "$R/notify.log"
grep -q gate "$R/gate.calls" && ok || bad "dry run still classifies through the gate"
check "dry run makes no pause call" test ! -e "$R/pause.calls"
check "dry run makes no resume call" test ! -e "$R/resume.calls"
out="$(run --apply)"
check "apply moved a" onglacier data/Media/Show/S01/a.mkv
check "apply moved b" onglacier data/Media/Movie/b.mkv
check "apply gone from source" test ! -e "$M/disk2/data/Media/Show/S01/a.mkv"
eq "one notification" "$(wc -l <"$R/notify.log")" 1
grep -q 'moved 2, failed 0, skipped 0' "$R/notify.log" && ok || bad "notify subject counts: $(cat "$R/notify.log")"
check "free files are never paused" test ! -e "$R/pause.calls"

setup top_20_folders
for i in $(seq 1 25); do mkf disk2 "data/Media/Show$i/S01/e.mkv" $((100 + i)) 100; done
out="$(run)"
nlines=$(echo "$out" | sed -n '/top folders by bytes/,/totals:/p' | grep -c 'file(s)')
eq "top folders capped at 20" "$nlines" 20

setup metadata_preserved
mkf disk2 data/Media/m.mkv 1000 100 640
mt=$(stat -c %Y "$M/disk2/data/Media/m.mkv"); at=$(stat -c %X "$M/disk2/data/Media/m.mkv")
XA=0   # user xattr via python3 (the sandbox has no setfattr); skipped if the fs refuses it
python3 -c 'import os,sys; os.setxattr(sys.argv[1], "user.tst", b"hello")' "$M/disk2/data/Media/m.mkv" 2>/dev/null && XA=1
run --apply >/dev/null
f="$M/disk1/data/Media/m.mkv"
eq "mode kept" "$(stat -c %a "$f")" 640
eq "mtime kept" "$(stat -c %Y "$f")" "$mt"
eq "atime kept" "$(stat -c %X "$f")" "$at"
if [ "$XA" = 1 ]; then eq "xattr kept" "$(python3 -c 'import os,sys; print(os.getxattr(sys.argv[1], "user.tst").decode())' "$f" 2>&1)" hello; else echo "  (xattr check skipped: fs refuses user xattrs)"; fi

setup destination_exists_not_overwritten
mkf disk2 data/Media/a.mkv 1000 100
mkf disk1 data/Media/a.mkv 500 5
before=$(md5sum <"$M/disk1/data/Media/a.mkv")
out="$(run --apply)"
eq "dest untouched" "$(md5sum <"$M/disk1/data/Media/a.mkv")" "$before"
check "source kept" src disk2 data/Media/a.mkv
echo "$out" | grep -q 'destination already exists' && ok || bad "logged skip"

GATE_PAUSE_OVR='gate_call() { echo gate >>"$R/gate.calls"; while IFS= read -r p; do echo "$p" >>"$R/gate.paths"; case "$p" in
  */pz/*) printf "pause\tcomplete\t%s\n" "$p";; */seed/*) printf "wait\ttorrent not complete\t%s\n" "$p";; *) printf "free\tno torrent\t%s\n" "$p";; esac; done; }'

setup torrent_gate
mkf disk2 data/Media/free/a.mkv 1000 100
mkf disk2 data/Media/pz/b.mkv 1000 99
mkf disk2 data/Media/seed/c.mkv 1000 98
EXTRA_OVR="$GATE_PAUSE_OVR"
out="$(run --apply)"
EXTRA_OVR=""; export EXTRA_OVR
check "free file moved" onglacier data/Media/free/a.mkv
check "paused-owner file moved" onglacier data/Media/pz/b.mkv
check "incomplete torrent waits" src disk2 data/Media/seed/c.mkv
check "incomplete torrent not on glacier" gone data/Media/seed/c.mkv
grep -qx '/data/Media/seed/c.mkv' "$R/gate.paths" && ok || bad "gate gets Deluge-namespace paths: $(cat "$R/gate.paths")"
eq "one pause call (only the pause unit)" "$(wc -l <"$R/pause.calls")" 1
grep -qx '/data/Media/pz/b.mkv' "$R/pause.paths" && ok || bad "pause gets the unit's Deluge paths"
eq "resumed exactly what it paused" "$(cat "$R/resume.hashes")" "hb.mkv"
echo "$out" | grep -q 'torrents-paused=1' && ok || bad "counts paused torrent"
echo "$out" | grep -q 'skipped=1' && ok || bad "counts the waiting file as skipped"

setup pause_order_pause_before_move_resume_after
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
pause_call() { cat >/dev/null; echo "pause src_exists=$([ -f "$M/disk2/data/Media/pz/b.mkv" ] && echo 1 || echo 0)" >>"$R/order"; printf "paused\thX\n"; }
resume_call() { cat >/dev/null; echo "resume src_exists=$([ -f "$M/disk2/data/Media/pz/b.mkv" ] && echo 1 || echo 0) dst=$([ -f "$M/disk1/data/Media/pz/b.mkv" ] && echo 1 || echo 0)" >>"$R/order"; }'
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
eq "pause before the move, resume after it" "$(tr '\n' '|' <"$R/order")" "pause src_exists=1|resume src_exists=0 dst=1|"

setup pause_foreign_hash_not_resumed
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
pause_call() { cat >/dev/null; printf "foreign\thUSER\n"; }
resume_call() { cat >"$R/resumed.body"; echo r >>"$R/resume.calls"; }'
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
check "file still moves when the owner was paused by the user" onglacier data/Media/pz/b.mkv
check "foreign-paused torrent is never resumed" test ! -e "$R/resume.calls"

setup pause_resume_on_move_failure
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
files_match() { return 1; }'
run --apply >/dev/null; rc=$?
EXTRA_OVR=""; export EXTRA_OVR
eq "failure exit code" "$rc" 1
check "source kept" src disk2 data/Media/pz/b.mkv
eq "resumed after a failed move" "$(cat "$R/resume.hashes")" "hb.mkv"

setup pause_resume_on_skip_open_file
mkf disk2 data/Media/pz/b.mkv 1000 99
# opened only after the pause (planning check passes, the pre-copy re-check sees the handle)
EXTRA_OVR="$GATE_PAUSE_OVR"'
is_open() { [ -e "$R/pause.calls" ]; }'
out="$(run --apply)"
EXTRA_OVR=""; export EXTRA_OVR
check "file opened after pause is not moved" src disk2 data/Media/pz/b.mkv
echo "$out" | grep -q 're-checked before copy' && ok || bad "open check re-run after pause: $out"
eq "resumed after a skipped unit" "$(cat "$R/resume.hashes")" "hb.mkv"

setup pause_resume_on_skip_destination_exists
mkf disk2 data/Media/pz/b.mkv 1000 99
mkf disk1 data/Media/pz/b.mkv 500 5
EXTRA_OVR="$GATE_PAUSE_OVR"
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
check "destination clash keeps source" src disk2 data/Media/pz/b.mkv
eq "resumed after destination-exists skip" "$(cat "$R/resume.hashes" 2>/dev/null)" "hb.mkv"

setup pause_call_failure_means_wait
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
pause_call() { cat >/dev/null; return 22; }'
out="$(run --apply)"; rc=$?
EXTRA_OVR=""; export EXTRA_OVR
eq "exit 0 (a wait is not a failure)" "$rc" 0
check "unit left in place" src disk2 data/Media/pz/b.mkv
check "nothing on glacier" gone data/Media/pz/b.mkv
check "no resume when nothing was paused" test ! -e "$R/resume.calls"
echo "$out" | grep -q 'could not pause' && ok || bad "logs the pause failure: $out"
echo "$out" | grep -q 'skipped=1' && ok || bad "counts it as skipped"

setup dry_run_reports_pause_and_never_calls_pause
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"
out="$(run)"
EXTRA_OVR=""; export EXTRA_OVR
echo "$out" | grep -q 'WOULD MOVE.*\[torrent: pause\]' && ok || bad "dry run line shows [torrent: pause]: $out"
check "dry run: no pause call" test ! -e "$R/pause.calls"
check "dry run: no resume call" test ! -e "$R/resume.calls"
check "dry run: file stays" src disk2 data/Media/pz/b.mkv

setup pause_resumed_by_exit_trap_on_term
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
rsync() { kill -TERM $$; sleep 2; }'
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
eq "trap resumed the paused torrent after SIGTERM" "$(cat "$R/resume.hashes" 2>/dev/null)" "hb.mkv"
check "source kept" src disk2 data/Media/pz/b.mkv

setup pause_resume_failure_is_retried_by_trap
mkf disk2 data/Media/pz/b.mkv 1000 99
EXTRA_OVR="$GATE_PAUSE_OVR"'
resume_call() { cat >/dev/null; echo try >>"$R/resume.tries"; return 22; }'
out="$(run --apply)"
EXTRA_OVR=""; export EXTRA_OVR
eq "resume tried per unit and again by the exit trap" "$(wc -l <"$R/resume.tries")" 2
echo "$out" | grep -q 'could not resume' && ok || bad "warns when resume fails"

setup mover_never_removes_torrents
for w in torrent-remove remove_torrent apply=1 'torrent-gate?[^"]*apply'; do grep -q "^[^#]*$w" "$SCRIPT" && bad "script mentions $w" || ok; done

setup torrent_gate_unreachable_fails_safe
mkf disk2 data/Media/a.mkv 1000 100
EXTRA_OVR='gate_call() { cat >/dev/null; return 22; }'
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
check "nothing moves when the gate is down" src disk2 data/Media/a.mkv
check "nothing on glacier" gone data/Media/a.mkv

setup torrent_gate_group_waits_if_any_member_waits
mkf disk2 data/Media/rel/a.mkv 1000 100
ln "$M/disk2/data/Media/rel/a.mkv" "$M/disk2/data/Media/seed-link.mkv"
EXTRA_OVR='gate_call() { while IFS= read -r p; do case "$p" in *seed-link*) printf "wait\tnot complete\t%s\n" "$p";; *) printf "pause\tok\t%s\n" "$p";; esac; done; }'
run --apply >/dev/null
EXTRA_OVR=""; export EXTRA_OVR
check "group not split (a)" src disk2 data/Media/rel/a.mkv
check "group not split (link)" src disk2 data/Media/seed-link.mkv

setup noatime_warns_but_works
mkf disk2 data/Media/a.mkv 1000 100
echo "/dev/md2 $M/disk2 xfs rw,noatime 0 0" >"$R/mounts"
out="$(PROC_MOUNTS="$R/mounts" run --apply)"; rc=$?
eq "exit 0" "$rc" 0
echo "$out" | grep -q 'WARN.*noatime' && ok || bad "warns about noatime"
check "still moves (ranking by date added)" onglacier data/Media/a.mkv
out="$(REQUIRE_ATIME=1 PROC_MOUNTS="$R/mounts" run --apply)"; rc=$?
eq "REQUIRE_ATIME=1 aborts" "$rc" 2

setup dry_run_prints_roots
mkf disk2 data/Media/a.mkv 1000 100
out="$(run)"
echo "$out" | grep -q 'allowlist roots: data/Media' && ok || bad "prints allowlist roots"

setup no_python_or_cmp_needed_on_host
for w in python3 python cmp b3sum; do grep -q "^[^#]*\b$w\b" "$SCRIPT" && bad "script uses $w" || ok; done

setup uses_disk_paths_only
mkf disk2 data/Media/a.mkv 1000 100
mkdir -p "$M/user"
run --apply >/dev/null
check "no /mnt/user entry created" test -z "$(ls -A "$M/user")"
! grep -q "/mnt/user" "$SCRIPT" 2>/dev/null && ok || bad "script never references /mnt/user"

echo
echo "glacier tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
