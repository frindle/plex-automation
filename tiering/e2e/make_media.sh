#!/usr/bin/env bash
# Deterministic synthetic media library in Plex naming. Needs ffmpeg.
#   make_media.sh OUT_DIR [--movies-only]
# Env: MEDIA_SECONDS (override duration for tiny test files; skips the size check)
set -euo pipefail

out=${1:?usage: make_media.sh OUT_DIR [--movies-only]}
movies_only=0
[ "${2:-}" = "--movies-only" ] && movies_only=1
override=${MEDIA_SECONDS:-}
MTIME=1700000000   # fixed mtime for every file

# 720p24 noise at about 24 Mbit/s: 24 s is about 72 MB, 18 s about 54 MB.
gen() { # path seed seconds
  local path=$1 seed=$2 secs=$3
  [ -n "$override" ] && secs=$override
  mkdir -p "$(dirname "$path")"
  ffmpeg -nostdin -v error -y -threads 1 -filter_threads 1 \
    -f lavfi -i "color=c=gray:s=1280x720:r=24:d=${secs},noise=alls=100:allf=t+u:all_seed=${seed}" \
    -f lavfi -i "anoisesrc=r=48000:a=0.5:c=white:seed=${seed}:d=${secs}" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 24M -maxrate 24M -bufsize 24M \
    -x264-params threads=1:keyint=48 \
    -c:a aac -b:a 192k -ac 2 \
    -map_metadata -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
    -f matroska "$path"
  touch -d "@$MTIME" "$path"
  if [ -z "$override" ]; then
    local mb=$(( $(stat -c %s "$path") / 1048576 ))
    if [ "$mb" -lt 40 ] || [ "$mb" -gt 120 ]; then
      echo "size out of range (40-120 MB): $path is ${mb} MB" >&2
      exit 1
    fi
  fi
}

gen "$out/Movies/Test Movie One (2001)/Test Movie One (2001).mkv" 101 24
gen "$out/Movies/Test Movie Two (2002)/Test Movie Two (2002).mkv" 102 24
if [ "$movies_only" = 0 ]; then
  for s in 1 2; do
    for e in 1 2 3 4; do
      f=$(printf "Test Show - S%02dE%02d.mkv" "$s" "$e")
      gen "$out/TV/Test Show/Season $(printf %02d "$s")/$f" $((200 + s * 10 + e)) 18
    done
  done
fi

( cd "$out" && find . -name '*.mkv' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$out/SHA256SUMS" )
cat "$out/SHA256SUMS"
