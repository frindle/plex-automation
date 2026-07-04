#!/bin/bash
# Regenerate the cleanup checklist covering missing movies, season packs,
# quality-upgrade candidates, and download↔media matches.
#
# Run from anywhere; writes the result next to update.sh in this folder.
# Override the output location or webhook URL via env vars if needed:
#   OUT=/tmp/mycheck.txt WEBHOOK=http://arr:9876 ./checklist.sh
set -euo pipefail

WEBHOOK="${WEBHOOK:-http://10.0.0.4:9876}"
DOWNLOADS="${DOWNLOADS:-/mnt/user/data/Downloads}"
MEDIA="${MEDIA:-/mnt/user/data/Media}"
OUT="${OUT:-/mnt/user/appdata/plex-automation/cleanup-checklist.txt}"

find "$DOWNLOADS" -type f -printf '%f\t%s\t%p\n' 2>/dev/null | sort > /tmp/downloads.tsv
find "$MEDIA"     -type f -printf '%f\t%s\t%p\n' 2>/dev/null | sort > /tmp/media.tsv

{
  echo "═══════════════════════════════════════════════════════════════"
  echo "  PLEX / ARR CLEANUP CHECKLIST — generated $(date +%F' '%H:%M)"
  echo "═══════════════════════════════════════════════════════════════"
  echo
  echo "  Storage snapshot:"
  echo "    Downloads:  $(du -sh "$DOWNLOADS" 2>/dev/null | cut -f1)"
  echo "    Media:      $(du -sh "$MEDIA"     2>/dev/null | cut -f1)"
  echo
  echo
  echo "───────────────────────────────────────────────────────────────"
  echo "  1. MISSING MOVIES (monitored, hasFile=false)"
  echo "───────────────────────────────────────────────────────────────"
  echo
  curl -sf "$WEBHOOK/missing-movies?sort=digital" \
    | jq -r '.movies[] | [.id, (.year|tostring // "?"), .title, (.digital_release // "" | .[0:10]), (.status // "")] | @tsv' \
    | awk -F'\t' '{printf "[ ] id=%-5s (%s)  %-45.45s  digital=%-10s  %s\n",$1,$2,$3,$4,$5}'
  echo
  echo
  echo "───────────────────────────────────────────────────────────────"
  echo "  2. SEASON PACKS TO FIND (sorted by fragmentation)"
  echo "     FRAG = distinct releases making up the season"
  echo "───────────────────────────────────────────────────────────────"
  echo
  curl -sf "$WEBHOOK/missing-season-packs" \
    | jq -r '.seasons | sort_by(-.distinct_releases) | .[] | [.distinct_releases, .series_id, .season, .series_title] | @tsv' \
    | awk -F'\t' '{printf "[ ] frag=%-3s  id=%-5s  S%02d  %s\n",$1,$2,$3,$4}'
  echo
  echo
  echo "───────────────────────────────────────────────────────────────"
  echo "  3. QUALITY UPGRADE CANDIDATES (missing surround + HDR + x265)"
  echo "───────────────────────────────────────────────────────────────"
  echo
  curl -sf "$WEBHOOK/quality-audit" \
    | jq -r '.movies | sort_by(.title|ascii_downcase) | .[] | [.id, (.year|tostring), .title, .file] | @tsv' \
    | awk -F'\t' '{printf "[ ] id=%-5s (%s)  %-40.40s  →  %s\n",$1,$2,$3,$4}'
  echo
  echo
  echo "───────────────────────────────────────────────────────────────"
  echo "  4. DOWNLOAD ↔ MEDIA MATCHES (mostly hardlinks; review only)"
  echo "───────────────────────────────────────────────────────────────"
  echo
  awk -F'\t' 'FNR==NR{k=$1"|"$2; a[k]=$3; next} {k=$1"|"$2; if(k in a) print $2"\t"a[k]"\t"$3}' \
    /tmp/downloads.tsv /tmp/media.tsv \
    | awk -F'\t' '{gb=$1/1073741824; printf "[ ] %6.2fG  %s  ↔  %s\n",gb,$2,$3}'
} > "$OUT"

wc -l "$OUT"
echo "→ $OUT"
