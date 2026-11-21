#!/usr/bin/env python3
"""Plex API checks against a live throwaway Plex (run by test_plex_api.sh).

Hard asserts cover Plex behaviour and hotset results. Mismatches between
Plex's real output and what tiering/offload/plex_watch.py expects are printed as
FINDING lines (and written to the step summary) without failing the run.
"""
import importlib.util
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from plexctl import Plex  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", os.devnull)


def load(name):
    sys.path.insert(0, f"{ROOT}/tiering")
    return importlib.import_module(f"offload.{name}")


def note(kind, msg):
    print(f"{kind}: {msg}", flush=True)
    with open(SUMMARY, "a") as fh:
        fh.write(f"- {kind}: {msg}\n")


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"ok: {msg}", flush=True)


px = Plex()
pw = load("plex_watch")
hs = load("hotset")
os.environ.setdefault("PLEX_URL", px.url)
if not os.environ.get("PLEX_TOKEN"):
    note("FINDING", "tiering/offload/plex_watch.py:25-26 raises PlexError when PLEX_TOKEN is empty, "
         "so it cannot talk to an unclaimed Plex that needs no token; the test sets a dummy value")
    os.environ["PLEX_TOKEN"] = "unclaimed-no-token"

# 1. TV scan ---------------------------------------------------------------
parts = px.parts()
eps = [p for p in parts if p["season"] is not None]
movies = [p for p in parts if p["season"] is None]
check(len(movies) == 2 and len(eps) == 8, f"2 movies and 8 episodes scanned (got {len(movies)}, {len(eps)})")
got = {(p["season"], p["episode"]) for p in eps}
check(got == {(s, e) for s in (1, 2) for e in (1, 2, 3, 4)}, "all 8 episodes have the right season and episode index")
for p in eps:
    s, e = p["season"], p["episode"]
    check(p["file"].endswith(f"Season {s:02d}/Test Show - S{s:02d}E{e:02d}.mkv"), f"S{s:02d}E{e:02d} maps to its file")

# 2. Real play calls -------------------------------------------------------
s1e3 = next(p for p in eps if (p["season"], p["episode"]) == (1, 3))
movie, other_movie = movies[0], movies[1]


def duration(rk):
    return px.mc(f"/library/metadata/{rk}")["Metadata"][0].get("duration") or 24000


t0 = time.time()
d = duration(movie["ratingKey"])
px.timeline(movie["ratingKey"], "playing", 0, d)
time.sleep(1)
px.timeline(movie["ratingKey"], "stopped", 8000, d)
d3 = duration(s1e3["ratingKey"])
px.timeline(s1e3["ratingKey"], "playing", 0, d3)
time.sleep(1)
px.timeline(s1e3["ratingKey"], "stopped", 8000, d3)
px.scrobble(s1e3["ratingKey"])
time.sleep(3)

md = px.mc(f"/library/metadata/{s1e3['ratingKey']}")["Metadata"][0]
note("RECORD", f"S01E03 after scrobble: viewCount={md.get('viewCount')} lastViewedAt={md.get('lastViewedAt')} "
     f"viewOffset={md.get('viewOffset')}")
raw = px.history()
if not raw:
    for q in ({"accountID": 1}, {"sort": "viewedAt:desc"}):
        note("RECORD", f"history with {q}: {len(px.mc('/status/sessions/history/all', params=q).get('Metadata', []))} rows")
    logp = os.path.join(os.environ.get("E2E_PLEX_LOGDIR", ""), "Plex Media Server.log")
    if os.path.exists(logp):
        for ln in open(logp, errors="replace"):
            if "scrobble" in ln.lower() or "timeline" in ln.lower():
                note("RECORD", "log: " + ln.strip()[:300])
note("RECORD", f"raw history rows: {len(raw)}; keys of first: {sorted(raw[0]) if raw else None}")
row = next((r for r in raw if str(r.get("ratingKey")) == str(s1e3["ratingKey"])), None)
check(md.get("viewCount") == 1, "scrobble marks S01E03 watched (viewCount=1)")
hist = pw.play_history(t0 - 60)
h = next((r for r in hist if r["ratingKey"] == str(s1e3["ratingKey"])), None)
if row is None:
    note("FINDING", "unclaimed Plex: /:/scrobble sets viewCount but writes NO row to "
         "/status/sessions/history/all (no account on the request), so plex_watch.play_history "
         "(plex_watch.py:99-110) returns nothing for plays on an unclaimed server; "
         "the hot-set input below falls back to lastViewedAt from library metadata")
    check(h is None, "play_history agrees with the raw history (empty)")
else:
    check(row.get("grandparentRatingKey") is not None and row.get("parentIndex") == 1 and row.get("index") == 3,
          "history row has grandparentRatingKey, parentIndex=1, index=3")
    check(h is not None and h["grandparentRatingKey"] == str(row["grandparentRatingKey"]),
          "play_history returns S01E03 with grandparentRatingKey")
    if "parentIndex" not in h or "index" not in h:
        note("FINDING", "tiering/offload/plex_watch.py:99-110 play_history drops parentIndex and index, which "
             "history rows carry; hotset gets them from library_items, so it only matters to callers using history alone")

prog = pw.in_progress()
mp = [r for r in prog if r["ratingKey"] == str(movie["ratingKey"])]
mdm = px.mc(f"/library/metadata/{movie['ratingKey']}")["Metadata"][0]
note("RECORD", f"movie after partial timeline: viewOffset={mdm.get('viewOffset')} lastViewedAt={mdm.get('lastViewedAt')}; "
     f"in_progress={prog}")
if not mp:
    note("FINDING", "unclaimed Plex: a /:/timeline play stopped at an offset does not store viewOffset, so "
         "plex_watch.in_progress (plex_watch.py:~136-150) never lists it")

items = pw.library_items()
check(len(items) == 10, f"plex_watch.library_items lists 10 items (got {len(items)})")
check(sum(len(i["files"]) for i in items) == 10, "library_items lists 10 files")
check(all(i["section"] in ("Movies", "TV") for i in items), "library_items has section titles")
note("RECORD", f"on_deck ratingKeys: {[x['ratingKey'] for x in pw.on_deck()]}")
note("RECORD", f"sessions with no playback: {pw.sessions()}")

# 3. Live session during a slow ranged read ----------------------------------
seen_raw, seen_watch = [], []
done = threading.Event()


def slow_read():
    hd = {"Accept": "*/*", "Range": "bytes=0-33554431", "X-Plex-Client-Identifier": "tiering-e2e-reader"}
    if px.token:
        hd["X-Plex-Token"] = px.token
    try:
        with urllib.request.urlopen(urllib.request.Request(px.url + other_movie["key"], headers=hd), timeout=120) as r:
            while r.read(1 << 20):
                time.sleep(0.15)
    finally:
        done.set()


th = threading.Thread(target=slow_read)
th.start()
while not done.is_set():
    time.sleep(0.4)
    seen_raw.append(len(px.sessions()))
    seen_watch.append(len(pw.sessions()))
th.join()
note("RECORD", f"/status/sessions during a direct-play Range read: raw counts {seen_raw}, "
     f"plex_watch counts {seen_watch}; session visible: {any(seen_raw)}")

# 4. Hot set ---------------------------------------------------------------
now = time.time()
plays = pw.play_history(0) + [{"ratingKey": r["ratingKey"], "viewedAt": r["viewedAt"] or now}
                              for r in pw.in_progress()]
if not plays:  # unclaimed Plex keeps no history rows: use lastViewedAt from the library metadata
    for p in eps + movies:
        m = px.mc(f"/library/metadata/{p['ratingKey']}")["Metadata"][0]
        if m.get("lastViewedAt"):
            plays.append({"ratingKey": str(p["ratingKey"]), "viewedAt": m["lastViewedAt"]})
    note("RECORD", f"hotset plays from lastViewedAt fallback: {[p['ratingKey'] for p in plays]}")
ses, deck = pw.sessions(), pw.on_deck()
ep_rk = {(p["season"], p["episode"]): str(p["ratingKey"]) for p in eps}


def run(cfg):
    return hs.desired_whole(items, plays, ses, deck, {}, now, cfg)


want = run(hs.Cfg())
for k, rk in ep_rk.items():
    check(rk in want, f"default Cfg: S{k[0]:02d}E{k[1]:02d} wanted whole")
# new_days=0 removes the 'new' rule so the play rules stand alone.
want0 = run(hs.Cfg(new_days=0))
for k, rk in ep_rk.items():
    check(rk in want0, f"new_days=0: S{k[0]:02d}E{k[1]:02d} wanted whole")
check(all(want0[ep_rk[(1, e)]][0] in ("played", "season") for e in (1, 2, 3, 4)), "S01 held by played/season rules")
check(all(want0[ep_rk[(2, e)]][0] == "next-season" for e in (1, 2, 3, 4)), "S02 held by next-season (E03 is in the last 3)")
check(str(other_movie["ratingKey"]) not in want0, "an unplayed movie is not wanted once new_days=0")
print("ALL OK")
