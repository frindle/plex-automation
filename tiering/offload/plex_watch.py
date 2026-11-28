"""Plex client (GET only; the one write-like call is scan_path, a GET) and the tierfs event reader.

Env: PLEX_URL, PLEX_TOKEN. The token is sent only in the X-Plex-Token
header and is never printed or logged.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

PAGE = 200
TIMEOUT = 30


class PlexError(RuntimeError):
    pass


def _get(path, params=None, start=None, size=None):
    base = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not base or not token:
        raise PlexError("PLEX_URL and PLEX_TOKEN must be set")
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + params
    headers = {"Accept": "application/json", "X-Plex-Token": token}
    if start is not None:
        headers["X-Plex-Container-Start"] = str(start)
        headers["X-Plex-Container-Size"] = str(size)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r).get("MediaContainer", {})
    except urllib.error.HTTPError as e:
        raise PlexError(f"GET {path} failed: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PlexError(f"GET {path} failed: {type(e).__name__}") from None


def _paged(path, params=None):
    out, start = [], 0
    while True:
        mc = _get(path, params, start, PAGE)
        rows = mc.get("Metadata") or []
        out.extend(rows)
        total = mc.get("totalSize")
        start += len(rows)
        if not rows or (total is not None and start >= total) \
                or (total is None and len(rows) < PAGE):
            return out


def _sections():
    return _get("/library/sections").get("Directory") or []


def _s(v):
    return None if v is None else str(v)


def _parts(row):
    return [p["file"] for m in (row.get("Media") or [])
            for p in (m.get("Part") or []) if p.get("file")]


def _durations(row):
    """Play time in ms per file: the part's own, else its media's, else the item's."""
    out = {}
    for m in row.get("Media") or []:
        for p in m.get("Part") or []:
            ms = p.get("duration") or m.get("duration") or row.get("duration")
            if p.get("file") and ms:
                try:
                    out[p["file"]] = int(ms)
                except (TypeError, ValueError):
                    pass
    return out


def _norm(row, section=None):
    res = next((m.get("videoResolution") for m in (row.get("Media") or [])
                if m.get("videoResolution")), None)
    return {
        "ratingKey": _s(row.get("ratingKey")),
        "type": row.get("type"),
        "grandparentRatingKey": _s(row.get("grandparentRatingKey")),
        "parentIndex": row.get("parentIndex"),
        "index": row.get("index"),
        "addedAt": row.get("addedAt"),
        "section": section or row.get("librarySectionTitle"),
        "videoResolution": res,
        "bitrate": next((m.get("bitrate") for m in (row.get("Media") or [])
                         if m.get("bitrate")), None),
        "files": _parts(row),
        "durations": _durations(row),
    }


def library_items():
    out = []
    for sec in _sections():
        typ = {"movie": 1, "show": 4}.get(sec.get("type"))
        if typ is None:
            continue
        for row in _paged(f"/library/sections/{sec['key']}/all", f"type={typ}"):
            out.append(_norm(row, sec.get("title")))
    return out


def play_history(since_ts):
    q = "sort=viewedAt:desc&viewedAt%3E=" + quote(str(int(since_ts)))
    out = []
    for r in _paged("/status/sessions/history/all", q):
        ts = r.get("viewedAt")
        if ts is None or ts < since_ts:
            continue
        gp = r.get("grandparentRatingKey")
        if gp is None and str(r.get("grandparentKey") or "").startswith("/library/metadata/"):
            gp = str(r["grandparentKey"]).rstrip("/").rsplit("/", 1)[-1] or None  # history rows carry grandparentKey
        out.append({"ratingKey": _s(r.get("ratingKey")),
                    "grandparentRatingKey": _s(gp),
                    "viewedAt": ts, "type": r.get("type")})
    return out


def sessions():
    out = []
    for r in _get("/status/sessions").get("Metadata") or []:
        files = _parts(r)
        out.append({"ratingKey": _s(r.get("ratingKey")),
                    "file": files[0] if files else None})
    return out


def on_deck():
    return [{"ratingKey": _s(r.get("ratingKey"))} for r in _paged("/library/onDeck")]


def in_progress():
    out = []
    for sec in _sections():
        typ = {"movie": 1, "show": 4}.get(sec.get("type"))  # 4 = episodes, not shows
        if typ is None:
            continue
        path = f"/library/sections/{sec['key']}/all"
        for r in _paged(path, urlencode({"type": typ, "inProgress": 1})):
            if (r.get("viewOffset") or 0) > 0:
                out.append({"ratingKey": _s(r.get("ratingKey")),
                            "viewedAt": r.get("lastViewedAt"),
                            "viewOffset": r["viewOffset"]})
    return out


def scan_path(folder):
    """Ask Plex for a partial scan of `folder` (a path as Plex sees it): the section whose
    location holds it, `GET /library/sections/<key>/refresh?path=<folder>`. Returns the
    section's title, or None when no section holds the folder. The token is sent in the
    header only; the answer has no body to parse."""
    base = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not base or not token:
        raise PlexError("PLEX_URL and PLEX_TOKEN must be set")
    folder = os.path.normpath(str(folder))
    best = None
    for sec in _sections():
        for loc in sec.get("Location") or []:
            root = os.path.normpath(str(loc.get("path") or ""))
            if root and (folder == root or folder.startswith(root.rstrip("/") + "/")) \
                    and (best is None or len(root) > best[0]):
                best = (len(root), sec)
    if best is None:
        return None
    sec = best[1]
    url = f"{base}/library/sections/{quote(str(sec['key']), safe='')}/refresh?" + \
        urlencode({"path": folder})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "X-Plex-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read(1024)
    except urllib.error.HTTPError as e:
        raise PlexError(f"scan failed: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PlexError(f"scan failed: {type(e).__name__}") from None
    return sec.get("title")


def read_tierfs_events(path, since_ts, id_to_rk):
    """Return {ratingKey: latest ts} for ids with a want event at or
    after since_ts. Reads `path` and rotated `path.1`..`path.3`; skips lines
    it cannot parse and files it cannot read."""
    found = {}
    for p in [str(path)] + [f"{path}.{i}" for i in (1, 2, 3)]:
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    if e.get("ev") != "want":
                        continue
                    ts = float(e["ts"])
                    rk = id_to_rk.get(e.get("id"))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if rk is None or ts < since_ts:
                    continue
                rk = str(rk)
                found[rk] = max(ts, found.get(rk, 0.0))
    return found
