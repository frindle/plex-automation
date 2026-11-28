"""The checks a file must pass before any local byte is deleted.

``qualify`` runs five steps in order and stops at the first failure:

1. find or reuse an NZB for the exact release, and archive it;
2. import it into AltMount;
3. hash ``lib_path`` itself with a fresh full read, then read the remote copy back
   through WebDAV by range reads (as tierfs plays it) and compare every chunk with
   ``lib_path``. A stored ``sha256`` is never trusted. A chunk that differs is read
   again (``compare_remote``): a read that comes right is a fluke, not a bad copy; the
   same wrong bytes every time is ``hash_mismatch``; different wrong bytes every time
   is ``hash_unsure`` (not tonight);
4. STAT every segment on the secondary and the primary provider: none missing
   (the primary alone when TIERING_SINGLE_PROVIDER left the secondary out);
5. restore path: another release of the same title, at least the resolution
   floor, that is a different upload (it shares no message-id with our NZB) and
   whose every segment is on both providers, like ``nzb_path``'s.

Then, best effort and never a failure, it looks for a twin (``find_twin``): a second
upload of the same bytes for a promote to read when the first copy fails. It runs last,
so it only spends the grabs the five steps left.

The first search is reused: one search per qualify, however many steps need it.

Row fields it reads:
  lib_path (required), nzb_path, release_name, title, year, imdb_id, tvdb_id,
  season, episode, is4k.
The source resolution comes from ``is4k`` or the release or file name. A 4K
source tries 4K alternatives first; 1080p is the last resort, never lower. When it
cannot be told, the floor is 1080p.

It does not write to the database. ``QualifyResult.fields`` holds only ``files``
columns for the caller to store: nzb_path, nzb_path2 (the archived NZB of the verified
restore candidate; always present, None when there is none, so a stale one is cleared),
twin_nzb_path and twin_remote_path (the same: always present, None without a twin),
remote_path, sha256, restore_score, restore_note, last_check_ts and last_stat_ok_ts. The result of each step is in
``QualifyResult.steps``, which is not a column.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError

from .altmount import AltMountError
from .newznab import (BadRelease, GrabBudgetSpent, IndexersUnavailable, ItemFailed, NewznabError, NotNzb, archive_nzb, nzb_segment_ids, parse_resolution,
                      read_archived, title_matches)
from .nntp_stat import NntpUnavailable, ProviderCfg
from .usenet_read import CannotTell, UsenetFile

_ERRORS = (AltMountError, NewznabError, NntpUnavailable, OSError, ParseError,
           EOFError, zlib.error, ValueError)
MAX_FAILED_GRABS = 3  # failed NZB grabs for one row before it is called unsure
GRAB_FAILED_NIGHTS = 3  # nights in a row a row's own grabs fail before it is called a miss
_READ = 1 << 20
TWIN_GRABS = 3  # NZB grabs one qualify may spend looking for a twin
TWIN_SAMPLES = 5  # places (1 MiB each) a twin's bytes are compared with the local file
HASH_CHUNK = 16 << 20  # bytes per range read when the remote copy is compared with L
HASH_REREADS = 2  # fresh reads of a chunk that differs, before the difference counts
HASH_REREAD_PAUSE = 2.0  # seconds before a reread (times its number)
HASH_WORKERS = 4  # range reads in flight during the compare
# Waits before each late read of a chunk AltMount serves wrong while the upload is L (17.5
# min in all). Seen live: a segment AltMount had restarted mid-read was served with its own
# head repeated for over 10 minutes, to every reader, then right again.
HASH_FAULT_PAUSES = (30.0, 120.0, 300.0, 600.0)
HASH_FAULT_MAX = 2  # chunks one compare waits out like that; more is a read fault at once
# A listing (or NZB file) is only "the same file" when its size is near the local file's.
# NZB sizes count par2 and yEnc overhead, so they run a little above the file: allow from
# SIZE_RATIO_MIN to SIZE_RATIO_MAX times the local size. A same-titled release of another
# edition (a much larger or smaller encode) falls outside and is skipped.
SIZE_RATIO_MIN = 0.98
SIZE_RATIO_MAX = 1.25
_ZEROS = b"\0" * (64 << 10)  # a run this long in the remote chunk, not in L: zero-filled


@dataclass
class QualifyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)  # files columns only
    steps: dict = field(default_factory=dict)   # per-step detail, for logs


def resolution_floor(src_res: int) -> int:
    """Lowest resolution an alternative may have, given the source's.

    Never below 1080p for a 1080p or 4K source: a 4K source may fall back to 1080p, but
    only as the last resort (``score_restore`` tries every 4K alternative first). A source
    below 1080p gets a 720p floor. An unknown source (0) gets 1080p.
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


def present(providers) -> tuple:
    """The providers that are set up. With TIERING_SINGLE_PROVIDER the secondary is None
    and every check runs on the primary alone."""
    return tuple((label, fn) for label, fn in providers if fn is not None)


def _stat_ok(raw: bytes, providers, tmp_dir: str, dead: dict):
    """True when every article is on every provider, False when one is missing, None
    when a provider could not be asked."""
    r = stat_nzb_bytes(raw, providers, tmp_dir, dead)
    return True if r == "ok" else (False if r == "missing" else None)


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

    Returns {count, floor, best, downgrade, below_floor, dead, partial, unsure, note,
    candidate}. ``downgrade`` is True when the best verified alternative is below the
    source's resolution (a 4K source with only 1080p left).
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
    # highest resolution first: upgrade or keep, never downgrade while another option is
    # left (a 4K source tries every 4K alternative before 1080p). Within a resolution a
    # name that differs from ours is the better bet for a different upload; the name never
    # decides, only the message-ids do
    cands.sort(key=lambda r: (-r.resolution, _clean(r.title) == own_name, _clean(r.title), r.link))
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
    src_res = source_resolution(row)
    downgrade = bool(count) and 0 < best < src_res
    if count:
        note = f"{count} alternative(s), best {best}p"
        if downgrade:
            note += f" (no {src_res}p alternative: {best}p is the last resort)"
    elif unsure:
        note = "restore path unchecked (an indexer or NZB did not answer)"
    elif dead:
        note = f"alternatives found but none can be restored: no NZB or articles gone ({dead})"
    elif below:
        note = f"only below-floor alternatives ({below}), floor {floor}p"
    else:
        note = "no alternative release found"
    return {"count": count, "floor": floor, "best": best, "downgrade": downgrade,
            "below_floor": below, "dead": dead,
            "partial": partial, "unsure": unsure, "note": note, "candidate": candidate}


def find_twin(found, name: str, own_ids: frozenset, lib_path: str, *, indexers, importer,
              reader, verify, grabs: int = TWIN_GRABS, own_link: str | None = None):
    """A second upload of the very same file, for a promote to read from when the first
    one fails. It has the same release name, no message-id in common with ``own_ids``,
    ``verify(raw)`` says every article is there, and once imported it has the local
    file's size and the local file's bytes at TWIN_SAMPLES places. A promote still checks
    every chunk it reads from it against the chunk list.

    Best effort: returns {title, nzb, remote_path} or None, and raises nothing of its
    own. At most ``grabs`` NZBs are grabbed (cached per client); the listing ``own_link``
    names (the one our upload came from) is never grabbed again."""
    try:
        size = os.path.getsize(lib_path)
    except OSError:
        return None
    seen: list[frozenset] = []
    for rel in _find_exact(found, name, size):
        if own_link and rel.link == own_link:
            continue
        if grabs <= 0:
            break
        grabs -= 1
        try:
            ids, raw = indexers.fetch_nzb_ids(rel)
        except GrabBudgetSpent:
            break
        except _ERRORS:
            continue
        if not ids or ids & own_ids or any(ids & s for s in seen):
            continue  # our own upload, or one already tried
        seen.append(ids)
        if nzb_problem(raw, size):
            _strike(indexers, rel)
            continue
        try:
            v = verify(raw)
            if v is None:
                break  # a provider could not be asked: more tries would not help
            if not v:
                continue
            path = importer.import_nzb(raw, name)
            if reader.size(path) != size or not _same_samples(reader, path, lib_path, size):
                continue  # the same name, other bytes (a repack, say)
        except _ERRORS:
            continue
        return {"title": rel.title, "nzb": raw, "remote_path": path}
    return None


def _same_samples(reader, remote_path: str, lib_path: str, size: int) -> bool:
    n = min(_READ, size)
    k = TWIN_SAMPLES if size > n else 1
    offs = sorted({(size - n) * i // max(1, k - 1) for i in range(k)})
    with open(lib_path, "rb") as f:
        for off in offs:
            if reader.read_range(remote_path, off, n) != os.pread(f.fileno(), n, off):
                return False
    return True


def _strike(indexers, rel) -> None:
    """A bad NZB is a strike against its release (it counts only once it repeats on a later
    night), when the client keeps such marks."""
    fn = getattr(indexers, "strike", None)
    if fn:
        fn(rel)


def size_in_ratio(size: int, local_size: int | None) -> bool:
    """Is ``size`` within SIZE_RATIO_MIN..SIZE_RATIO_MAX of the local file's size? An
    unknown size on either side (<= 0 or None) is not held against the candidate."""
    if not local_size or local_size <= 0 or not size or size <= 0:
        return True
    return SIZE_RATIO_MIN <= size / local_size <= SIZE_RATIO_MAX


def _find_exact(found, name: str, local_size: int | None = None):
    """Listings with the exact cleaned title and, when the local size is known, a listing
    size within the sane ratio of it (same title, other edition: skipped)."""
    want = _clean(name)
    for rel in found:
        if _clean(rel.title) == want and size_in_ratio(getattr(rel, "size", 0), local_size):
            yield rel


def nzb_problem(raw: bytes, local_size: int | None) -> str | None:
    """Why this NZB must not be imported, or None. It needs segment byte counts (an NZB
    without them makes the importer fail with "total size is zero"), and its largest
    non-par2 file, or for a release split into RAR volumes the sum of its non-par2 files,
    must be within the sane size ratio of the local file."""
    total, best, data = 0, 0, 0
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "file":
            continue
        n = 0
        for seg in el.iter():
            if seg.tag.rsplit("}", 1)[-1] == "segment":
                try:
                    n += max(0, int(seg.get("bytes") or 0))
                except ValueError:
                    pass
        total += n
        if ".par2" not in (el.get("subject") or "").lower():
            best = max(best, n)
            data += n
        el.clear()
    if total <= 0:
        return "zero_size"
    if best <= 0 or not (size_in_ratio(best, local_size) or size_in_ratio(data, local_size)):
        return "size_out_of_ratio"
    return None


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


def local_size_of(path) -> int:
    try:
        return os.path.getsize(str(path))
    except OSError:
        return -1  # compare_remote reports it


def _pread_all(fd: int, n: int, off: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, n - len(out), off + len(out))
        if not b:
            break
        out += b
    return bytes(out)


def compare_remote(reader, remote_path: str, lib_path: str, *, chunk: int | None = None,
                   rereads: int | None = None, workers: int | None = None,
                   sleep=time.sleep, usenet=None, fault_pauses=None) -> dict:
    """Compare the remote copy with ``lib_path`` byte for byte, by range reads.

    Range reads are how tierfs plays the file, so they are what has to be right. AltMount
    zero-fills a segment it could not fetch in time and still answers 206, so one read
    that differs proves nothing: the chunk is read again, each time with a fresh request,
    up to ``rereads`` times. A chunk that then matches counts as ``fixed``.

    AltMount has also served the same wrong bytes for a chunk on every read for minutes
    while the articles held L's bytes (a segment restarted mid-read, its head served
    again where its tail belongs). So a chunk that stays wrong is put to ``usenet(off, n)``
    (``UsenetFile.read``: the bytes the upload really holds, CRC-checked) when it is given:
      the upload is not L there   -> "mismatch", whatever the reads said;
      the upload is L there       -> AltMount's fault, not the copy's. The chunk is read
                                     again after each of ``fault_pauses``; right then, it
                                     counts as fixed (and in ``read_faults``); still wrong,
                                     the result is "read_fault";
      it cannot tell (CannotTell) -> as without it.

    ``result`` is one of:
      "ok"          every chunk matched (after rereads);
      "size"        the remote size is not L's;
      "mismatch"    a chunk came back the same wrong bytes on every read, or the upload
                    itself differs from L: the copy is not L;
      "unstable"    a chunk came back different on each read and never right: a flaky
                    read path, not proof that the copy is bad;
      "read_fault"  the upload is L, but AltMount kept serving other bytes for a chunk:
                    not proof that the copy is bad either.
    The first chunk that cannot be made to match ends the compare (``first_bad`` is its
    offset; ``zero_filled`` says whether the remote bytes hold a zero run that L lacks;
    ``usenet`` is what the upload said for it: "same", "differ" or None).
    Read errors that outlast the rereads raise AltMountError.
    """
    chunk = chunk or HASH_CHUNK
    rereads = HASH_REREADS if rereads is None else rereads
    workers = workers or HASH_WORKERS
    fault_pauses = HASH_FAULT_PAUSES if fault_pauses is None else fault_pauses
    local_size = os.path.getsize(lib_path)
    remote_size = reader.size(remote_path)
    out = {"result": "ok", "chunks": 0, "rereads": 0, "fixed": 0, "first_bad": None,
           "zero_filled": False, "remote_size": remote_size, "local_size": local_size,
           "read_faults": 0, "usenet": None}
    if remote_size != local_size:
        out["result"] = "size"
        return out
    stop = threading.Event()
    faults = []  # chunks whose wrong bytes were AltMount's
    fmu = threading.Lock()

    def upload_says(off, n, want):
        """"same" when the upload holds L's bytes here, "differ" when not, None when it
        cannot be asked."""
        if usenet is None:
            return None
        try:
            theirs = usenet(off, n)
        except CannotTell:
            return None
        except Exception:  # best effort: a fault in here is never a verdict on the copy
            return None
        return "same" if theirs == want else "differ"

    def wait_out(off, n, want, done):
        """Read a chunk the upload has right again after each pause. True when it comes
        right."""
        with fmu:
            faults.append(off)
            if len(faults) > HASH_FAULT_MAX:
                return False
        for pause in fault_pauses:
            if stop.is_set():
                return False
            sleep(pause)
            done[0] += 1
            try:
                got = reader.read_range(remote_path, off, n)
            except AltMountError:
                continue
            if got == want:
                return True
        return False

    def one(fd, off):
        if stop.is_set():
            return None
        n = min(chunk, local_size - off)
        want = _pread_all(fd, n, off)
        seen = []
        for attempt in range(rereads + 1):
            if attempt:
                sleep(HASH_REREAD_PAUSE * attempt)
            try:
                got = reader.read_range(remote_path, off, n)
            except AltMountError:
                if attempt == rereads:
                    if not seen:
                        raise
                    break
                continue
            if got == want:
                return {"off": off, "rereads": attempt, "bad": None}
            seen.append(got)
        z = _ZEROS[:max(1, min(len(_ZEROS), n))]
        zero = any(z in g and z not in want for g in seen)
        says = upload_says(off, n, want)
        if says == "differ":
            return {"off": off, "rereads": rereads, "bad": "mismatch", "zero": zero,
                    "usenet": says}
        if says == "same":
            done = [0]
            if wait_out(off, n, want, done):
                return {"off": off, "rereads": rereads + done[0], "bad": None, "fault": True}
            if stop.is_set():
                return None
            return {"off": off, "rereads": rereads + done[0], "bad": "read_fault",
                    "zero": zero, "usenet": says}
        # the same wrong bytes twice is a bad copy; one wrong read among errors is not
        same = len({hashlib.sha256(g).digest() for g in seen}) == 1 and \
            (len(seen) >= 2 or rereads == 0)
        return {"off": off, "rereads": rereads, "bad": "mismatch" if same else "unstable",
                "zero": zero}

    fd = os.open(lib_path, os.O_RDONLY)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(one, fd, off) for off in range(0, local_size, chunk)]
            try:
                for f in futs:  # in offset order, so first_bad is the lowest bad chunk
                    r = f.result()
                    if r is None:
                        continue
                    out["rereads"] += r["rereads"]
                    if r["bad"]:
                        out.update(result=r["bad"], first_bad=r["off"], zero_filled=r["zero"],
                                   usenet=r.get("usenet"))
                        stop.set()
                        break
                    out["chunks"] += 1
                    out["fixed"] += 1 if r["rereads"] else 0
                    out["read_faults"] += 1 if r.get("fault") else 0
            finally:
                stop.set()  # an error or a bad chunk: the queued reads are skipped
    finally:
        os.close(fd)
    return out


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

    try:
        local_size = os.path.getsize(str(row.get("lib_path") or ""))
    except OSError:
        local_size = None  # the hash step reports it

    def bad_nzb(raw, rel=None):
        try:
            why = nzb_problem(raw, local_size)
        except ParseError:
            why = "unparsable"
        if why is None:
            return None
        if rel is not None:
            _strike(indexers, rel)
        return fail("nzb", "nzb_bad", why=why)

    # 1. exact NZB
    nzb_path = row.get("nzb_path")
    nzb_bytes = None
    own_link = None  # the listing our NZB was grabbed from this time
    try:
        if nzb_path and os.path.exists(nzb_path):
            nzb_bytes = read_archived(nzb_path)
            note_attempt("reset")  # it has an NZB: nothing of the row's is failing here
            steps["nzb"] = {"ok": True, "reused": True}
            if (res := bad_nzb(nzb_bytes)) is not None:
                return res
        else:
            fails = 0
            grabbed = None
            items = 0  # candidates that failed as items: no NZB behind the listing
            try:
                found = searched()
            except IndexersUnavailable:
                note_attempt("keep")
                return fail("nzb", "nzb_unsure", why="indexers unavailable")
            unsure = bool(getattr(found, "partial", False))  # an indexer was not asked
            others = unsure  # trouble that is not the row's
            for rel in _find_exact(found, name, local_size):
                try:
                    nzb_bytes = indexers.fetch_nzb(rel)
                    own_link = rel.link
                    grabbed = rel
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
            if (res := bad_nzb(nzb_bytes, grabbed)) is not None:
                return res
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
    # a chunk that stays wrong is checked against the articles themselves
    cfg = next((getattr(c, "cfg", None) for c in (stat_primary, stat_secondary)
                if isinstance(getattr(c, "cfg", None), ProviderCfg)), None)
    usenet = UsenetFile(nzb_bytes, cfg, local_size_of(lib_path)) if cfg and nzb_bytes else None
    try:
        cmp = compare_remote(reader, remote_path, str(lib_path),
                             usenet=usenet.read if usenet else None)
    except _ERRORS as e:
        return fail("hash", "read_failed", error=type(e).__name__)
    finally:
        if usenet:
            usenet.close()
    info = {k: cmp[k] for k in ("chunks", "rereads", "fixed", "first_bad", "zero_filled")}
    if cmp.get("read_faults"):
        info["read_faults"] = cmp["read_faults"]
    if cmp["result"] == "unstable":
        # different wrong bytes on every read: the read path is flaky tonight, which
        # proves nothing about the copy. Not tonight, and nothing is recorded.
        return fail("hash", "hash_unsure", **info)
    if cmp["result"] == "read_fault":
        # the articles are L's bytes, but AltMount kept serving others: its fault, not the
        # copy's. Not tonight; the runner raises an alert.
        return fail("hash", "hash_unsure", kind="read_fault", usenet=cmp["usenet"], **info)
    if cmp["result"] != "ok":
        return fail("hash", "hash_mismatch", kind=cmp["result"], remote_size=cmp["remote_size"],
                    local_size=cmp["local_size"],
                    **({"usenet": cmp["usenet"]} if cmp.get("usenet") else {}), **info)
    steps["hash"] = {"ok": True, **info}

    # 4. every segment on both providers
    for label, fn in present((("secondary", stat_secondary), ("primary", stat_primary))):
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

    fields["twin_nzb_path"] = fields["twin_remote_path"] = None  # set again in step 6

    # 5. restore path: a different upload, checked on every provider like nzb_path
    fields["nzb_path2"] = None  # a stored one belongs to the release that was; set again below
    providers = present((("secondary", stat_secondary), ("primary", stat_primary)))
    dead_providers: dict = {}

    def verify(raw: bytes):
        return _stat_ok(raw, providers, archive_dir, dead_providers)

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
    steps["restore"] = {"ok": True, "count": sc["count"], "best": sc["best"],
                        "downgrade": sc["downgrade"]}

    # 6. a twin: the same bytes on a second upload, for a promote to switch to when the
    # first fails mid-stream. Best effort, and last, so it only spends the grabs the
    # required steps left: a file without one still qualifies.
    try:
        twin = find_twin(searched(), name, nzb_segment_ids(nzb_bytes), str(lib_path),
                         indexers=indexers, importer=importer, reader=reader,
                         verify=lambda raw: _stat_ok(raw, providers, archive_dir, {}),
                         own_link=own_link)
    except (IndexersUnavailable, *_ERRORS):
        twin = None
    if twin:
        try:
            tpath = archive_nzb(twin["nzb"], archive_dir, twin["title"])
        except OSError:
            tpath = None
        if tpath and tpath != nzb_path:
            fields["twin_nzb_path"], fields["twin_remote_path"] = tpath, twin["remote_path"]
    steps["twin"] = {"ok": True, "found": bool(fields["twin_remote_path"])}
    return QualifyResult(True, [], fields, steps)


def oldest_first(rows, indexers) -> list:
    """``rows`` ordered by the time of their last qualify attempt, oldest (or never) first,
    so a spent budget does not starve the same rows night after night."""
    info = getattr(indexers, "attempt_info", None)
    if info is None:
        return list(rows)
    return sorted(rows, key=lambda r: info(release_name_of(r))[0])
