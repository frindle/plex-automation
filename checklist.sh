#!/bin/bash
# Regenerate a clean, checkable cleanup list — just names, one line each.
# Sections: missing movies, missing season packs, quality upgrades.
# Override output location or webhook URL via env vars:
#   OUT=/tmp/x.txt WEBHOOK=http://arr:9876 ./checklist.sh
set -euo pipefail

WEBHOOK="${WEBHOOK:-http://10.0.0.4:9876}"
OUT="${OUT:-/mnt/user/appdata/plex-automation/cleanup-checklist.txt}"

{
  echo "# Missing Movies"
  curl -sf "$WEBHOOK/missing-movies?sort=digital" \
    | jq -r '.movies[] | "[ ] \(.title) (\(.year // "?"))"'
  echo
  echo "# Season Packs to Find"
  curl -sf "$WEBHOOK/missing-season-packs" \
    | jq -r '.seasons | sort_by(-.distinct_releases) | .[] | "[ ] \(.series_title) — S\(.season | tostring | if length < 2 then "0" + . else . end)"'
  echo
  echo "# Quality Upgrades (missing surround + HDR + x265)"
  curl -sf "$WEBHOOK/quality-audit" \
    | jq -r '.movies | sort_by(.title|ascii_downcase) | .[] | "[ ] \(.title) (\(.year))"'
} > "$OUT"

wc -l "$OUT"
echo "→ $OUT"
