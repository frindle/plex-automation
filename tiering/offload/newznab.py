"""Newznab client, release parsing and the NZB archive.

Indexers come from the environment: NEWZNAB_INDEXERS is a JSON list of
{"name", "url", "apikey"}. Nothing here logs that value, a request URL or an
indexer host. The ``name`` is a label for the caller's own use only; the
restore note in qualify.py never includes it.

Every URL must be https, except http to the local machine (used by tests), and
TLS is 1.2 or newer. NZB grabs are cached per client and spaced
``NEWZNAB_GRAB_INTERVAL`` seconds apart (default 1), because each grab is one
call against the indexer's daily API allowance.

Indexer etiquette (paid indexers ban for abuse). The runner keeps one client for
weeks, so every budget is a rolling 24-hour window kept inside the client; the caller
never resets anything.

- ``NEWZNAB_GRAB_BUDGET`` caps NZB grabs per indexer per 24 hours; default 10. When it
  is spent ``fetch_nzb`` raises ``GrabBudgetSpent``.
- ``NEWZNAB_SEARCH_BUDGET`` caps searches per indexer per 24 hours; default 30. When it
  is spent the indexer is skipped (``SearchBudgetSpent``) and the search is partial.
- ``NEWZNAB_MIN_INTERVAL_S`` is the least time between two requests to one indexer
  (search or grab); default 2.
- An indexer is dropped for 24 hours after 3 errors in a row, and at once on a 429
  (for the ``Retry-After`` time when it gives one, else 24 hours).

A budget that is spent, or an indexer that is dropped, makes a check unsure (unchecked,
not failed). Message-ids are compared without ``<>`` or white space, and case-sensitive.
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
import collections
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

NZB_MAX = 100 * 1024 * 1024
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class NewznabError(Exception):
    pass


class GrabBudgetSpent(NewznabError):
    """The NZB grab budget for this indexer is used up for the window."""


class SearchBudgetSpent(NewznabError):
    """The search budget for this indexer is used up for the window."""


class IndexerDropped(NewznabError):
    """This indexer is left alone for the rest of the window (errors, or a 429)."""


class RateLimited(NewznabError):
    """The indexer answered 429 (too many requests)."""

    def __init__(self, msg: str = "indexer rate limited", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


WINDOW_S = 24 * 3600.0
DROP_AFTER_ERRORS = 3


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


def _env_num(name: str, default, cast=float):
    try:
        v = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return v


def _retry_after(headers) -> float | None:
    try:
        v = headers.get("Retry-After") if headers is not None else None
        return max(0.0, float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


_CACHE_ENTRIES = 16
_CACHE_BYTES = 64 * 1024 * 1024


class NewznabClient:
    def __init__(self, indexers: list[Indexer], timeout: float = 30.0,
                 grab_interval: float | None = None, sleep=time.sleep, clock=time.monotonic,
                 grab_budget: int | None = None, search_budget: int | None = None,
                 min_interval: float | None = None):
        self.indexers = list(indexers)
        self.timeout = timeout
        if grab_interval is None:
            grab_interval = _env_num("NEWZNAB_GRAB_INTERVAL", 1.0)
        self.grab_interval = max(0.0, grab_interval)
        if grab_budget is None:
            grab_budget = _env_num("NEWZNAB_GRAB_BUDGET", 10, int)
        self.grab_budget = max(0, grab_budget)  # per indexer, per rolling 24 hours
        if search_budget is None:
            search_budget = _env_num("NEWZNAB_SEARCH_BUDGET", 30, int)
        self.search_budget = max(0, search_budget)
        if min_interval is None:
            min_interval = _env_num("NEWZNAB_MIN_INTERVAL_S", 2.0)
        self.min_interval = max(0.0, min_interval)
        self._sleep, self._clock = sleep, clock
        self._spent: dict[tuple[str, str], collections.deque] = {}  # (kind, indexer) -> times
        self._last_req: dict[str, float] = {}
        self._errors: dict[str, int] = {}
        self._dropped_until: dict[str, float] = {}
        self._cache: collections.OrderedDict = collections.OrderedDict()  # link -> (ids, raw)
        self._cache_bytes = 0
        self._last_grab: float | None = None
        self._lock = threading.RLock()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=tls_context()), _SafeRedirect())
        self.grabs = 0
        self.searches = 0

    # -- etiquette
    def _take(self, kind: str, name: str, budget: int, exc: type[NewznabError]) -> None:
        """Count one request against the rolling window, or raise ``exc``."""
        now = self._clock()
        q = self._spent.setdefault((kind, name), collections.deque())
        while q and now - q[0] >= WINDOW_S:
            q.popleft()
        if len(q) >= budget:
            raise exc(f"{kind} budget spent")
        q.append(now)

    def _gate(self, name: str) -> None:
        """Refuse a dropped indexer; space requests to one indexer apart."""
        now = self._clock()
        until = self._dropped_until.get(name)
        if until is not None:
            if now < until:
                raise IndexerDropped("indexer dropped for the window")
            del self._dropped_until[name]
            self._errors[name] = 0
        last = self._last_req.get(name)
        if self.min_interval and last is not None:
            wait = self.min_interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_req[name] = self._clock()

    def _note(self, name: str, err: NewznabError | None) -> None:
        """Track consecutive errors; drop the indexer on the third, or at once on a 429."""
        with self._lock:
            if err is None:
                self._errors[name] = 0
                return
            if isinstance(err, (IndexerDropped, SearchBudgetSpent, GrabBudgetSpent)):
                return
            if isinstance(err, RateLimited):
                wait = WINDOW_S if err.retry_after is None else min(err.retry_after, WINDOW_S)
                self._dropped_until[name] = self._clock() + wait
                return
            self._errors[name] = self._errors.get(name, 0) + 1
            if self._errors[name] >= DROP_AFTER_ERRORS:
                self._dropped_until[name] = self._clock() + WINDOW_S

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
            if e.code == 429:
                raise RateLimited("indexer status 429", _retry_after(e.headers)) from None
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
        with self._lock:
            self._gate(ix.name)
            self._take("search", ix.name, self.search_budget, SearchBudgetSpent)
            self.searches += 1
        try:
            out = self._parse(ix, self._get(ix, p))
        except NewznabError as e:
            self._note(ix.name, e)
            raise
        self._note(ix.name, None)
        return out

    def search(self, **kw) -> SearchResult:
        """Search every indexer.

        An indexer that fails (or is dropped, or has spent its budget) is skipped and
        the result says so: ``.partial`` is true and ``.failed`` counts them. A partial
        result may lack releases, so "nothing found" in it never means "nothing exists".
        Raises NewznabError when there are no indexers or every one failed.
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
            if root.get("code") == "429":
                raise RateLimited("indexer error 429")
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
        # the link holds the API key: it is never a fallback for the guid
        return Release(title, guid, size, ix.name, parse_resolution(title), link)

    # -- fetch
    def fetch_nzb(self, rel: Release) -> bytes:
        """Download the NZB for a release. Returns raw NZB XML bytes."""
        if not rel.link:
            raise NewznabError("release has no link")
        with self._lock:
            self._gate(rel.indexer)
            self._take("grab", rel.indexer, self.grab_budget, GrabBudgetSpent)
            if self.grab_interval and self._last_grab is not None:
                wait = self.grab_interval - (self._clock() - self._last_grab)
                if wait > 0:
                    self._sleep(wait)
            self._last_grab = self._clock()
            self.grabs += 1
        try:
            raw = self._fetch(rel.link, NZB_MAX)
        except NewznabError as e:
            self._note(rel.indexer, e)
            raise
        self._note(rel.indexer, None)
        if raw[:2] == b"\x1f\x8b":
            raw = _gunzip_capped(raw, NZB_MAX)
        if b"<nzb" not in raw[:2048]:
            raise NewznabError("not an nzb")
        return raw

    def fetch_nzb_ids(self, rel: Release) -> tuple[frozenset, bytes]:
        """Message-ids and bytes of a release's NZB. A link is grabbed once while it is
        in the client's small cache; the bytes come back with the ids on every hit."""
        with self._lock:
            hit = self._cache.get(rel.link)
            if hit is not None:
                self._cache.move_to_end(rel.link)
                return hit
        raw = self.fetch_nzb(rel)
        try:
            ids = nzb_segment_ids(raw)
        except ET.ParseError:
            raise NewznabError("bad nzb xml") from None
        with self._lock:
            self._cache[rel.link] = (ids, raw)
            self._cache_bytes += len(raw)
            while self._cache and (len(self._cache) > _CACHE_ENTRIES
                                   or self._cache_bytes > _CACHE_BYTES):
                _, (_, old) = self._cache.popitem(last=False)
                self._cache_bytes -= len(old)
        return ids, raw

    def fetch_ids(self, rel: Release) -> frozenset:
        """Message-ids of a release's NZB. Each link is grabbed once per client."""
        return self.fetch_nzb_ids(rel)[0]

    def take_raw(self, rel: Release) -> bytes | None:
        """The NZB bytes of a release ``fetch_ids`` has grabbed and still holds, else None."""
        with self._lock:
            hit = self._cache.get(rel.link)
        return hit[1] if hit else None


def _gunzip_capped(raw: bytes, limit: int) -> bytes:
    """Decompress gzip, refusing to produce more than ``limit`` bytes."""
    d = zlib.decompressobj(31)
    out, total = [], 0
    try:
        data = raw
        while data:
            chunk = d.decompress(data, limit + 1 - total)
            total += len(chunk)
            if total > limit:
                raise NewznabError("nzb too large")
            out.append(chunk)
            data = d.unconsumed_tail
        if not d.eof:
            raise NewznabError("bad gzip")
    except zlib.error:
        raise NewznabError("bad gzip") from None
    return b"".join(out)


def norm_id(mid: str) -> str:
    """A message-id as compared: no white space, no ``<>``. Case is kept (it is significant)."""
    return mid.strip().strip("<>").strip()


def nzb_segment_ids(nzb_bytes: bytes) -> frozenset[str]:
    """The set of message-ids in an NZB, normalised with ``norm_id``."""
    ids = set()
    for _, el in ET.iterparse(io.BytesIO(nzb_bytes), events=("end",)):
        if el.tag.rsplit("}", 1)[-1] == "segment":
            t = norm_id(el.text or "")
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
    try:
        with gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb") as f:
            return f.read()
    except (EOFError, zlib.error):
        raise NewznabError("bad archived nzb") from None
