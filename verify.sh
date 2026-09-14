#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then python3 -m venv .venv >/dev/null 2>&1; .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1; fi
fails=0
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read()); ast.parse(open('media_share.py').read())" || { echo "PARSE FAIL"; fails=$((fails+1)); }
# edit-landed literal guards
grep -q "realpath" media_share.py || { echo "FAIL: media_share still not using realpath"; fails=$((fails+1)); }
grep -q "torrent_matches_any_title(new_name" arr-webhook.py || { echo "FAIL: find_new_torrent_hash not using token match"; fails=$((fails+1)); }
if grep -q "torrent_name in new_name or new_name in torrent_name" arr-webhook.py; then echo "FAIL: old substring match still present"; fails=$((fails+1)); fi
# behavioural property
"$PY" fixture_pathmatch.py || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
