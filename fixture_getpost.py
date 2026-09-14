#!/usr/bin/env python3
"""Adversarial behavioural test: destructive endpoints refuse to MUTATE on GET.

Property: for the destructive maintenance routes, a request that would change
state (delete torrents/files, run a repair/rescue) must be REJECTED with 405
unless the HTTP method is POST. A GET dry-run REPORT (no mutation) must still
pass the guard. State-changing GETs are triggerable by prefetch/crawler/CSRF,
so they must not act.

Mutation is signalled two ways in this codebase:
  - apply-style routes: ?apply=1 turns the dry-run into a real delete.
  - dry_run-style routes (/auto-rescue, /run-deluge-repair): mutate BY DEFAULT
    on a bare GET; ?dry_run=1 is the safe report.
"""
import sys
aw = __import__("arr-webhook")

# make any route body that slips through fail fast (no real network waits)
aw.DELUGE_URL = "http://127.0.0.1:1"
aw.RADARR_URL = "http://127.0.0.1:1"
aw.SONARR_URL = "http://127.0.0.1:1"
aw.RADARR_API_KEY = "x"
aw.app.testing = False
c = aw.app.test_client()

fails = 0
def check(desc, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {'ok ' if ok else 'FAIL'}: {desc}: got {got!r} want {want!r}")

# ── mutating GETs must be 405 ────────────────────────────────────────────────
print("mutating GET -> 405:")
for path, qs in [
    ("/torrent-purge", "apply=1&hash=deadbeef"),
    ("/purge-unstarted-superseded", "apply=1"),
    ("/complete-orphans", "apply=1"),
    ("/incomplete-orphans", "apply=1"),
    ("/run-stalled-seeds", "apply=1"),
    ("/auto-rescue", ""),            # mutates by default (no dry_run)
    ("/run-deluge-repair", ""),      # mutates by default (no dry_run)
]:
    check(f"GET {path}?{qs}", c.get(f"{path}?{qs}").status_code, 405)

# ── POST is allowed through the guard (not 405) ──────────────────────────────
print("POST mutation allowed through guard (status != 405):")
check("POST /torrent-purge?apply=1&hash=deadbeef != 405",
      c.post("/torrent-purge?apply=1&hash=deadbeef").status_code != 405, True)
check("POST /auto-rescue != 405",
      c.post("/auto-rescue").status_code != 405, True)

# ── non-mutating (dry-run) GET is NOT blocked by the guard (proves selectivity) ─
print("dry-run GET passes the guard (status != 405):")
check("GET /torrent-purge?hash=deadbeef (no apply) != 405",
      c.get("/torrent-purge?hash=deadbeef").status_code != 405, True)
check("GET /auto-rescue?dry_run=1 != 405",
      c.get("/auto-rescue?dry_run=1").status_code != 405, True)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
