#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then python3 -m venv .venv >/dev/null 2>&1; .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1; fi
fails=0
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read())" || { echo "PARSE FAIL"; fails=$((fails+1)); }
# edit-landed literal guards (the guard hook must be present)
grep -q "@app.before_request" arr-webhook.py || { echo "FAIL: no @app.before_request"; fails=$((fails+1)); }
grep -q "_DESTRUCTIVE_GET_GUARD_PATHS" arr-webhook.py || { echo "FAIL: no _DESTRUCTIVE_GET_GUARD_PATHS"; fails=$((fails+1)); }
grep -q "mutation requires POST" arr-webhook.py || { echo "FAIL: no 405 message"; fails=$((fails+1)); }
# behavioural property
"$PY" fixture_getpost.py || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
