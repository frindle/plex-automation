"""Newznab client, release parsing and the NZB archive.

Indexers come from the environment: NEWZNAB_INDEXERS is a JSON list of
{"name", "url", "apikey"}. Nothing here logs that value, a request URL or an
indexer host. The ``name`` is a label for the caller's own use only; the
restore note in qualify.py never includes it.

Every URL must be https, except http to the local machine (used by tests), and
TLS is 1.2 or newer. NZB grabs are cached per client and spaced
``NEWZNAB_GRAB_INTERVAL`` seconds apart (default 1), because each grab is one
call against the indexer's daily API allowance.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NZB_MAX = 100 * 1024 * 1024
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class NewznabError(Exception):
    pass


def check_url(url: str) -> urllib.parse.SplitResult:
    """Allow https anywhere, and http only to the local machine."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return u
    if u.scheme == "http" and u.hostname in _LOCAL_HOSTS:
        return u
    raise NewznabError("indexer url must be https (http only to localhost)")


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect may not step down to plain http or to another scheme."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(urllib.parse.urljoin(req.full_url, newurl))
        except NewznabError:
            raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp) from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Indexer:
    name: str
    url: str = field(repr=False)
    apikey: str = field(repr=False)


@dataclass(frozen=True)
class Release:
    title: str
    guid: str
    size: int
    indexer: str  # label only
    resolution: int
    link: str = field(default="", repr=False)  # may carry the key; never log


def load_indexers(raw: str | None = None) -> list[Indexer]:
    raw = os.environ.get("NEWZNAB_INDEXERS", "") if raw is None else raw
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
        return [Indexer(str(i["name"]), str(i["url"]), str(i["apikey"])) for i in items]
    except (ValueError, KeyError, TypeError):
        raise NewznabError("NEWZNAB_INDEXERS is not a valid list") from None


_RES = re.compile(r"(?<![0-9])(2160|1080|720|576|480)[pi]?(?![0-9])", re.I)


def parse_resolution(title: str) -> int:
    """Vertical resolution from a release title; 0 when it does not say."""
    t = title.lower()
    if re.search(r"(?<![a-z0-9])(4k|uhd)(?![a-z0-9])", t):
        return 2160
    m = _RES.search(title)
    return int(m.group(1)) if m else 0


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


# First word after a title that shows the title is over (year, episode, quality, source).
_STOP = re.compile(
    r"^(?:(?:19|20)\d{2}|s\d{1,3}(?:e\d{1,3})?|\d{1,2}x\d{2,3}|2160p?|1080[pi]?|720p?|576p?|480p?"
    r"|4k|uhd|bluray|blu|bdrip|brrip|web|webdl|webrip|hdtv|dvdrip|dvd|remux|x264|x265|h264"
    r"|h265|hevc|avc|repack|proper|internal|extended|unrated|remastered|hdr|hdr10|dv)$")


def title_matches(release_title: str, title: str, year: int | None = None,
                  season: int | None = None, episode: int | None = None) -> bool:
    """True when ``release_title`` is a release of the same title (and episode).

    With no year (and no episode) there is nothing to tell "Movie" from "Movie Two"
    but the title itself, so the cleaned title must match exactly: what follows it
    must be a year, an episode or a quality tag, not more title words.
    """
    rel, want = _clean(release_title), _clean(title)
    if not want or not (rel == want or rel.startswith(want + " ")):
        return False
    rest = rel[len(want):].strip()
    if season is not None and episode is not None:
        pat = (rf"^(?:(?:19|20)\d{{2}} |[a-z]{{2}} )?(?:s0*{season}e0*{episode}|{season}x0*{episode})"
               r"(?![0-9])")
        return re.search(pat, rest) is not None
    if year:
        return rest.split(" ", 1)[0] == str(year)
    return rest == "" or _STOP.match(rest.split(" ", 1)[0]) is not None


class SearchResult(list):
    """A list of releases that also says how complete the search was.

    ``failed`` is how many indexers did not answer and ``total`` how many were
    asked. ``partial`` is true when at least one failed. (When all fail, search
    raises instead.)
    """

    failed: int = 0
    total: int = 0

    @property
    def partial(self) -> bool:
        return self.failed > 0


class NewznabClient:
    def __init__(self, indexers: list[Indexer], timeout: float = 30.0,
                 grab_interval: float | None = None, sleep=time.sleep, clock=time.monotonic):
        self.indexers = list(indexers)
        self.timeout = timeout
        if grab_interval is None:
            try:
                grab_interval = float(os.environ.get("NEWZNAB_GRAB_INTERVAL", "1") or 0)
            except ValueError:
                grab_interval = 1.0
        self.grab_interval = max(0.0, grab_interval)
        self._sleep, self._clock = sleep, clock
        self._ids_cache: dict[str, frozenset] = {}
        self._last_grab: float | None = None
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=tls_context()), _SafeRedirect())
        self.grabs = 0

    # -- transport
    def _get(self, ix: Indexer, params: dict, limit: int = 8 * 1024 * 1024) -> bytes:
        q = urllib.parse.urlencode({**params, "apikey": ix.apikey})
        base = ix.url.rstrip("/")
        sep = "&" if "?" in base else "?"
        return self._fetch(f"{base}{sep}{q}", limit)

    def _fetch(self, url: str, limit: int) -> bytes:
        check_url(url)
        try:
            with self._opener.open(url, timeout=self.timeout) as r:
                data = r.read(limit + 1)
                want = r.headers.get("Content-Length")
                if want and want.isdigit() and len(data) < int(want) <= limit:
                    raise http.client.IncompleteRead(data, int(want) - len(data))
        except urllib.error.HTTPError as e:
            raise NewznabError(f"indexer status {e.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise NewznabError(f"indexer unreachable: {type(e).__name__}") from None
        if len(data) > limit:
            raise NewznabError("response too large")
        return data

    # -- search
    def search_indexer(self, ix: Indexer, *, title: str | None = None,
                       imdb_id: str | None = None, tvdb_id: str | int | None = None,
                       season: int | None = None, episode: int | None = None,
                       limit: int = 100) -> list[Release]:
        tv = season is not None or tvdb_id is not None
        p: dict = {"t": "tvsearch" if tv else "movie", "limit": limit, "extended": 1}
        if title:
            p["q"] = title
        if imdb_id:
            p["imdbid"] = str(imdb_id).lower().removeprefix("tt")
        if tvdb_id:
            p["tvdbid"] = str(tvdb_id)
        if season is not None:
            p["season"] = season
        if episode is not None:
            p["ep"] = episode
        return self._parse(ix, self._get(ix, p))

    def search(self, **kw) -> SearchResult:
        """Search every indexer.

        An indexer that fails is skipped and the result says so: ``.partial`` is
        true and ``.failed`` counts them. A partial result may lack releases, so
        "nothing found" in it never means "nothing exists". Raises NewznabError
        when there are no indexers or every one failed.
        """
        if not self.indexers:
            raise NewznabError("no indexers configured")
        out = SearchResult()
        out.total = len(self.indexers)
        for ix in self.indexers:
            try:
                out.extend(self.search_indexer(ix, **kw))
            except NewznabError:
                out.failed += 1
        if out.failed == out.total:
            raise NewznabError("all indexers failed")
        return out

    def _parse(self, ix: Indexer, raw: bytes) -> list[Release]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise NewznabError("indexer sent bad xml") from None
        if root.tag == "error":
            raise NewznabError(f"indexer error {root.get('code', '')}")
        out = []
        for it in root.iter("item"):
            try:
                rel = self._item(ix, it)
            except (ValueError, TypeError, AttributeError):
                continue  # one bad item does not spoil the rest
            if rel is not None:
                out.append(rel)
        return out

    @staticmethod
    def _item(ix: Indexer, it) -> Release | None:
        title = (it.findtext("title") or "").strip()
        if not title:
            return None
        guid = (it.findtext("guid") or "").strip()
        size = 0
        link = (it.findtext("link") or "").strip()
        enc = it.find("enclosure")
        if enc is not None:
            link = enc.get("url") or link
            size = int(enc.get("length") or 0)
        for a in it:
            if a.tag.endswith("}attr"):
                if a.get("name") == "size":
                    size = int(a.get("value") or size)
                elif a.get("name") == "guid" and not guid:
                    guid = a.get("value") or ""
        return Release(title, guid or link, size, ix.name, parse_resolution(title), link)

    # -- fetch
    def fetch_nzb(self, rel: Release) -> bytes:
        """Download the NZB for a release. Returns raw NZB XML bytes."""
        if not rel.link:
            raise NewznabError("release has no link")
        with self._lock:
            if self.grab_interval and self._last_grab is not None:
                wait = self.grab_interval - (self._clock() - self._last_grab)
                if wait > 0:
                    self._sleep(wait)
            self._last_grab = self._clock()
            self.grabs += 1
        raw = self._fetch(rel.link, NZB_MAX)
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                raise NewznabError("bad gzip") from None
        if b"<nzb" not in raw[:2048]:
            raise NewznabError("not an nzb")
        return raw

    def fetch_ids(self, rel: Release) -> frozenset:
        """Message-ids of a release's NZB. Each link is grabbed once per client."""
        hit = self._ids_cache.get(rel.link)
        if hit is not None:
            return hit
        try:
            ids = nzb_segment_ids(self.fetch_nzb(rel))
        except ET.ParseError:
            raise NewznabError("bad nzb xml") from None
        self._ids_cache[rel.link] = ids
        return ids


def nzb_segment_ids(nzb_bytes: bytes) -> frozenset[str]:
    """The set of message-ids in an NZB."""
    ids = set()
    for _, el in ET.iterparse(io.BytesIO(nzb_bytes), events=("end",)):
        if el.tag.rsplit("}", 1)[-1] == "segment":
            t = (el.text or "").strip()
            if t:
                ids.add(t)
            el.clear()
    return frozenset(ids)


def safe_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("._") or "release"
    return n[:150]


def archive_nzb(nzb_bytes: bytes, archive_dir: str, release_name: str) -> str:
    """Store the NZB gzipped as ``<name>.<sha256>.nzb.gz``. Returns the path.

    The sha256 is of the raw NZB bytes. The temp file has a unique name, the
    write is fsynced, and so is the directory after the rename.
    """
    sha = hashlib.sha256(nzb_bytes).hexdigest()
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"{safe_name(release_name)}.{sha}.nzb.gz")
    if os.path.exists(path):
        return path
    tmp = os.path.join(archive_dir, f".{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(nzb_bytes)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(archive_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return path


def read_archived(path: str) -> bytes:
    with gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb") as f:
        return f.read()
