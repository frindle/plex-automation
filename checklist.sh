#!/bin/bash
# Regenerate a clean, checkable cleanup list — just names, one line each.
# Sections: missing movies, missing season packs, quality upgrades.
# Override output location or webhook URL via env vars:
#   OUT=/tmp/x.txt WEBHOOK=http://arr:9876 ./checklist.sh
# The app's request gate wants a bearer token. DOWNLOADS_TOKEN_CMD is a command that prints a curl config
# (`header = "Authorization: Bearer ..."`), e.g. DOWNLOADS_TOKEN_CMD="$HOME/bin/downloads-token.sh --curl" on the Mac;
# it is fed to curl on stdin (-K -), so the token is never an argument. Unset = no header.
# Note: `set -e` + `pipefail` is deliberately NOT used here. If one endpoint
# is temporarily down we still want the other sections to write. Errors get
# noted inline in the output file.
set -u

WEBHOOK="${WEBHOOK:?set WEBHOOK to the plex-automation container URL, e.g. http://<host>:9876}"
OUT="${OUT:-/mnt/user/appdata/plex-automation/cleanup-checklist.txt}"

auth_curl() {
  if [ -n "${DOWNLOADS_TOKEN_CMD:-}" ]; then
    # shellcheck disable=SC2086
    $DOWNLOADS_TOKEN_CMD | curl -K - "$@"
  else
    curl "$@"
  fi
}

section() {
  local title="$1" url="$2" filter="$3"
  echo "# $title"
  local body
  if body=$(auth_curl -s --fail-with-body "$WEBHOOK$url" 2>&1); then
    echo "$body" | jq -r "$filter" 2>/dev/null || echo "(parse error)"
  else
    echo "(fetch failed: $url)"
  fi
  echo
}

{
  section "Missing Movies" "/missing-movies?sort=digital" \
    '.movies[] | "[ ] \(.title) (\(.year // "?"))"'
  section "Season Packs to Find" "/missing-season-packs" \
    '.seasons | sort_by(-.distinct_releases) | .[] | "[ ] \(.series_title) — S\(.season | tostring | if length < 2 then "0" + . else . end)"'
  section "Quality Upgrades (missing surround + HDR + x265)" "/quality-audit" \
    '.movies | sort_by(.title|ascii_downcase) | .[] | "[ ] \(.title) (\(.year))"'
} > "$OUT"

wc -l "$OUT"
echo "→ $OUT"
