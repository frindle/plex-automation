"""The checks a file must pass before any local byte is deleted.

``qualify`` runs five steps in order and stops at the first failure:

1. find or reuse an NZB for the exact release, and archive it;
2. import it into AltMount;
3. hash ``lib_path`` itself with a fresh full read, read the remote copy back
   through WebDAV, and require the two SHA-256 to be equal. A stored ``sha256``
   is never trusted;
4. STAT every segment on the secondary and the primary provider: none missing;
5. restore path: another release of the same title, at least the resolution
   floor, that is a different upload (it shares no message-id with our NZB) and
   whose every segment is on both providers, like ``nzb_path``'s.

The first search is reused: one search per qualify, however many steps need it.

Row fields it reads:
  lib_path (required), nzb_path, release_name, title, year, imdb_id, tvdb_id,
  season, episode, is4k.
The source resolution comes from ``is4k`` or the release or file name. When it
cannot be told, the floor is 1080p.

It does not write to the database. ``QualifyResult.fields`` holds only ``files``
columns for the caller to store: nzb_path, nzb_path2 (the archived NZB of the verified
restore candidate; always present, None when there is none, so a stale one is cleared), remote_path, sha256, restore_score,
restore_note, last_check_ts and last_stat_ok_ts. The result of each step is in
``QualifyResult.steps``, which is not a column.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError

from .altmount import AltMountError
from .newznab import (BadRelease, GrabBudgetSpent, IndexersUnavailable, ItemFailed, NewznabError, NotNzb, archive_nzb, nzb_segment_ids, parse_resolution,
                      read_archived, title_matches)
from .nntp_stat import NntpUnavailable

_ERRORS = (AltMountError, NewznabError, NntpUnavailable, OSError, ParseError,
           EOFError, zlib.error, ValueError)
MAX_FAILED_GRABS = 3  # failed NZB grabs for one row before it is called unsure
GRAB_FAILED_NIGHTS = 3  # nights in a row a row's own grabs fail before it is called a miss
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


_SOFT = ("auth", "quota")  # a provider that cannot be asked at all


def stat_status(path: str, providers, dead: dict | None = None) -> str:
    """STAT an NZB file on each provider. ``ok``, ``missing`` or ``unchecked``.

    ``providers`` is a list of (label, checker). A provider that cannot be asked makes
    the answer ``unchecked`` (never ``missing``); with ``dead`` given, one that refused
    the login or reported a quota is remembered there and not asked again. An NZB that
    is not valid, has no segment or lacks any segment is ``missing``; any other error
    reading it (a disk fault, say) is ``unchecked``.
    """
    dead = {} if dead is None else dead
    for label, fn in providers:
        if label in dead:
            return "unchecked"
        try:
            out = fn(path)
        except NntpUnavailable as e:
            if e.kind in _SOFT:
                dead[label] = e.kind
            return "unchecked"
        except (ParseError, EOFError, zlib.error):  # not an NZB at all: as good as a dead one
            return "missing"
        except Exception:  # noqa: BLE001  # a read error says nothing about the articles
            return "unchecked"
        if out["total"] == 0 or out["missing"]:
            return "missing"
    return "ok"


def stat_nzb_bytes(raw: bytes, providers, tmp_dir: str, dead: dict | None = None) -> str:
    """``stat_status`` for NZB bytes not archived (yet): through a temp file that is removed."""
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix=".stat-", suffix=".nzb")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return stat_status(tmp, providers, dead)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def score_restore(row: dict, indexers, own_ids: frozenset | None,
                  stop_at: int | None = None, found=None, verify=None) -> dict:
    """Count releases that could replace this file if the exact upload dies.

    ``own_ids`` is the set of message-ids of our NZB. A candidate whose NZB shares
    any message-id with it is the same upload, whatever it is called, and does not
    count. Size never decides. Two candidates that share message-ids count once.

    ``found`` is a search result the caller already has (else it searches once).
    ``verify(raw)`` is called with a disjoint candidate's NZB bytes and says whether its
    articles are all there: True counts it, False drops it (``dead`` counts these), None
    means it could not be checked (unsure; no more candidates are tried).

    Each candidate that passes the title and floor checks costs one NZB grab
    (cached per client). ``stop_at`` ends the loop once that many are verified, so
    a caller that only needs "is there one" pays for as few grabs as it can. An alternative
    with no NZB behind its listing, or known to have none, counts as dead (a "no"). After
    ``MAX_FAILED_GRABS`` failed grabs the row is unsure and no more are tried; a
    spent grab budget, an indexer that cannot be asked or a failed transport also make it
    unsure. None of these is a "no".

    Returns {count, floor, best, below_floor, dead, partial, unsure, note, candidate}.
    ``candidate`` is {title, nzb} of the first verified alternative (its NZB bytes), or None.
    ``partial`` means some indexer did not answer; ``unsure`` means ``partial`` or
    some candidate's NZB could not be checked, or ``own_ids`` was not given.
    With ``unsure``, a count of 0 means "not checked", not "no restore path".
    Raises NewznabError when no indexer answered at all.
    """
    ident = identity(row)
    floor = resolution_floor(source_resolution(row))
    if found is None:
        found = _search(indexers, ident)
    partial = bool(getattr(found, "partial", False))
    unsure = partial or own_ids is None
    own_name = _clean(release_name_of(row))
    cands, below = [], 0
    for rel in found:
        if not title_matches(rel.title, ident["title"] or "", ident["year"],
                             ident["season"], ident["episode"]):
            continue
        if rel.resolution < floor:
            below += 1
            continue
        cands.append(rel)
    # a name that differs from ours is the better bet for a different upload, so it is
    # tried first; the name never decides, only the message-ids do
    cands.sort(key=lambda r: (_clean(r.title) == own_name, -r.resolution, _clean(r.title), r.link))
    counted: list[frozenset] = []
    best = 0
    fails = 0
    dead = 0
    candidate = None
    for rel in cands if own_ids is not None else ():
        if stop_at and len(counted) >= stop_at:
            break
        try:
            ids, raw = indexers.fetch_nzb_ids(rel)
        except GrabBudgetSpent:
            unsure = True
            continue
        except BadRelease:
            dead += 1  # known to have no NZB: no restore path, and no grab was spent
            continue
        except NotNzb:
            # one page that is no NZB may be a maintenance page: only a strike, so the
            # path is unchecked. Dead once the strike confirms (no NZB on two nights)
            if indexers.is_bad(rel):
                dead += 1
            else:
                unsure = True
            fails += 1
            if fails >= MAX_FAILED_GRABS:
                unsure = True  # the rest were not asked
                break
            continue
        except ItemFailed:
            dead += 1  # the indexer says the item is gone (300, 404, 410): a dead one
            fails += 1
            if fails >= MAX_FAILED_GRABS:
                unsure = True  # the rest were not asked
                break
            continue
        except _ERRORS:
            unsure = True
            fails += 1
            if fails >= MAX_FAILED_GRABS:
                break
            continue
        if not ids or ids & own_ids:
            continue  # the same upload (or nothing to compare)
        if any(ids & c for c in counted):
            continue  # the same upload as one already counted
        if verify is not None:
            v = verify(raw)
            if v is None:
                unsure = True  # a provider could not be asked: more tries would not help
                break
            if not v:
                dead += 1  # an alternative whose articles are gone is no restore path
                continue
        counted.append(ids)
        best = max(best, rel.resolution)
        if candidate is None:
            candidate = {"title": rel.title, "nzb": raw}
    count = len(counted)
    if count:
        note = f"{count} alternative(s), best {best}p"
    elif unsure:
        note = "restore path unchecked (an indexer or NZB did not answer)"
    elif dead:
        note = f"alternatives found but none can be restored: no NZB or articles gone ({dead})"
    elif below:
        note = f"only below-floor alternatives ({below}), floor {floor}p"
    else:
        note = "no alternative release found"
    return {"count": count, "floor": floor, "best": best, "below_floor": below, "dead": dead,
            "partial": partial, "unsure": unsure, "note": note, "candidate": candidate}


def _find_exact(found, name: str):
    want = _clean(name)
    for rel in found:
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
    """Run the five steps. Every call records an attempt for the row (so ``oldest_first``
    rotates it), whichever way it ends."""
    noted: list = []
    try:
        return _qualify(row, noted, reader=reader, importer=importer, indexers=indexers,
                        stat_primary=stat_primary, stat_secondary=stat_secondary,
                        archive_dir=archive_dir, clock=clock)
    finally:
        if not noted:
            fn = getattr(indexers, "note_attempt", None)
            if fn:
                fn(release_name_of(row), "keep")


def _qualify(row: dict, noted: list, *, reader, importer, indexers, stat_primary,
             stat_secondary, archive_dir: str, clock=time.time) -> QualifyResult:
    fields: dict = {}
    steps: dict = {}
    name = release_name_of(row)
    memo: list = []

    def searched():
        """The one search of this qualify, shared by the exact-NZB and restore steps."""
        if not memo:
            memo.append(_search(indexers, identity(row)))
        return memo[0]

    def note_attempt(streak: str) -> int:
        """Record this attempt for the row (when the client keeps such a record)."""
        noted.append(streak)
        fn = getattr(indexers, "note_attempt", None)
        return fn(name, streak) if fn else 0

    def fail(step: str, reason: str, **info) -> QualifyResult:
        steps[step] = {"ok": False, **info}
        return QualifyResult(False, [reason], fields, steps)

    # 1. exact NZB
    nzb_path = row.get("nzb_path")
    nzb_bytes = None
    try:
        if nzb_path and os.path.exists(nzb_path):
            nzb_bytes = read_archived(nzb_path)
            note_attempt("reset")  # it has an NZB: nothing of the row's is failing here
            steps["nzb"] = {"ok": True, "reused": True}
        else:
            fails = 0
            items = 0  # candidates that failed as items: no NZB behind the listing
            try:
                found = searched()
            except IndexersUnavailable:
                note_attempt("keep")
                return fail("nzb", "nzb_unsure", why="indexers unavailable")
            unsure = bool(getattr(found, "partial", False))  # an indexer was not asked
            others = unsure  # trouble that is not the row's
            for rel in _find_exact(found, name):
                try:
                    nzb_bytes = indexers.fetch_nzb(rel)
                    break
                except GrabBudgetSpent:
                    unsure = others = True
                    continue
                except BadRelease:
                    unsure = True  # known to have no NZB: no grab was spent, so no failed try
                    items += 1
                    continue
                except NotNzb:
                    # one page that is no NZB may be the indexer's maintenance page: not the
                    # row's fault until the strike confirms on a second night
                    unsure = True
                    if indexers.is_bad(rel):
                        items += 1
                    else:
                        others = True
                    fails += 1
                    if fails >= MAX_FAILED_GRABS:
                        break
                except ItemFailed:
                    unsure = True
                    items += 1
                    fails += 1
                    if fails >= MAX_FAILED_GRABS:
                        break
                except NewznabError:
                    unsure = others = True
                    fails += 1
                    if fails >= MAX_FAILED_GRABS:
                        break
            if nzb_bytes is None:
                if unsure and items and not others:
                    # every candidate failed as an item: the row's own trouble. A few nights
                    # of it and the release is called gone, so the row is not retried for ever
                    n = note_attempt("add")
                    if n >= GRAB_FAILED_NIGHTS:
                        note_attempt("reset")
                        return fail("nzb", "no_exact_nzb", why="grab_failed", nights=n)
                    return fail("nzb", "nzb_unsure", why="grab_failed", nights=n)
                note_attempt("keep" if unsure else "reset")
                # "not on any indexer" only after a complete search; else try again later
                return fail("nzb", "nzb_unsure" if unsure else "no_exact_nzb")
            note_attempt("reset")
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

    # 5. restore path: a different upload, checked on every provider like nzb_path
    fields["nzb_path2"] = None  # a stored one belongs to the release that was; set again below
    providers = (("secondary", stat_secondary), ("primary", stat_primary))
    dead_providers: dict = {}

    def verify(raw: bytes):
        r = stat_nzb_bytes(raw, providers, archive_dir, dead_providers)
        return True if r == "ok" else (False if r == "missing" else None)

    try:
        sc = score_restore(row, indexers, nzb_segment_ids(nzb_bytes), stop_at=1,
                           found=searched(), verify=verify)
    except IndexersUnavailable:
        return fail("restore", "restore_check_unsure", why="indexers unavailable")
    except _ERRORS as e:
        return fail("restore", "restore_check_failed", error=type(e).__name__)
    if sc["count"] or not sc["unsure"]:  # an unchecked 0 is not a score
        fields["restore_score"] = sc["count"]
    fields["restore_note"] = sc["note"]
    if sc["partial"]:
        return fail("restore", "restore_check_unsure", count=sc["count"])  # unsure is a fail
    if sc["count"] < 1:
        fields["nzb_path2"] = None
        if sc["unsure"]:
            return fail("restore", "restore_check_unsure", count=0)
        return fail("restore", "no_restore_path", count=0, below_floor=sc["below_floor"])
    cand = sc.get("candidate")
    if cand:  # kept beside the primary's NZB: weekly STATs it instead of grabbing again
        try:
            path2 = archive_nzb(cand["nzb"], archive_dir, cand["title"])
        except OSError as e:
            return fail("restore", "restore_archive_failed", error=type(e).__name__)
        if path2 == nzb_path or not nzb_segment_ids(read_archived(path2)).isdisjoint(
                nzb_segment_ids(nzb_bytes)):
            fields["restore_note"] = "restore NZB is the same upload"
            fields["restore_score"] = 0
            return fail("restore", "no_restore_path", count=0)
        fields["nzb_path2"] = path2
    steps["restore"] = {"ok": True, "count": sc["count"], "best": sc["best"]}
    return QualifyResult(True, [], fields, steps)


def oldest_first(rows, indexers) -> list:
    """``rows`` ordered by the time of their last qualify attempt, oldest (or never) first,
    so a spent budget does not starve the same rows night after night."""
    info = getattr(indexers, "attempt_info", None)
    if info is None:
        return list(rows)
    return sorted(rows, key=lambda r: info(release_name_of(r))[0])
