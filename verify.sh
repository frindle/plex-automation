#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then python3 -m venv .venv >/dev/null 2>&1; .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1; fi
fails=0
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read())" || { echo "PARSE FAIL"; fails=$((fails+1)); }
# edit-landed literal guards
grep -qF "_recent_upgrade_download_ids" arr-webhook.py || { echo "FAIL: no dedupe set"; fails=$((fails+1)); }
grep -qF "threading.Thread(target=handle_upgrade_import" arr-webhook.py || { echo "FAIL: not dispatched to a thread"; fails=$((fails+1)); }
# behavioural property
"$PY" fixture_upgrade.py || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
