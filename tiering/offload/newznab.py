"""Newznab client, release parsing and the NZB archive.

Indexers come from the environment: NEWZNAB_INDEXERS is a JSON list of
{"name", "url", "apikey"}. Nothing here logs that value, a request URL or an
indexer host. The ``name`` is a label for the caller's own use only; the
restore note in qualify.py never includes it.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NZB_MAX = 100 * 1024 * 1024


class NewznabError(Exception):
    pass


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


def title_matches(release_title: str, title: str, year: int | None = None,
                  season: int | None = None, episode: int | None = None) -> bool:
    """True when ``release_title`` is a release of the same title (and episode)."""
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
    return True


class NewznabClient:
    def __init__(self, indexers: list[Indexer], timeout: float = 30.0):
        self.indexers = list(indexers)
        self.timeout = timeout

    # -- transport
    def _get(self, ix: Indexer, params: dict, limit: int = 8 * 1024 * 1024) -> bytes:
        q = urllib.parse.urlencode({**params, "apikey": ix.apikey})
        base = ix.url.rstrip("/")
        sep = "&" if "?" in base else "?"
        return self._fetch(f"{base}{sep}{q}", limit)

    def _fetch(self, url: str, limit: int) -> bytes:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise NewznabError("indexer url must be http or https")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:  # noqa: S310
                data = r.read(limit + 1)
        except urllib.error.HTTPError as e:
            raise NewznabError(f"indexer status {e.code}") from None
        except (OSError, ValueError) as e:
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

    def search(self, **kw) -> list[Release]:
        """Search every indexer. An indexer that fails is skipped.

        Raises NewznabError only when every indexer failed.
        """
        if not self.indexers:
            raise NewznabError("no indexers configured")
        out: list[Release] = []
        errors = 0
        for ix in self.indexers:
            try:
                out.extend(self.search_indexer(ix, **kw))
            except NewznabError:
                errors += 1
        if self.indexers and errors == len(self.indexers):
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
            title = (it.findtext("title") or "").strip()
            if not title:
                continue
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
            out.append(Release(title, guid or link, size, ix.name, parse_resolution(title), link))
        return out

    # -- fetch
    def fetch_nzb(self, rel: Release) -> bytes:
        """Download the NZB for a release. Returns raw NZB XML bytes."""
        if not rel.link:
            raise NewznabError("release has no link")
        raw = self._fetch(rel.link, NZB_MAX)
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                raise NewznabError("bad gzip") from None
        if b"<nzb" not in raw[:2048]:
            raise NewznabError("not an nzb")
        return raw


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

    The sha256 is of the raw NZB bytes. Writing is atomic and repeatable.
    """
    sha = hashlib.sha256(nzb_bytes).hexdigest()
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"{safe_name(release_name)}.{sha}.nzb.gz")
    if os.path.exists(path):
        return path
    tmp = path + ".tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(nzb_bytes)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, path)
    return path


def read_archived(path: str) -> bytes:
    with gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb") as f:
        return f.read()
