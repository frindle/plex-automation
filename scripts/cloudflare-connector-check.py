#!/usr/bin/env python3
"""Alert when the Cloudflare tunnel loses redundancy.

WHY THIS EXISTS
Cloudflare only emails when a tunnel has ZERO connectors. On 2026-08-21 the
Firewalla connector was stopped and stayed down for 40 hours with no warning,
because storagedemon was still serving -- the tunnel looked healthy. Then
storagedemon rebooted at 02:07 on the 23rd, the count hit zero, and only then
did the email arrive. By that point all 31 hostnames were already down.

Losing redundancy silently is worse than having none, because you believe you
are covered. This closes that gap: it alerts on the drop from 2 to 1, which is
the moment something can still be done about it.

Deliberately NOT part of arr-webhook.py's scheduler: it must keep working even
if the Flask app is wedged, and it has no dependency on Radarr/Sonarr/Deluge.

Run from cron, e.g. every 15 minutes:
  */15 * * * * /usr/bin/python3 /path/to/cloudflare-connector-check.py

Env (add to the same .env the webhook uses):
  CF_API_TOKEN     Cloudflare token with Account > Cloudflare Tunnel > Read
  CF_ACCOUNT_ID    account id
  CF_TUNNEL_ID     tunnel id (optional; without it, every tunnel is checked)
  CF_MIN_CONNECTORS  expected healthy count, default 2
  PUSHOVER_TOKEN / PUSHOVER_USER   reused from the existing setup
"""

import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"
STATE_FILE = os.environ.get(
    "CF_CONNECTOR_STATE", "/mnt/user/appdata/homelab-scripts/cf-connector-state.json"
)


def _get(path, token):
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def pushover(title, message, priority=1):
    tok, usr = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not (tok and usr):
        print("pushover not configured; would have sent:", title, message)
        return
    data = urllib.parse.urlencode({
        "token": tok, "user": usr, "title": title,
        "message": message, "priority": priority,
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json", data=data),
            timeout=20,
        ).read()
    except Exception as exc:                                   # noqa: BLE001
        print(f"pushover send failed: {exc}", file=sys.stderr)


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:                                          # noqa: BLE001
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)
    except Exception as exc:                                   # noqa: BLE001
        print(f"could not persist state: {exc}", file=sys.stderr)


def main():
    token = os.environ.get("CF_API_TOKEN")
    account = os.environ.get("CF_ACCOUNT_ID")
    if not (token and account):
        print("CF_API_TOKEN and CF_ACCOUNT_ID required", file=sys.stderr)
        return 2
    want = int(os.environ.get("CF_MIN_CONNECTORS", "2"))
    only = os.environ.get("CF_TUNNEL_ID")

    tunnels = _get(f"/accounts/{account}/cfd_tunnel?is_deleted=false&per_page=50", token)
    state = load_state()
    changed = False

    for t in tunnels.get("result") or []:
        if only and t.get("id") != only:
            continue
        tid, name = t.get("id"), t.get("name")
        conns = (_get(f"/accounts/{account}/cfd_tunnel/{tid}/connections", token).get("result")
                 or [])
        count = len(conns)
        # Alert only on TRANSITIONS, so a persistent single-connector state does
        # not page every 15 minutes and train you to ignore it.
        prev = state.get(tid, {}).get("count")
        state[tid] = {"count": count, "name": name}
        if prev is not None and prev == count:
            continue
        changed = True
        if count == 0:
            pushover(f"Tunnel {name}: ALL connectors down",
                     f"{name} has no connectors. Every hostname on it is offline.", 2)
        elif count < want:
            hosts = ", ".join(sorted({
                (c.get("conns") or [{}])[0].get("origin_ip", "?") for c in conns
            }))
            pushover(f"Tunnel {name}: redundancy lost ({count}/{want})",
                     f"Only {count} connector(s) remain (origin {hosts}). "
                     f"The tunnel still serves, so Cloudflare will NOT email about this.")
        elif prev is not None and prev < want <= count:
            pushover(f"Tunnel {name}: redundancy restored ({count}/{want})",
                     f"Back to {count} connectors.", 0)
        print(f"{name}: {count} connector(s) (was {prev})")

    if changed:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
