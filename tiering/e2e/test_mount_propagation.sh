#!/usr/bin/env bash
# Mount-propagation smoke test. Runs in CI (needs docker, sudo, ffmpeg, bindfs).
#
# bindfs stands in for tierfs until tierfs is merged: any FUSE mount made on the
# host after Plex starts must be visible inside the container (rslave), Plex
# must follow a library symlink into it, and swapping a real file for a symlink
# of the same size and mtime must not change the Plex part.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
WORK=${E2E_WORK:-$(mktemp -d)}
WORK=$(realpath "$WORK")
LIB=$WORK/lib; MNT=$WORK/mnt; REMOTE=$WORK/remote
NAME=${PLEX_CONTAINER:-tiering-e2e-plex}
PX="python3 $here/plexctl.py"
SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}
LOGDIR="$WORK/plexconfig/Library/Application Support/Plex Media Server/Logs"

fail() { echo "FAIL: $*" >&2; exit 1; }
rec()  { echo "RECORD: $*"; echo "- $*" >> "$SUMMARY"; }
jq_py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cleanup() {
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "--- container logs (tail)"; docker logs --tail 40 "$NAME" 2>&1 || true
    echo "--- plex log (tail)"; tail -n 60 "$LOGDIR/Plex Media Server.log" 2>/dev/null | cut -c1-300 || true
  fi
  "$here/plex_up.sh" down || true
  sudo umount "$MNT" 2>/dev/null || true
  sudo umount "$MNT" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT

mkdir -p "$LIB" "$MNT" "$REMOTE"
echo "work dir: $WORK"
echo "## Mount propagation smoke test" >> "$SUMMARY"

# 1. Media. Two movies; TV dir stays empty so the TV section can exist.
"$here/make_media.sh" "$LIB" --movies-only
mkdir -p "$LIB/TV"
M1="$LIB/Movies/Test Movie One (2001)/Test Movie One (2001).mkv"   # stays real, swapped later
M2="$LIB/Movies/Test Movie Two (2002)/Test Movie Two (2002).mkv"   # symlinked from the start
ID1=1111111111111111; ID2=2222222222222222
sha1=$(sha256sum "$M1" | cut -d' ' -f1); sha2=$(sha256sum "$M2" | cut -d' ' -f1)
size1=$(stat -c %s "$M1"); size2=$(stat -c %s "$M2")
mtime1=$(stat -c %Y "$M1")

# 2. The mount point must be a shared mount so a later FUSE mount propagates
# to the rslave bind inside the container.
sudo mount --bind "$MNT" "$MNT"
sudo mount --make-rshared "$MNT"

# 3. Start Plex FIRST, then mount.
"$here/plex_up.sh" up "$WORK" "$LIB" "$MNT"

cp -p "$M1" "$REMOTE/$ID1.mkv"
mv "$M2" "$REMOTE/$ID2.mkv"
sudo bindfs -o allow_other,ro --perms=a+rX "$REMOTE" "$MNT"
findmnt -n -o FSTYPE "$MNT" | grep -q fuse || fail "host mount is not FUSE"

# 4. The mount made after the container started is visible inside it.
docker exec "$NAME" ls -l "$MNT" >/dev/null || fail "mount dir not listable in container"
in_sha=$(docker exec "$NAME" sha256sum "$MNT/$ID2.mkv" | cut -d' ' -f1)
[ "$in_sha" = "$sha2" ] || fail "file read inside container does not match"
echo "ok: FUSE mount made after container start is visible inside it (rslave)"
rec "FUSE mount made after the container started is visible inside it via :rslave; bytes read inside the container match."

# 5. Library symlink into the mount (the offloaded layout), then scan.
ln -s "$MNT/$ID2.mkv" "$M2"
[ -L "$M2" ] || fail "M2 is not a symlink"
[ "$(stat -L -c %s "$M2")" = "$size2" ] || fail "symlink target size differs"
docker exec "$NAME" test -L "$M2" || fail "symlink not seen inside container"

$PX create-sections --movies "$LIB/Movies" --tv "$LIB/TV" >/dev/null
$PX scan >/dev/null
parts=$($PX parts)
n=$(echo "$parts" | jq_py "len(d)")
[ "$n" = 2 ] || { echo "$parts"; fail "expected 2 parts, got $n"; }
p2=$(echo "$parts" | jq_py "[p for p in d if 'Two' in p['file']][0]['key']")
echo "$parts" | jq_py "[p for p in d if 'Two' in p['file']][0]['size']" | grep -qx "$size2" || fail "Plex size mismatch (symlinked file)"
echo "$parts" | jq_py "[p for p in d if 'Two' in p['file']][0]['videoCodec']+'/'+[p for p in d if 'Two' in p['file']][0]['audioCodec']" | grep -qx 'h264/aac' || rec "codec check: Plex reports $(echo "$parts" | jq_py "[p for p in d if 'Two' in p['file']][0]['videoCodec']") (analysis may be pending)"
echo "ok: Plex scanned the library and matched the symlinked file"
rec "Plex scanned a library whose file is a symlink into the FUSE mount and created a part of the right size."

# 6. Range reads through Plex match the original bytes.
check_range() { # part_key start end
  local got exp
  got=$($PX fetch --part-key "$1" --range "$2-$3" | jq_py "d['sha256']")
  exp=$(tail -c +$(( $2 + 1 )) "$REMOTE/$ID2.mkv" | head -c $(( $3 - $2 + 1 )) | sha256sum | cut -d' ' -f1)
  [ "$got" = "$exp" ] || fail "range $2-$3 sha256 mismatch"
}
mid=$(( size2 / 2 ))
check_range "$p2" 0 1048575
check_range "$p2" "$mid" $(( mid + 1048575 ))
check_range "$p2" $(( size2 - 1048576 )) $(( size2 - 1 ))
full=$($PX fetch --part-key "$p2" | jq_py "d['sha256']")
[ "$full" = "$sha2" ] || fail "full download sha256 mismatch"
echo "ok: head/middle/tail Range reads and a full download match through Plex"
rec "Range reads (head, middle, tail) and a full download through Plex match the original sha256."

# 7. Swap a real file for a symlink with the same size and mtime.
before=$($PX parts --match "Movie One")
logf="$LOGDIR/Plex Media Server.log"
lines_before=$(wc -l < "$logf")
lib_path=$(echo "$before" | jq_py "d[0]['file']")
[ "$lib_path" = "$M1" ] || fail "unexpected Plex file path $lib_path"
ino_before=$(stat -c %i "$M1")

ln -s "$MNT/$ID1.mkv" "$M1.tierfs-tmp"
mv -T "$M1.tierfs-tmp" "$M1"
[ -L "$M1" ] || fail "swap did not produce a symlink"
[ "$(stat -L -c %s "$M1")" = "$size1" ] || fail "size changed by swap"
[ "$(stat -L -c %Y "$M1")" = "$mtime1" ] || fail "mtime changed by swap"

$PX scan >/dev/null
$PX scan --force >/dev/null
sleep 5
after=$($PX parts --match "Movie One")

for f in partId mediaId ratingKey size; do
  b=$(echo "$before" | jq_py "d[0]['$f']"); a=$(echo "$after" | jq_py "d[0]['$f']")
  [ "$a" = "$b" ] || fail "$f changed across the swap: $b -> $a"
done
echo "ok: part id, media id, rating key and size unchanged after swap"
rec "Real file replaced by a same-size, same-mtime symlink: part id, media id, rating key and size unchanged after a normal scan and a forced scan."

hb=$(echo "$before" | jq_py "d[0]['hash']"); ha=$(echo "$after" | jq_py "d[0]['hash']")
ub=$(echo "$before" | jq_py "d[0]['updatedAt']"); ua=$(echo "$after" | jq_py "d[0]['updatedAt']")
rec "part hash before=$hb after=$ha; item updatedAt before=$ub after=$ua"
an=$(tail -n +$(( lines_before + 1 )) "$logf" | grep -i 'Movie One' | grep -ciE 'analy|deep|probe|ffprobe|transcoder' || true)
rec "Plex log lines about Movie One mentioning analysis/probe after the swap: $an"
tail -n +$(( lines_before + 1 )) "$logf" | grep -i 'Movie One' | grep -iE 'analy|deep|probe|ffprobe|transcoder' | head -n 5 | sed 's/^/    /' || true

# Reading the swapped file through Plex returns the original bytes.
p1=$(echo "$after" | jq_py "d[0]['key']")
got=$($PX fetch --part-key "$p1" | jq_py "d['sha256']")
[ "$got" = "$sha1" ] || fail "swapped file download sha256 mismatch"
echo "ok: swapped file reads back identically through Plex"
rec "After the swap a full download through Plex matches the original sha256 (inode before $ino_before is gone; the path now resolves through the FUSE mount)."

echo "ALL OK"
