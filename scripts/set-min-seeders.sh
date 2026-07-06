#!/bin/sh
# Set Minimum Seeders on every torrent indexer in Radarr and Sonarr.
#
# Why: Radarr was grabbing 1-seeder torrents that stall or supersede
# immediately — part of the importBlocked/dupe-wall pattern. A floor of
# 5 seeders (override with MIN_SEEDERS) avoids grabbing dead releases.
#
# Idempotent — safe to re-run any time, e.g. after adding a new indexer.
#
# Usage:
#   RADARR_API_KEY=xxx SONARR_API_KEY=yyy ./set-min-seeders.sh
#   MIN_SEEDERS=3 RADARR_URL=http://10.0.0.7:7878 ... ./set-min-seeders.sh
#
# Requires: curl, jq

set -eu

MIN_SEEDERS="${MIN_SEEDERS:-5}"
RADARR_URL="${RADARR_URL:-http://10.0.0.7:7878}"
SONARR_URL="${SONARR_URL:-http://10.0.0.8:8989}"
RADARR_API_KEY="${RADARR_API_KEY:-}"
SONARR_API_KEY="${SONARR_API_KEY:-}"

update_indexers() {
  name="$1"; base="$2"; key="$3"
  if [ -z "$key" ]; then
    echo "[$name] skipped — no API key provided"
    return 0
  fi
  echo "[$name] fetching indexers from $base ..."
  curl -sf "$base/api/v3/indexer" -H "X-Api-Key: $key" | jq -c '.[]' |
  while read -r idx; do
    id=$(echo "$idx" | jq -r '.id')
    label=$(echo "$idx" | jq -r '.name')
    proto=$(echo "$idx" | jq -r '.protocol')
    if [ "$proto" != "torrent" ]; then
      echo "[$name] #$id \"$label\": $proto — skipped"
      continue
    fi
    current=$(echo "$idx" | jq -r '(.fields[] | select(.name=="minimumSeeders")).value // "none"')
    if [ "$current" = "$MIN_SEEDERS" ]; then
      echo "[$name] #$id \"$label\": already $MIN_SEEDERS — ok"
      continue
    fi
    patched=$(echo "$idx" | jq --argjson v "$MIN_SEEDERS" \
      '(.fields[] | select(.name=="minimumSeeders")).value = $v')
    status=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
      "$base/api/v3/indexer/$id" \
      -H "X-Api-Key: $key" -H "Content-Type: application/json" \
      -d "$patched")
    if [ "$status" = "200" ] || [ "$status" = "202" ]; then
      echo "[$name] #$id \"$label\": $current → $MIN_SEEDERS"
    else
      echo "[$name] #$id \"$label\": PUT failed (HTTP $status)" >&2
    fi
  done
}

update_indexers "Radarr" "$RADARR_URL" "$RADARR_API_KEY"
update_indexers "Sonarr" "$SONARR_URL" "$SONARR_API_KEY"
echo "Done."
