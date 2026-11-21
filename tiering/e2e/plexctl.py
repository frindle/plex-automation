#!/usr/bin/env python3
"""Plex helpers for the tiering e2e tests (stdlib only).

Talks to a throwaway, unclaimed Plex Media Server. The token comes from the
PLEX_TOKEN env var if set (an unclaimed server on an allowed network needs
none) and is sent only in a header; it is never printed or put in a URL.

Env: PLEX_URL (default http://127.0.0.1:32400), PLEX_TOKEN (optional).
Every command prints JSON on stdout. Importable: use Plex(...).
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "tiering-e2e-client"

# (agent, scanner) pairs tried in order. The legacy "none" agent needs no
# internet, which matches an unclaimed server with no account.
AGENTS = {
    "movie": [("com.plexapp.agents.none", "Plex Movie Scanner"),
              ("tv.plex.agents.movie", "Plex Movie")],
    "show": [("com.plexapp.agents.none", "Plex Series Scanner"),
             ("tv.plex.agents.series", "Plex TV Series")],
}


class PlexError(Exception):
    pass


class Plex:
    def __init__(self, url=None, token=None, timeout=60):
        self.url = (url or os.environ.get("PLEX_URL") or "http://127.0.0.1:32400").rstrip("/")
        self.token = token if token is not None else os.environ.get("PLEX_TOKEN", "")
        self.timeout = timeout

    def _headers(self, extra=None):
        h = {"Accept": "application/json", "X-Plex-Client-Identifier": CLIENT_ID,
             "X-Plex-Product": "tiering-e2e", "X-Plex-Version": "1"}
        if self.token:
            h["X-Plex-Token"] = self.token
        h.update(extra or {})
        return h

    def request(self, path, method="GET", params=None, headers=None, raw=False):
        url = self.url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers=self._headers(headers))
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            # Report the path only; never the headers.
            raise PlexError(f"{method} {path.split('?')[0]} -> HTTP {e.code}") from None
        if raw:
            return resp
        with resp:
            body = resp.read()
        if not body:
            return {}
        try:
            return json.loads(body)
        except ValueError:
            return {}

    def mc(self, path, **kw):
        return self.request(path, **kw).get("MediaContainer", {})

    # -- server ------------------------------------------------------------
    def wait_ready(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.mc("/identity").get("machineIdentifier"):
                    self.sections()  # 4xx/5xx until the library is up
                    return True
            except Exception:
                pass
            time.sleep(2)
        raise PlexError("server not ready")

    # -- sections ----------------------------------------------------------
    def sections(self):
        return self.mc("/library/sections").get("Directory", [])

    def create_section(self, name, kind, location):
        """kind is 'movie' or 'show'. Returns the section key."""
        last = None
        end = time.time() + 180
        while time.time() < end:  # "server is still starting up" is a 400 for a while
            for agent, scanner in AGENTS[kind]:
                try:
                    self.request("/library/sections", method="POST", params={
                        "name": name, "type": kind, "agent": agent, "scanner": scanner,
                        "language": "en-US", "location": location})
                except PlexError as e:
                    last = e
                    continue
                for s in self.sections():
                    if s.get("title") == name:
                        return s["key"]
            time.sleep(5)
        raise PlexError(f"could not create section {name}: {last}")

    def create_standard(self, movies_dir, tv_dir):
        return {"Movies": self.create_section("Movies", "movie", movies_dir),
                "TV": self.create_section("TV", "show", tv_dir)}

    # -- scan --------------------------------------------------------------
    def scan(self, key, force=False):
        self.request(f"/library/sections/{key}/refresh", params={"force": 1} if force else None)

    def _busy(self):
        acts = self.mc("/activities").get("Activity", [])
        return any(str(a.get("type", "")).startswith("library.") for a in acts)

    def wait_scan(self, timeout=300, settle=3):
        """Wait until Plex reports no library activity for `settle` polls."""
        end = time.time() + timeout
        quiet = 0
        time.sleep(1)
        while time.time() < end:
            quiet = 0 if self._busy() else quiet + 1
            if quiet >= settle:
                return True
            time.sleep(1)
        raise PlexError("scan did not finish")

    def scan_and_wait(self, key, force=False, timeout=300):
        self.scan(key, force)
        return self.wait_scan(timeout)

    # -- items and parts ---------------------------------------------------
    def items(self, key):
        """All leaf items (movies, or episodes) of a section."""
        kind = {s["key"]: s["type"] for s in self.sections()}.get(str(key))
        t = 4 if kind == "show" else 1
        return self.mc(f"/library/sections/{key}/all", params={"type": t}).get("Metadata", [])

    def parts(self, key=None):
        """List parts: item rating key, title, season/episode, part id, file, size, hash, key."""
        out = []
        keys = [key] if key is not None else [s["key"] for s in self.sections()]
        for k in keys:
            for it in self.items(k):
                md = self.mc(f"/library/metadata/{it['ratingKey']}").get("Metadata", [{}])[0]
                for m in md.get("Media", []):
                    for p in m.get("Part", []):
                        out.append({
                            "ratingKey": md.get("ratingKey"), "title": md.get("title"),
                            "season": md.get("parentIndex"), "episode": md.get("index"),
                            "mediaId": m.get("id"), "partId": p.get("id"),
                            "file": p.get("file"), "size": p.get("size"),
                            "hash": p.get("hash"), "key": p.get("key"),
                            "updatedAt": md.get("updatedAt"),
                            "videoCodec": m.get("videoCodec"), "audioCodec": m.get("audioCodec"),
                        })
        return out

    def find_part(self, substr):
        hits = [p for p in self.parts() if substr in (p.get("file") or "")]
        if len(hits) != 1:
            raise PlexError(f"{len(hits)} parts match {substr!r}")
        return hits[0]

    # -- reading bytes -----------------------------------------------------
    def fetch(self, part_key, rng=None):
        """Return (status, bytes). rng is (start, end_inclusive) or None for a full download."""
        h = {"Accept": "*/*"}
        if rng:
            h["Range"] = f"bytes={rng[0]}-{rng[1]}"
        resp = self.request(part_key, headers=h, raw=True)
        with resp:
            return resp.status, resp.read()

    def fetch_sha256(self, part_key, rng=None, chunk=1 << 20):
        """Stream a (ranged) read and hash it without holding it in memory."""
        h = {"Accept": "*/*"}
        if rng:
            h["Range"] = f"bytes={rng[0]}-{rng[1]}"
        d, n = hashlib.sha256(), 0
        resp = self.request(part_key, headers=h, raw=True)
        with resp:
            status = resp.status
            while True:
                b = resp.read(chunk)
                if not b:
                    break
                d.update(b)
                n += len(b)
        return {"status": status, "bytes": n, "sha256": d.hexdigest()}

    # -- playback ----------------------------------------------------------
    def timeline(self, rating_key, state, time_ms=0, duration_ms=60000):
        self.request("/:/timeline", params={
            "ratingKey": rating_key, "key": f"/library/metadata/{rating_key}",
            "identifier": "com.plexapp.plugins.library", "state": state,
            "time": time_ms, "duration": duration_ms, "playbackTime": time_ms})

    def play(self, rating_key, offset_ms=10000, duration_ms=60000):
        """Simulate a play: state playing at 0, then stopped at offset_ms."""
        self.timeline(rating_key, "playing", 0, duration_ms)
        time.sleep(1)
        self.timeline(rating_key, "stopped", offset_ms, duration_ms)

    def scrobble(self, rating_key):
        self.request("/:/scrobble", params={
            "key": rating_key, "identifier": "com.plexapp.plugins.library"})

    def history(self):
        return self.mc("/status/sessions/history/all").get("Metadata", [])

    def on_deck(self):
        return self.mc("/library/onDeck").get("Metadata", [])

    def sessions(self):
        return self.mc("/status/sessions").get("Metadata", [])


def _rng(s):
    a, _, b = s.partition("-")
    return int(a), int(b)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait-ready")
    p = sub.add_parser("create-sections")
    p.add_argument("--movies", required=True)
    p.add_argument("--tv", required=True)
    for n in ("scan",):
        p = sub.add_parser(n)
        p.add_argument("--section", help="section key (default: all)")
        p.add_argument("--force", action="store_true")
        p.add_argument("--timeout", type=int, default=300)
    sub.add_parser("sections")
    p = sub.add_parser("parts")
    p.add_argument("--match", help="only parts whose file contains this")
    p = sub.add_parser("fetch")
    p.add_argument("--part-key", help="Part key, e.g. /library/parts/1/2/file.mkv")
    p.add_argument("--match", help="select the part by file substring instead")
    p.add_argument("--range", type=_rng, help="START-END inclusive; omit for a full download")
    p = sub.add_parser("play")
    p.add_argument("rating_key")
    p.add_argument("--offset-ms", type=int, default=10000)
    p = sub.add_parser("timeline")
    p.add_argument("rating_key")
    p.add_argument("state", choices=["playing", "paused", "stopped"])
    p.add_argument("--time-ms", type=int, default=0)
    p = sub.add_parser("scrobble")
    p.add_argument("rating_key")
    sub.add_parser("history")
    sub.add_parser("on-deck")
    a = ap.parse_args(argv)

    px = Plex()
    out = {}
    try:
        if a.cmd == "wait-ready":
            out = {"ready": px.wait_ready()}
        elif a.cmd == "create-sections":
            out = px.create_standard(a.movies, a.tv)
        elif a.cmd == "scan":
            keys = [a.section] if a.section else [s["key"] for s in px.sections()]
            for k in keys:
                px.scan(k, a.force)
            out = {"done": px.wait_scan(a.timeout), "sections": keys}
        elif a.cmd == "sections":
            out = px.sections()
        elif a.cmd == "parts":
            out = [x for x in px.parts() if not a.match or a.match in (x["file"] or "")]
        elif a.cmd == "fetch":
            key = a.part_key or px.find_part(a.match)["key"]
            out = px.fetch_sha256(key, a.range)
        elif a.cmd == "play":
            px.play(a.rating_key, a.offset_ms)
        elif a.cmd == "timeline":
            px.timeline(a.rating_key, a.state, a.time_ms)
        elif a.cmd == "scrobble":
            px.scrobble(a.rating_key)
        elif a.cmd == "history":
            out = px.history()
        elif a.cmd == "on-deck":
            out = px.on_deck()
    except PlexError as e:
        print(f"plexctl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
