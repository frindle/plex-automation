#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then python3 -m venv .venv >/dev/null 2>&1; .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1; fi
fails=0
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read())" || { echo "PARSE FAIL"; fails=$((fails+1)); }
grep -qF "'untracked_seeding'" arr-webhook.py || { echo "FAIL: no untracked_seeding bucket"; fails=$((fails+1)); }
grep -qF "cat in ('dupe', 'untracked')" arr-webhook.py || { echo "FAIL: untracked not in seeding reroute"; fails=$((fails+1)); }
grep -qF "cat + '_seeding'" arr-webhook.py || { echo "FAIL: reroute not using cat + _seeding"; fails=$((fails+1)); }
"$PY" fixture_orphan.py || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
