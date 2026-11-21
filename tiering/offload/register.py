"""Create and refresh ``whole`` rows from Radarr and Sonarr.

Run once a night before the demote window (the runner calls ``register``).

For every movie file and episode file the arrs list, it upserts a row keyed on
``lib_path``:

- a new row gets a fresh random 16-hex ``id``, ``state=whole``, ``size``,
  ``mtime_ns`` and the media identity (``title``, ``year``, ``imdb_id``,
  ``tvdb_id``, ``season``, ``episode``, ``release_name``). ``sha256`` stays empty
  until qualify fills it;
- an existing row is only touched while ``state=whole``. Its identity fields are
  refreshed; when the size or mtime differs it takes the new ones and ``sha256``
  is cleared, so qualify hashes the file again. The write is
  ``update_if(db, id, "whole", ...)``, so a row the runner moved on is left alone;
- it never deletes a row. A path the arrs no longer list is logged (only when
  every arr answered: a missing answer is not a missing file);
- ``apply=False`` writes nothing and only reports what it would do.

Environment (nothing here is ever logged, including keys and URLs):
  RADARR_URL, RADARR_API_KEY
  SONARR_URL, SONARR_API_KEY   a comma list for more than one Sonarr; one key may
                               be given to be used for all of them
  TIERING_PATH_MAP             ``arr_prefix=local_prefix`` pairs, comma separated,
                               the longest matching prefix wins
  TIERING_ARR_ALLOW_HTTP=1     allow plain http to an arr that is not on localhost
                               (a trusted LAN only; the key is sent in a header)

The API key is sent in the ``X-Api-Key`` header, never in a URL. mtime comes from
the local file: the arrs do not report it.

State functions used: ``by_path``, ``upsert``, ``update_if``, ``list_files``,
``log_event``.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_LIMIT = 256 * 1024 * 1024
IDENTITY = ("title", "year", "imdb_id", "tvdb_id", "season", "episode", "release_name")


class ArrError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # the key header must not follow a redirect
        return None


class ArrClient:
    """Minimal Radarr/Sonarr v3 API client. The key is a header and is never logged."""

    def __init__(self, url: str, key: str, timeout: float = 60.0, allow_http: bool = False):
        u = urllib.parse.urlsplit(url.strip())
        if not u.hostname or u.scheme not in ("http", "https"):
            raise ArrError("bad arr url")
        if u.scheme == "http" and u.hostname not in _LOCAL_HOSTS and not allow_http:
            raise ArrError("plain http is only allowed to localhost")
        if not key:
            raise ArrError("missing api key")
        self._base = url.strip().rstrip("/")
        self._key = key
        self._timeout = timeout
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), _NoRedirect())

    def __repr__(self) -> str:
        return "ArrClient()"

    def get(self, path: str, **params):
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self._base}{path}" + (f"?{q}" if q else ""),
                                     headers={"X-Api-Key": self._key, "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self._timeout) as r:
                raw = r.read(_LIMIT + 1)
        except urllib.error.HTTPError as e:
            raise ArrError(f"arr status {e.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise ArrError(f"arr unreachable: {type(e).__name__}") from None
        if len(raw) > _LIMIT:
            raise ArrError("response too large")
        try:
            return json.loads(raw)
        except ValueError:
            raise ArrError("arr sent bad json") from None


def _movie_files(client: ArrClient):
    for m in client.get("/api/v3/movie"):
        f = m.get("movieFile") if isinstance(m, dict) else None
        if not f or not f.get("path"):
            continue
        yield {"path": f["path"], "size": f.get("size"), "scene": f.get("sceneName"),
               "title": m.get("title"), "year": m.get("year") or None,
               "imdb_id": m.get("imdbId") or None, "tvdb_id": None,
               "season": None, "episode": None}


def _episode_files(client: ArrClient):
    for s in client.get("/api/v3/series"):
        sid = s.get("id")
        if sid is None:
            continue
        seen: dict = {}
        for ep in client.get("/api/v3/episode", seriesId=sid, includeEpisodeFile="true"):
            f = ep.get("episodeFile")
            if not f or not f.get("path"):
                continue
            key = (ep.get("seasonNumber"), ep.get("episodeNumber"))
            # a file that holds several episodes is listed once, under the first
            if f["path"] not in seen or key < seen[f["path"]][0]:
                seen[f["path"]] = (key, ep, f)
        for _, ep, f in seen.values():
            yield {"path": f["path"], "size": f.get("size"), "scene": f.get("sceneName"),
                   "title": s.get("title"), "year": s.get("year") or None,
                   "imdb_id": s.get("imdbId") or None, "tvdb_id": s.get("tvdbId") or None,
                   "season": ep.get("seasonNumber"), "episode": ep.get("episodeNumber")}


def parse_path_map(raw: str | None) -> list[tuple[str, str]]:
    """``a=b,c=d`` to [(a, b), (c, d)], longest source prefix first."""
    pairs = []
    for part in (raw or "").split(","):
        src, sep, dst = part.strip().partition("=")
        if sep and src.strip():
            pairs.append((src.strip().rstrip("/"), dst.strip().rstrip("/")))
    return sorted(pairs, key=lambda p: -len(p[0]))


def map_path(path: str, pmap: list[tuple[str, str]]) -> str:
    for src, dst in pmap:
        if path == src or path.startswith(src + "/"):
            return dst + path[len(src):]
    return path


def _split(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def clients_from_env(env=None) -> tuple[list[ArrClient], list[ArrClient]]:
    e = os.environ if env is None else env
    allow = e.get("TIERING_ARR_ALLOW_HTTP", "") in ("1", "true", "yes")

    def build(url_var: str, key_var: str) -> list[ArrClient]:
        urls, keys = _split(e.get(url_var)), _split(e.get(key_var))
        if not urls:
            return []
        if len(keys) == 1:
            keys = keys * len(urls)
        if len(keys) != len(urls):
            raise ArrError(f"{key_var} must have one key, or one per url")
        return [ArrClient(u, k, allow_http=allow) for u, k in zip(urls, keys)]

    return build("RADARR_URL", "RADARR_API_KEY"), build("SONARR_URL", "SONARR_API_KEY")


def _entry_row(ent: dict, local: str, size: int, mtime_ns: int) -> dict:
    scene = (ent.get("scene") or "").strip()
    release = scene or os.path.splitext(os.path.basename(local))[0]
    return {"title": ent.get("title"), "year": ent.get("year"), "imdb_id": ent.get("imdb_id"),
            "tvdb_id": ent.get("tvdb_id"), "season": ent.get("season"),
            "episode": ent.get("episode"), "release_name": release,
            "size": size, "mtime_ns": mtime_ns}


def register(db, apply: bool, *, state=None, radarrs=None, sonarrs=None, path_map=None,
             stat=os.stat, new_id=None) -> dict:
    """Sync rows from the arrs. Returns counts; with ``apply=False`` nothing is written."""
    if state is None:
        from . import state as _state
        state = _state
    if radarrs is None and sonarrs is None:
        radarrs, sonarrs = clients_from_env()
    pmap = parse_path_map(os.environ.get("TIERING_PATH_MAP")) if path_map is None else \
        (parse_path_map(path_map) if isinstance(path_map, str) else list(path_map))
    new_id = new_id or (lambda: secrets.token_hex(8))
    out = {"new": 0, "updated": 0, "unchanged": 0, "skipped_state": 0, "missing_local": 0,
           "unlisted": 0, "errors": 0, "applied": bool(apply)}

    listed: set[str] = set()
    complete = True
    sources = [(c, _movie_files) for c in (radarrs or [])] + \
              [(c, _episode_files) for c in (sonarrs or [])]
    for client, lister in sources:
        try:
            ents = list(lister(client))
        except ArrError as e:
            out["errors"] += 1
            complete = False
            log.warning("register: an arr did not answer (%s)", e)
            continue
        for ent in ents:
            local = map_path(str(ent["path"]), pmap)
            if local in listed:
                continue
            listed.add(local)
            try:
                st = stat(local)
            except OSError:
                out["missing_local"] += 1
                continue
            size = int(ent.get("size") or st.st_size)
            want = _entry_row(ent, local, size, st.st_mtime_ns)
            have = state.by_path(db, local)
            if have is None:
                out["new"] += 1
                if apply:
                    state.upsert(db, {"id": new_id(), "lib_path": local, "state": "whole",
                                      **want})
                continue
            if have.get("state") != "whole":
                out["skipped_state"] += 1
                continue
            change = {k: v for k, v in want.items()
                      if k in IDENTITY and v is not None and have.get(k) != v}
            if have.get("size") != size or have.get("mtime_ns") != st.st_mtime_ns:
                change.update(size=size, mtime_ns=st.st_mtime_ns, sha256=None)
            if not change:
                out["unchanged"] += 1
                continue
            out["updated"] += 1
            if apply and not state.update_if(db, have["id"], "whole", **change):
                out["updated"] -= 1
                out["skipped_state"] += 1

    if complete:
        for row in state.list_files(db):
            lp = row.get("lib_path")
            if lp and lp not in listed:
                out["unlisted"] += 1
                log.info("register: the arrs no longer list row %s", row.get("id"))
                if apply:
                    state.log_event(db, row["id"], "register_unlisted", {})
    return out
