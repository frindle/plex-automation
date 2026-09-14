#!/usr/bin/env bash
# Verify: seed-obligation authoritative on every data-delete path.
# Env parity: arr-webhook imports flask/requests + media_share(paramiko), so we
# need a venv with requirements.txt or the module won't import.
set -uo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  python3 -m venv .venv >/dev/null 2>&1
  .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1
fi

fails=0

# 1. target still parses
"$PY" -c "import ast; ast.parse(open('arr-webhook.py').read())" || { echo "PARSE FAIL arr-webhook.py"; fails=$((fails+1)); }

# 2. the adversarial behavioural property (drives the real functions)
"$PY" fixture_hnr.py || fails=$((fails+1))

# 3. edit-landed guard: the old OR-hole literals must be GONE from both sites.
#    remove_torrent guard used tracker registration as a delete licence:
if grep -q "not torrent_is_unregistered(info) and seeding_time <" arr-webhook.py; then
  echo "FAIL: old remove_torrent OR-guard still present"; fails=$((fails+1)); fi
#    queued_superseded_targets OR-branch on unregistered:
if grep -qE "== 0 or torrent_is_unregistered\(info\)" arr-webhook.py; then
  echo "FAIL: queued_superseded_targets still purges on unregistered"; fails=$((fails+1)); fi

echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
