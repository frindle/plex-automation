#!/usr/bin/env bash
# One-shot update for the plex-automation container on Unraid.
#
# Usage:  ./update.sh
#         ./update.sh --no-cache    # rebuild from scratch (slower; use when
#                                     a dependency changed and you suspect
#                                     a stale layer)

set -e
cd "$(dirname "$0")"

EXTRA_BUILD_ARGS=""
if [ "${1:-}" = "--no-cache" ]; then
  EXTRA_BUILD_ARGS="--no-cache"
fi

echo "=== git pull ==="
git pull

echo "=== last commit ==="
git --no-pager log -1 --pretty='%h %s'

echo "=== building${EXTRA_BUILD_ARGS:+ (no cache)} ==="
docker-compose build $EXTRA_BUILD_ARGS

echo "=== restart ==="
docker-compose up -d

echo "=== done ==="
docker-compose ps
