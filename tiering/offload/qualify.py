"""The checks a file must pass before any local byte is deleted.

``qualify`` runs five steps in order and stops at the first failure:

1. find or reuse an NZB for the exact release, and archive it;
2. import it into AltMount;
3. hash ``lib_path`` itself with a fresh full read, read the remote copy back
   through WebDAV, and require the two SHA-256 to be equal. A stored ``sha256``
   is never trusted;
4. STAT every segment on the secondary and the primary provider: none missing;
5. restore path: another release of the same title, at least the resolution
   floor, that is a different upload (it shares no message-id with our NZB).

Row fields it reads:
  lib_path (required), nzb_path, release_name, title, year, imdb_id, tvdb_id,
  season, episode, is4k.
The source resolution comes from ``is4k`` or the release or file name. When it
cannot be told, the floor is 1080p.

It does not write to the database. ``QualifyResult.fields`` holds only ``files``
columns for the caller to store: nzb_path, remote_path, sha256, restore_score,
restore_note, last_check_ts and last_stat_ok_ts. The result of each step is in
``QualifyResult.steps``, which is not a column.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError

from .altmount import AltMountError
from .newznab import (NewznabError, archive_nzb, nzb_segment_ids, parse_resolution,
                      read_archived, title_matches)
from .nntp_stat import NntpUnavailable

_ERRORS = (AltMountError, NewznabError, NntpUnavailable, OSError, ParseError)
_READ = 1 << 20


@dataclass
class QualifyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)  # files columns only
    steps: dict = field(default_factory=dict)   # per-step detail, for logs


def resolution_floor(src_res: int) -> int:
    """Lowest resolution an alternative may have, given the source's.

    A source below 1080p gets a 720p floor. An unknown source (0) gets 1080p.
    """
    return 720 if 0 < src_res < 1080 else 1080


def source_resolution(row: dict) -> int:
    """The source's resolution, from ``is4k`` or the name; 0 when unknown."""
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
    return {"title": title, "year": year or None, "season": season, "episode": episode,
            "imdb_id": row.get("imdb_id"), "tvdb_id": row.get("tvdb_id")}


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _search(indexers, ident: dict):
    return indexers.search(title=ident["title"], imdb_id=ident["imdb_id"],
                           tvdb_id=ident["tvdb_id"], season=ident["season"],
                           episode=ident["episode"])


def score_restore(row: dict, indexers, own_ids: frozenset | None,
                  stop_at: int | None = None) -> dict:
    """Count releases that could replace this file if the exact upload dies.

    ``own_ids`` is the set of message-ids of our NZB. A candidate whose NZB shares
    any message-id with it is the same upload, whatever it is called, and does not
    count. Size never decides. Two candidates that share message-ids count once.

    Each candidate that passes the title and floor checks costs one NZB grab
    (cached per client). ``stop_at`` ends the loop once that many are verified, so
    a caller that only needs "is there one" pays for as few grabs as it can.

    Returns {count, floor, best, below_floor, partial, unsure, note}.
    ``partial`` means some indexer did not answer; ``unsure`` means ``partial`` or
    some candidate's NZB could not be checked, or ``own_ids`` was not given.
    With ``unsure``, a count of 0 means "not checked", not "no restore path".
    Raises NewznabError when no indexer answered at all.
    """
    ident = identity(row)
    floor = resolution_floor(source_resolution(row))
    found = _search(indexers, ident)
    partial = bool(getattr(found, "partial", False))
    unsure = partial or own_ids is None
    cands, below = [], 0
    for rel in found:
        if not title_matches(rel.title, ident["title"] or "", ident["year"],
                             ident["season"], ident["episode"]):
            continue
        if rel.resolution < floor:
            below += 1
            continue
        cands.append(rel)
    cands.sort(key=lambda r: (-r.resolution, _clean(r.title), r.link))
    counted: list[frozenset] = []
    best = 0
    for rel in cands if own_ids is not None else ():
        if stop_at and len(counted) >= stop_at:
            break
        try:
            ids = indexers.fetch_ids(rel)
        except _ERRORS:
            unsure = True
            continue
        if not ids or ids & own_ids:
            continue  # the same upload (or nothing to compare)
        if any(ids & c for c in counted):
            continue  # the same upload as one already counted
        counted.append(ids)
        best = max(best, rel.resolution)
    count = len(counted)
    if count:
        note = f"{count} alternative(s), best {best}p"
    elif unsure:
        note = "restore path unchecked (an indexer or NZB did not answer)"
    elif below:
        note = f"only below-floor alternatives ({below}), floor {floor}p"
    else:
        note = "no alternative release found"
    return {"count": count, "floor": floor, "best": best, "below_floor": below,
            "partial": partial, "unsure": unsure, "note": note}


def _find_exact(row: dict, indexers, name: str):
    want = _clean(name)
    for rel in _search(indexers, identity(row)):
        if _clean(rel.title) == want:
            yield rel


def sha256_file(path: str) -> str:
    """SHA-256 of a file, from a fresh read of every byte."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(_READ)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def qualify(row: dict, *, reader, importer, indexers, stat_primary, stat_secondary,
            archive_dir: str, clock=time.time) -> QualifyResult:
    fields: dict = {}
    steps: dict = {}
    name = release_name_of(row)

    def fail(step: str, reason: str, **info) -> QualifyResult:
        steps[step] = {"ok": False, **info}
        return QualifyResult(False, [reason], fields, steps)

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

    # 3. the local file and the remote copy, both read in full now
    lib_path = row.get("lib_path")
    if not lib_path:
        return fail("hash", "no_lib_path")
    try:
        local = sha256_file(str(lib_path))
    except OSError as e:
        return fail("hash", "local_unreadable", error=type(e).__name__)
    fields["sha256"] = local
    try:
        remote = reader.sha256_full(remote_path)
    except _ERRORS as e:
        return fail("hash", "read_failed", error=type(e).__name__)
    if remote != local:
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
    now = clock()
    fields["last_check_ts"] = now
    fields["last_stat_ok_ts"] = now

    # 5. restore path
    try:
        sc = score_restore(row, indexers, nzb_segment_ids(nzb_bytes), stop_at=3)
    except _ERRORS as e:
        return fail("restore", "restore_check_failed", error=type(e).__name__)
    if sc["count"] or not sc["unsure"]:  # an unchecked 0 is not a score
        fields["restore_score"] = sc["count"]
    fields["restore_note"] = sc["note"]
    if sc["partial"]:
        return fail("restore", "restore_check_unsure", count=sc["count"])  # unsure is a fail
    if sc["count"] < 1:
        if sc["unsure"]:
            return fail("restore", "restore_check_unsure", count=0)
        return fail("restore", "no_restore_path", count=0, below_floor=sc["below_floor"])
    steps["restore"] = {"ok": True, "count": sc["count"], "best": sc["best"]}
    return QualifyResult(True, [], fields, steps)
