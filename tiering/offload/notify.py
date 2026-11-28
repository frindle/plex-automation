"""After a restore swap: tell the arr and Plex that a file at a path is a new one.

``notify_from_env()`` returns ``notify(row, new_share_path) -> dict`` for
``ops.restore_from_path``. It never raises: each part reports what it did in the dict
(``arr``, ``plex``), so a missing setting only means that part is skipped.

* Radarr (``row.kind == "movie"``) gets ``RescanMovie``, Sonarr (episodes) ``RescanSeries``,
  through ``POST /api/v3/command``. The id comes from the arr's own list: the item whose
  folder (mapped by ``TIERING_ARR_PATH_MAP``) holds the file, else the one with the row's
  ``imdb_id`` (movies) or ``tvdb_id`` (episodes) when exactly one matches.
* Plex gets a partial scan of the file's folder (``plex_watch.scan_path``) when ``PLEX_URL``
  and ``PLEX_TOKEN`` are set; otherwise that part is skipped and says so.

Environment: RADARR_URL, RADARR_API_KEY, SONARR_URL, SONARR_API_KEY (see register.py),
TIERING_ARR_PATH_MAP, TIERING_ARR_ALLOW_HTTP, PLEX_URL, PLEX_TOKEN. Keys go in headers only,
and nothing here logs a URL or a key.
"""
from __future__ import annotations

import logging
import os

from . import paths, plex_watch, register

log = logging.getLogger(__name__)


def _under(path: str, root: str) -> bool:
    root = (root or "").rstrip("/")
    return bool(root) and (path == root or path.startswith(root + "/"))


def find_item(client, kind: str, share_path: str, row: dict, pairs):
    """The arr item (movie or series) that owns `share_path`, or None."""
    items = client.get("/api/v3/movie" if kind == "movie" else "/api/v3/series") or []
    for it in items:
        if _under(share_path, register.map_path(str(it.get("path") or ""), pairs)):
            return it
    idk, rk = ("imdbId", row.get("imdb_id")) if kind == "movie" else ("tvdbId", row.get("tvdb_id"))
    if rk not in (None, ""):
        hits = [it for it in items if str(it.get(idk) or "") == str(rk)]
        if len(hits) == 1:
            return hits[0]
    return None


def rescan(clients, kind: str, share_path: str, row: dict, pairs) -> str:
    cmd, idk = ("RescanMovie", "movieId") if kind == "movie" else ("RescanSeries", "seriesId")
    for c in clients:
        try:
            it = find_item(c, kind, share_path, row, pairs)
            if it is None:
                continue
            c.post("/api/v3/command", {"name": cmd, idk: it["id"]})
            return f"{cmd} {it['id']}"
        except register.ArrError as e:
            log.warning("restore: %s lookup failed: %s", cmd, e)
    return "skipped: no arr item owns this path"


def notify_from_env(env=None, *, radarrs=None, sonarrs=None, plex_scan=None):
    e = os.environ if env is None else env
    if radarrs is None and sonarrs is None:
        try:
            radarrs, sonarrs = register.clients_from_env(e)
        except register.ArrError as ex:
            log.warning("restore: arr settings unusable: %s", ex)
            radarrs, sonarrs = [], []
    scan = plex_scan or plex_watch.scan_path
    pairs = paths.parse_path_map(e.get("TIERING_ARR_PATH_MAP") or "")

    def notify(row, new_share_path):
        out = {}
        kind = row.get("kind")
        cl = radarrs if kind == "movie" else sonarrs if kind == "episode" else []
        if not cl:
            out["arr"] = "skipped: no arr client for this kind"
        else:
            try:
                out["arr"] = rescan(cl, kind, new_share_path, row, pairs)
            except Exception as ex:
                out["arr"] = f"failed: {type(ex).__name__}"
        if not (e.get("PLEX_URL") and e.get("PLEX_TOKEN")) and plex_scan is None:
            out["plex"] = "skipped: PLEX_URL and PLEX_TOKEN are not set"
        else:
            try:
                sec = scan(os.path.dirname(new_share_path))
                out["plex"] = "scanned folder" if sec else "skipped: no Plex section holds the folder"
            except Exception as ex:
                out["plex"] = f"failed: {type(ex).__name__}"
        return out
    return notify
