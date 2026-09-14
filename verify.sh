#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then python3 -m venv .venv >/dev/null 2>&1; .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1; fi
fails=0
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read()); ast.parse(open('media_share.py').read())" || { echo "PARSE FAIL"; fails=$((fails+1)); }
# edit-landed literal guards (exact MUST-contain literals)
grep -qF "os.path.realpath(os.path.join(root, rel_path))" media_share.py || { echo "FAIL: safe_join realpath literal absent"; fails=$((fails+1)); }
grep -qF "torrent_matches_any_title(new_name, [info.get('name', '')])" arr-webhook.py || { echo "FAIL: token-match literal absent"; fails=$((fails+1)); }
grep -qF "os.path.normpath(os.path.join(root, rel_path))" media_share.py && { echo "FAIL: old normpath still present"; fails=$((fails+1)); }
grep -qF "torrent_name in new_name or new_name in torrent_name" arr-webhook.py && { echo "FAIL: old substring match still present"; fails=$((fails+1)); }
# behavioural property
"$PY" fixture_pathmatch.py || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
