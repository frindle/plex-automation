"""The checks a file must pass before any local byte is deleted.

``qualify`` runs five steps in order and stops at the first failure:

1. find or reuse an NZB for the exact release, and archive it;
2. import it into AltMount;
3. read the whole file back through WebDAV; its SHA-256 must equal the local one;
4. STAT every segment on the secondary and the primary provider: none missing;
5. restore path: another release of the same title that is at least the
   resolution floor.

Row fields it reads (all optional except ``sha256``):
  sha256, nzb_path, release_name, lib_path, title, year, imdb_id, tvdb_id,
  season, episode, resolution, is4k.

It does not write to the database. ``QualifyResult.fields`` carries what the
caller should store (nzb_path, remote_path, restore_score, restore_note and the
result of each step).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError

from .altmount import AltMountError
from .newznab import (NewznabError, archive_nzb, nzb_segment_ids, parse_resolution,
                      read_archived, title_matches)
from .nntp_stat import NntpUnavailable

_ERRORS = (AltMountError, NewznabError, NntpUnavailable, OSError, ParseError)


@dataclass
class QualifyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)


def resolution_floor(src_res: int) -> int:
    """Lowest resolution an alternative may have, given the source's."""
    return 1080 if src_res >= 1080 else 720


def source_resolution(row: dict) -> int:
    if row.get("resolution"):
        return int(row["resolution"])
    if row.get("is4k"):
        return 2160
    for k in ("release_name", "lib_path"):
        if row.get(k):
            r = parse_resolution(os.path.basename(str(row[k])))
            if r:
                return r
    return 0


def release_name_of(row: dict) -> str:
    if row.get("release_name"):
        return str(row["release_name"])
    p = os.path.basename(str(row.get("lib_path") or ""))
    return os.path.splitext(p)[0]


_SPLIT = re.compile(
    r"[ ._\-(\[]((?:19|20)\d{2}|s\d{1,2}e\d{1,3}|\d{1,2}x\d{2,3})(?:[ ._\-)\]]|$)", re.I)


def identity(row: dict) -> dict:
    """Title, year, season and episode, from the row or parsed from the name."""
    name = release_name_of(row)
    title, year = row.get("title"), row.get("year")
    season, episode = row.get("season"), row.get("episode")
    if not title:
        m = _SPLIT.search(" " + name)
        raw = (" " + name)[: m.start()] if m else name
        title = raw.replace(".", " ").replace("_", " ").strip()
        if m and not year and re.fullmatch(r"(?:19|20)\d{2}", m.group(1)):
            year = int(m.group(1))
    if season is None or episode is None:
        m = re.search(r"s(\d{1,2})e(\d{1,3})", name, re.I)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    return {"title": title, "year": year, "season": season, "episode": episode,
            "imdb_id": row.get("imdb_id"), "tvdb_id": row.get("tvdb_id")}


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _search(indexers, ident: dict):
    return indexers.search(title=ident["title"], imdb_id=ident["imdb_id"],
                           tvdb_id=ident["tvdb_id"], season=ident["season"],
                           episode=ident["episode"])


def score_restore(row: dict, indexers, own_ids: frozenset | None = None) -> dict:
    """Count releases that could replace this file if the exact release dies.

    Returns {count, floor, best, below_floor, note}. ``own_ids`` is the set of
    message-ids of our NZB: a same-named release on another indexer only counts
    when its NZB has a different set. The note never names an indexer.
    """
    ident = identity(row)
    own = _clean(release_name_of(row))
    floor = resolution_floor(source_resolution(row))
    seen: dict[str, int] = {}  # cleaned title -> resolution, qualifying only
    below = 0
    for rel in _search(indexers, ident):
        if not title_matches(rel.title, ident["title"] or "", ident["year"],
                             ident["season"], ident["episode"]):
            continue
        key = _clean(rel.title)
        if key in seen:
            continue
        if rel.resolution < floor:
            below += 1
            continue
        if key == own:
            if own_ids is None:
                continue
            try:
                if nzb_segment_ids(indexers.fetch_nzb(rel)) == own_ids:
                    continue  # the same NZB again
            except _ERRORS:
                continue
        seen[key] = rel.resolution
    count = len(seen)
    best = max(seen.values()) if seen else 0
    if count:
        note = f"{count} alternative(s), best {best}p"
    elif below:
        note = f"only below-floor alternatives ({below}), floor {floor}p"
    else:
        note = "no alternative release found"
    return {"count": count, "floor": floor, "best": best, "below_floor": below, "note": note}


def _find_exact(row: dict, indexers, name: str):
    want = _clean(name)
    for rel in _search(indexers, identity(row)):
        if _clean(rel.title) == want:
            yield rel


def qualify(row: dict, *, reader, importer, indexers, stat_primary, stat_secondary,
            archive_dir: str) -> QualifyResult:
    fields: dict = {"steps": {}}
    steps = fields["steps"]
    name = release_name_of(row)

    def fail(step: str, reason: str, **info) -> QualifyResult:
        steps[step] = {"ok": False, **info}
        return QualifyResult(False, [reason], fields)

    # 1. exact NZB
    nzb_path = row.get("nzb_path")
    nzb_bytes = None
    try:
        if nzb_path and os.path.exists(nzb_path):
            nzb_bytes = read_archived(nzb_path)
            steps["nzb"] = {"ok": True, "reused": True}
        else:
            for rel in _find_exact(row, indexers, name):
                try:
                    nzb_bytes = indexers.fetch_nzb(rel)
                    break
                except NewznabError:
                    continue
            if nzb_bytes is None:
                return fail("nzb", "no_exact_nzb")
            nzb_path = archive_nzb(nzb_bytes, archive_dir, name)
            steps["nzb"] = {"ok": True, "reused": False}
    except _ERRORS as e:
        return fail("nzb", "nzb_error", error=type(e).__name__)
    fields["nzb_path"] = nzb_path

    # 2. import
    try:
        remote_path = importer.import_nzb(nzb_bytes, name)
    except _ERRORS as e:
        return fail("import", "import_failed", error=type(e).__name__)
    steps["import"] = {"ok": True}
    fields["remote_path"] = remote_path

    # 3. full read
    try:
        got = reader.sha256_full(remote_path)
    except _ERRORS as e:
        return fail("hash", "read_failed", error=type(e).__name__)
    if got != row.get("sha256"):
        return fail("hash", "hash_mismatch")
    steps["hash"] = {"ok": True}

    # 4. every segment on both providers
    for label, fn in (("secondary", stat_secondary), ("primary", stat_primary)):
        try:
            res = fn(nzb_path)
        except NntpUnavailable as e:
            return fail(f"stat_{label}", f"stat_unavailable_{label}", kind=e.kind)
        except _ERRORS as e:
            return fail(f"stat_{label}", f"stat_unavailable_{label}", kind=type(e).__name__)
        info = {"total": res["total"], "missing": res["missing"]}
        if res["missing"] or res["total"] == 0:
            return fail(f"stat_{label}", f"missing_segments_{label}", **info)
        steps[f"stat_{label}"] = {"ok": True, **info}

    # 5. restore path
    try:
        sc = score_restore(row, indexers, nzb_segment_ids(nzb_bytes))
    except _ERRORS as e:
        return fail("restore", "restore_check_failed", error=type(e).__name__)
    fields["restore_score"] = sc["count"]
    fields["restore_note"] = sc["note"]
    if sc["count"] < 1:
        return fail("restore", "no_restore_path", count=0, below_floor=sc["below_floor"])
    steps["restore"] = {"ok": True, "count": sc["count"], "best": sc["best"]}
    return QualifyResult(True, [], fields)
