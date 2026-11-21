"""Crash-safe file operations for the offload tier.

offload / demote / promote follow "Offload order" and "Promote order" in
tiering/OFFLOAD_DESIGN.md. The library path L is never unlinked: it is only
ever replaced by an atomic exchange (renameat2 RENAME_EXCHANGE), and the entry
that comes out of the exchange is checked before it is dropped, so a file
that appeared at L meanwhile (an arr upgrade) is put back, never deleted.
Each step is idempotent and the last finished step is recorded in the state
store, so a crashed run resumes by calling the same function again.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import os
import secrets
import stat as stat_mod
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Protocol

import blake3

from . import paths
from . import state as st

log = logging.getLogger("tiering.offload")

MIB = 1024 * 1024
TAIL_BASE = 32 * MIB
CHUNK = 8 * MIB
CHUNK_SIZE = 4 * MIB  # granularity of the chunk list
TMP_SUFFIX = ".tierfs-tmp"

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02
RENAME_EXCHANGE = 2
AT_FDCWD = -100


class RemoteReader(Protocol):
    def read_range(self, remote_path: str, offset: int, length: int) -> bytes: ...


class LayoutError(ValueError):
    pass


class OpsError(RuntimeError):
    pass


class ShortRead(ConnectionError):
    """The remote returned fewer bytes than asked."""

    def __init__(self, offset, wanted, got):
        super().__init__(f"short read at {offset}: wanted {wanted}, got {got}")
        self.offset = offset


SHORT_LIMIT = 3  # consecutive attempts that end short at the same offset


class ChunkMismatch(OpsError):
    def __init__(self, offset):
        super().__init__(f"chunk at offset {offset} does not match the chunk list")
        self.offset = offset


@dataclass
class Result:
    ok: bool
    state: str | None
    step: int
    reason: str = ""
    dry_run: bool = False


class Layout(NamedTuple):
    head_len: int
    tail_len: int


def _point(name: str) -> None:
    """Named crash-injection point between syscalls. No-op; tests patch it."""


# --------------------------------------------------------------------------
# layout, ids, atomic writes


def plan_layout(lib_path, size: int, head_frac: float = 0.20) -> Layout:
    """head_len = ceil(size * head_frac) rounded up to 1 MiB;
    tail_len = 32 MiB + size mod 1 MiB, so the middle is 1 MiB aligned."""
    if size <= 0:
        raise LayoutError(f"{lib_path}: empty file")
    head = -(-int(size * head_frac * 1000000) // 1000000)  # ceil, tolerant of float noise
    head = max(1, head)
    head = -(-head // MIB) * MIB
    tail = TAIL_BASE + size % MIB
    if head + tail >= size:
        raise LayoutError(
            f"{lib_path}: size {size} too small (head {head} + tail {tail} >= size)"
        )
    return Layout(head, tail)


def new_id(db=None) -> str:
    """16 lowercase hex chars, random, never reused (checked against the db)."""
    while True:
        i = secrets.token_hex(8)
        if db is None or st.get(db, i) is None:
            return i


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(final: str, data: bytes) -> None:
    """Temp file in the same dir, fsync, rename, fsync of the dir."""
    tmp = f"{final}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, final)
    _fsync_dir(os.path.dirname(final) or ".")


def manifest_path(manifest_dir, id) -> str:
    return os.path.join(str(manifest_dir), f"{id}.json")


def chunks_path(manifest_dir, id) -> str:
    return os.path.join(str(manifest_dir), f"{id}.chunks")


def write_manifest(manifest_dir, manifest: dict) -> str:
    final = manifest_path(manifest_dir, manifest["id"])
    _write_atomic(final, json.dumps(manifest, sort_keys=True).encode())
    return final


def write_chunks(manifest_dir, id, blob: bytes) -> str:
    final = chunks_path(manifest_dir, id)
    _write_atomic(final, blob)
    return final


def _rm(path):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


# --------------------------------------------------------------------------
# low-level helpers


def _dev(path) -> int:
    return os.stat(path).st_dev


def _same_inode(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def punch_hole(fd: int, offset: int, length: int) -> None:
    if length <= 0:
        return
    libc = _libc()
    libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    libc.fallocate.restype = ctypes.c_int
    rc = libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, offset, length)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"fallocate(PUNCH_HOLE) failed: {os.strerror(e)}")


def exchange_available() -> bool:
    try:
        _libc().renameat2  # noqa: B018
        return True
    except AttributeError:
        return False


def _exchange(a: str, b: str) -> None:
    """Atomically swap two directory entries (renameat2 RENAME_EXCHANGE)."""
    libc = _libc()
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"renameat2(RENAME_EXCHANGE) failed: {os.strerror(e)}", a, b)


REAL_FS = frozenset({"xfs", "btrfs", "ext4", "zfs", "tmpfs"})


def _unescape_mount(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 4 <= len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def fs_type_of(path, mountinfo: str | None = None) -> str | None:
    """File system type of the longest mount point that holds `path`, read from
    /proc/self/mountinfo (or the given text). None if it cannot be determined."""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo") as f:
                mountinfo = f.read()
        except OSError:
            return None
    path = os.path.realpath(str(path))
    best = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mp = _unescape_mount(fields[4])
        base = mp.rstrip("/")
        if mp == "/" or path == mp or path.startswith(base + "/"):
            if best is None or len(mp) >= len(best[0]):  # a later line stacks on top
                best = (mp, right.split()[0])
    return best[1] if best else None


def _fs_type(path) -> str | None:
    return fs_type_of(path)


def _require_real_fs(*paths) -> None:
    """Refuse to work through a union or FUSE layer (see "Real file system only")."""
    for p in paths:
        t = _fs_type(p)
        if t is None:
            raise OpsError(f"{p}: cannot determine the file system type; refusing")
        if t not in REAL_FS:
            raise OpsError(f"{p}: file system type {t!r} is not a real disk file system; refusing")


def _try_exchange(tmp: str, lib: str) -> None:
    """_exchange, but a file system without RENAME_EXCHANGE leaves L untouched."""
    try:
        _exchange(tmp, lib)
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise OpsError("file system lacks RENAME_EXCHANGE") from e
        raise


def _rm_our_symlink(tmp: str, id: str, lib: str) -> None:
    """Remove tmp if it is a symlink we made for this id (never a regular file)."""
    ext = os.path.splitext(lib)[1]
    with contextlib.suppress(OSError):
        if os.path.islink(tmp) and os.path.basename(os.readlink(tmp)) == f"{id}{ext}":
            os.unlink(tmp)


def _scan(path, size, head_len, tail_len) -> dict:
    """One sequential read: sha256 of the file, blake3 of head and tail, and
    the chunk list (blake3 per CHUNK_SIZE piece of the middle). Refuses if the
    file changed while it was read."""
    mid_end = size - tail_len
    sha, hb, tb = hashlib.sha256(), blake3.blake3(), blake3.blake3()
    chunks, cur, cur_len = [], None, 0
    with open(path, "rb") as f:
        s0 = os.fstat(f.fileno())
        if s0.st_size != size:
            raise OpsError(f"{path}: size {s0.st_size} != expected {size}")
        off = 0
        while off < size:
            blk = f.read(min(MIB, size - off))  # regions start on 1 MiB boundaries
            if not blk:
                raise OpsError(f"{path}: short read at {off}")
            sha.update(blk)
            if off < head_len:
                hb.update(blk)
            elif off < mid_end:
                if cur is None:
                    cur, cur_len = blake3.blake3(), 0
                cur.update(blk)
                cur_len += len(blk)
                if cur_len >= CHUNK_SIZE or off + len(blk) >= mid_end:
                    chunks.append(cur.digest())
                    cur = None
            else:
                tb.update(blk)
            off += len(blk)
        s1 = os.fstat(f.fileno())
    if (s1.st_size, s1.st_mtime_ns) != (s0.st_size, s0.st_mtime_ns):
        raise OpsError(f"{path}: modified while it was being hashed")
    return {"sha256": sha.hexdigest(), "head_b3": hb.hexdigest(), "tail_b3": tb.hexdigest(),
            "chunks": b"".join(chunks), "dev": s0.st_dev, "ino": s0.st_ino,
            "size": s0.st_size, "mtime_ns": s0.st_mtime_ns}


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _b3_range(path, offset: int, length: int) -> str:
    h = blake3.blake3()
    with open(path, "rb") as f:
        f.seek(offset)
        left = length
        while left > 0:
            b = f.read(min(CHUNK, left))
            if not b:
                raise OpsError(f"{path}: short read hashing range")
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def scan_open_holders(path, ignore_comm=("tierfs",), ignore_unreadable=False) -> list[int]:
    """Pids holding `path`'s inode open, from /proc/*/fd. Fails closed:
    - a process whose fd table cannot be read counts as a holder
      (ignore_unreadable=True is for tests running unprivileged);
    - TIERING_PROC_CANARY is a comma list of process names (comm, cut to 15
      characters, e.g. the media server and shfs); every one must be visible; if it is unset or not visible the answer is "not
      clear" (pid -1), because the scan then cannot see the processes that
      matter (wrong PID namespace).
    Ignores this process and ignore_comm (tierfs preads head and tail only).
    Needs root or CAP_SYS_PTRACE, and a PID namespace shared with the media
    server."""
    target = os.stat(path)
    me = os.getpid()
    canaries = [c.strip() for c in (os.environ.get("TIERING_PROC_CANARY") or "").split(",")
                if c.strip()]
    seen_canary: set = set()
    holders = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        try:
            with open(f"/proc/{name}/comm") as f:
                comm = f.read().strip()
        except PermissionError:
            if not ignore_unreadable:
                holders.append(pid)
            continue
        except OSError:
            continue  # process went away
        if comm in canaries:
            seen_canary.add(comm)
        if comm in ignore_comm:
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except PermissionError:
            if not ignore_unreadable:
                holders.append(pid)
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                s = os.stat(f"/proc/{name}/fd/{fd}")
            except PermissionError:
                if not ignore_unreadable:
                    holders.append(pid)
                    break
                continue
            except OSError:
                continue
            if _same_inode(s, target):
                holders.append(pid)
                break
    if not canaries:
        log.warning("open check: TIERING_PROC_CANARY is not set; cannot call the file clear")
        holders.append(-1)
    elif seen_canary != set(canaries):
        log.warning("open check: canary process(es) %r not visible; cannot call the file clear",
                    sorted(set(canaries) - seen_canary))
        holders.append(-1)
    return holders


def _is_symlink_to(path, target) -> bool:
    """path is a symlink into the tierfs mount for this id. Matched on the entry's
    basename (<id><ext>), as promote does, so a mount spelled another way (a
    trailing "/.", a bind alias) still counts as ours."""
    return os.path.islink(path) and os.path.basename(os.readlink(path)) == os.path.basename(
        str(target))


def _norm_mount(p) -> str:
    return os.path.normpath(os.path.abspath(str(p)))


def _path_pairs(path_map):
    if path_map is None:
        path_map = os.environ.get("TIERING_PATH_MAP", "")
    if isinstance(path_map, str):
        return paths.parse_path_map(path_map)
    return list(path_map)


def _resolve_lib(share, path_map):
    """(disk_path, "") or ("", refusal). With no map the path is used as is;
    with one, exactly one existing disk path must come out."""
    try:
        pairs = _path_pairs(path_map)
    except ValueError as e:
        return "", f"bad TIERING_PATH_MAP: {e}"
    if not pairs:
        return str(share), ""
    cands = paths.disk_paths(share, pairs)
    if len(cands) != 1:
        return "", (f"{share}: maps to {len(cands)} disk paths; refusing "
                    "(a file must be on exactly one disk)")
    return cands[0], ""


def _pick_stub_root(lib, stub_root):
    """(stub_root, "") or ("", refusal). A given stub_root is used as is (its
    same-device check stays in _check_fresh). Otherwise TIERFS_STUB_ROOTS is
    matched by st_dev against the directory of the resolved disk path."""
    if stub_root:
        return str(stub_root), ""
    roots = [r.strip() for r in os.environ.get("TIERFS_STUB_ROOTS", "").split(",") if r.strip()]
    if not roots:
        return "", "stub_root not given and TIERFS_STUB_ROOTS unset"
    try:
        dev = _dev(os.path.dirname(lib) or ".")
    except OSError as e:
        return "", f"cannot stat the directory of L: {e}"
    match = []
    for r in roots:
        try:
            if _dev(r) == dev:
                match.append(r)
        except OSError:
            continue
    if len(match) != 1:
        return "", (f"{len(match)} stub roots in TIERFS_STUB_ROOTS are on the same disk as "
                    f"{lib}; need exactly one")
    return match[0], ""


def _record(db, id, step, state, hook):
    st.set_state(db, id, state, step=step)
    if hook is not None:
        hook(step)


class _StateMoved(Exception):
    """The row is no longer in the state this offload expects (someone failed it)."""


def _record_off(db, id, step, hook):
    """Offload step marker. It only writes while the row is still `offloading`, so a
    row another thread set to failed (a bad_chunk) is not overwritten."""
    if not st.update_if(db, id, "offloading", step=step):
        raise _StateMoved(f"row left state offloading before step {step}")
    if hook is not None:
        hook(step)


def _fail(db, id, reason, **detail):
    log.error("%s: %s", id, reason)
    st.set_state(db, id, "failed", restore_note=reason[:500])
    st.log_event(db, id, "failed", {"reason": reason, **detail})


def _stall(db, id, kind, reason, state, step):
    """Resumable stop: state is left alone so the same call can retry."""
    log.warning("%s: %s", id, reason)
    st.log_event(db, id, kind, {"reason": reason})
    return Result(False, state, step, reason)


def _no_exchange() -> str:
    return "" if exchange_available() else "renameat2 is unavailable; refusing to swap"


@contextlib.contextmanager
def _id_lock(db, id):
    """Non-blocking per-id flock, kept beside the state db. Yields True if held."""
    row = db.execute("PRAGMA database_list").fetchone()
    dbfile = row["file"] if row and row["file"] else ""
    base = os.path.join(os.path.dirname(dbfile) if dbfile else tempfile.gettempdir(), "locks")
    os.makedirs(base, exist_ok=True)
    fd = os.open(os.path.join(base, f"{id}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


_BUSY = "busy: another operation holds the lock for this id"


# --------------------------------------------------------------------------
# offload / demote


def offload(
    db,
    row: dict,
    *,
    stub_root=None,
    manifest_dir,
    tierfs_mount,
    apply: bool = False,
    open_check: Callable[[str], list] = scan_open_holders,
    open_timeout: float = 60.0,
    open_poll: float = 1.0,
    mount_wait: float = 30.0,
    head_frac: float = 0.20,
    after_step: Callable[[int], None] | None = None,
    path_map=None,
    _demote: bool = False,
) -> Result:
    """Offload one file. Dry run unless apply=True. Re-run to resume.
    row["lib_path"] is the share path; it is resolved to the disk path here (path_map or
    TIERING_PATH_MAP) and the database keeps the share path. stub_root defaults to the
    TIERFS_STUB_ROOTS entry on the same disk as L."""
    id = row.get("id")
    if not id:
        return Result(False, None, 0, "row has no id")
    with _id_lock(db, id) as held:
        if not held:
            return Result(False, None, 0, _BUSY)
        try:
            return _offload(db, row, stub_root, manifest_dir, _norm_mount(tierfs_mount), apply,
                            open_check, open_timeout, open_poll, mount_wait, head_frac,
                            after_step, _demote, path_map)
        except _StateMoved as e:
            return _moved(db, id, str(e))


def _moved(db, id, reason) -> Result:
    """Stop an offload whose row was changed under it. Nothing is punched or swapped
    back: L stays whatever it is (the tierfs symlink over a whole stub)."""
    cur = st.get(db, id) or {}
    log.error("%s: %s; offload stopped", id, reason)
    st.log_event(db, id, "offload_state_moved", {"reason": reason, "state": cur.get("state")})
    return Result(False, cur.get("state"), int(cur.get("step") or 0),
                  f"{reason}; stopped without punching")


def demote(db, row: dict, **kw) -> Result:
    """Offload a file that already has a verified sha256 and its NZB (nzb_path)."""
    return offload(db, row, _demote=True, **kw)


def _abort_offload(db, id, reason, lib, stub, manifest_dir, step, to_whole=False):
    """Back out before the swap: drop the stub link only if it is another name
    for a regular L, and remove the manifest and chunk list. While L is a
    symlink into tierfs the manifest stays (tierfs would drop its entry and L
    would dangle). A stub that is not a name of L is never deleted: it is the
    only copy of the original, and the row and an event name it."""
    ours_link = os.path.islink(lib) and os.path.basename(os.readlink(lib)) == os.path.basename(
        stub)
    orphan = False
    with contextlib.suppress(OSError):
        ls = os.lstat(lib)
        if stat_mod.S_ISREG(ls.st_mode) and os.path.lexists(stub):
            if _same_inode(ls, os.lstat(stub)):
                os.unlink(stub)
            else:
                orphan = True
    _rm_our_symlink(lib + TMP_SUFFIX, id, lib)
    if not ours_link:
        _rm(manifest_path(manifest_dir, id))
        _rm(chunks_path(manifest_dir, id))
    if to_whole and not ours_link and not orphan:
        st.set_state(db, id, "whole", step=0, stub_path=None)
        st.log_event(db, id, "offload_aborted", {"at_step": step, "reason": reason})
        return Result(False, "whole", 0, reason)
    if orphan:
        reason = (f"{reason}; the original is kept at {stub} (nothing else points at it), "
                  "and the file at L is unmanaged")
        st.log_event(db, id, "takeover_orphan", {"stub": stub, "lib": lib, "reason": reason})
    _fail(db, id, reason, **({"orphan_stub": stub} if orphan else {}))
    return Result(False, "failed", step, reason)


def _offload(db, row, stub_root, manifest_dir, tierfs_mount, apply, open_check,
             open_timeout, open_poll, mount_wait, head_frac, after_step, _demote,
             path_map=None) -> Result:
    id = row["id"]
    dbrow = st.get(db, id) or {}
    cur = {**row, **{k: v for k, v in dbrow.items() if v is not None}}
    state = cur.get("state") or "whole"
    step = int(cur.get("step") or 0)
    share = cur.get("lib_path")  # the share path; the database keeps it
    if not share:
        return Result(False, state, step, "row has no lib_path")
    if state == "offloaded":
        return Result(True, state, step, "already offloaded", dry_run=not apply)
    if state not in ("whole", "offloading"):
        return Result(False, state, step, f"state {state!r} cannot be offloaded")
    lib, reason = _resolve_lib(share, path_map)  # every file operation uses the disk path
    if reason:
        return Result(False, state, step, reason)
    stub_root, reason = _pick_stub_root(lib, stub_root)
    if reason:
        return Result(False, state, step, reason)
    ext = os.path.splitext(lib)[1]
    stub = os.path.join(str(stub_root), f"{id}{ext}")
    target = os.path.join(str(tierfs_mount), f"{id}{ext}")
    tmp = lib + TMP_SUFFIX
    resume = state == "offloading" and step >= 1

    if not cur.get("nzb_path"):
        return Result(False, state, step, "row has no nzb_path (the byte-identical release's NZB)")
    if not cur.get("sha256"):
        return Result(False, state, step, "offload needs the sha256 that qualify wrote")
    if apply:
        if os.environ.get("TIERING_APPLY") != "1":
            return Result(False, state, step, "TIERING_APPLY=1 is not set; refusing to apply")
        if not os.environ.get("TIERING_PROC_CANARY"):
            return Result(False, state, step, "TIERING_PROC_CANARY is not set; refusing to apply")
        reason = _no_exchange()
        if reason:
            return Result(False, state, step, reason)

    if not resume:
        # ---- step 1: preconditions, then one full read for every hash
        reason = _check_fresh(cur, lib, stub_root, manifest_dir)
        if reason:
            return Result(False, state, step, reason)
        lst = os.lstat(lib)
        try:
            lay = plan_layout(lib, lst.st_size, head_frac)
        except LayoutError as e:
            return Result(False, state, step, str(e))
        for k, v in (("size", lst.st_size), ("mtime_ns", lst.st_mtime_ns)):
            if cur.get(k) is not None and cur[k] != v:
                return Result(False, state, step, f"{k} differs from recorded value; file changed")
        if not apply:
            log.info("dry run: would offload %s (%s): hash, link stub %s, chunk list, manifest, "
                     "exchange symlink -> %s, punch [%d, %d)", id, lib, stub, target,
                     lay.head_len, lst.st_size - lay.tail_len)
            return Result(True, state, step, "dry run", dry_run=True)
        try:
            sc = _scan(lib, lst.st_size, lay.head_len, lay.tail_len)
        except OpsError as e:
            return Result(False, state, step, str(e))
        if (sc["dev"], sc["ino"]) != (lst.st_dev, lst.st_ino):
            return Result(False, state, step, "L changed while hashing")
        if cur["sha256"] != sc["sha256"]:
            return Result(False, state, step, "sha256 differs from the recorded value; file changed")
        # head_b3/tail_b3 in the row may be left over from an earlier cycle; with the
        # same sha256 the fresh values are the truth and replace them below.
        for k in ("head_b3", "tail_b3"):
            if cur.get(k) and cur[k] != sc[k]:
                log.warning("%s: stale %s in the row replaced by the fresh value", id, k)
        facts = {
            **{k: v for k, v in cur.items() if k in st.COLUMNS},
            "id": id, "lib_path": share, "stub_path": stub, "remote_path": cur["remote_path"],
            "size": sc["size"], "mtime_ns": sc["mtime_ns"], "sha256": sc["sha256"],
            "head_len": lay.head_len, "tail_len": lay.tail_len,
            "head_b3": sc["head_b3"], "tail_b3": sc["tail_b3"],
            "dev": sc["dev"], "ino": sc["ino"], "state": "offloading", "step": 1,
        }
        st.upsert(db, facts)
        st.log_event(db, id, "offload_start", {"demote": _demote, "dev": sc["dev"], "ino": sc["ino"]})
        cur = st.get(db, id)
        step = 1
        if after_step:
            after_step(1)
    else:
        if cur.get("stub_path") != stub:
            return Result(False, state, step, "stub_path differs from stub_root; refusing to resume")
        if cur.get("dev") is None or cur.get("ino") is None:
            return Result(False, state, step, "row has no recorded inode; refusing to resume")
        if not apply:
            log.info("dry run: would resume offload of %s after step %d", id, step)
            return Result(True, state, step, "dry run", dry_run=True)

    size, mtime_ns = cur["size"], cur["mtime_ns"]
    head_len, tail_len = cur["head_len"], cur["tail_len"]
    orig = (cur["dev"], cur["ino"])

    # ---- step 2: hardlink L to the stub path (same inode that step 1 hashed)
    if step < 2:
        lst = os.lstat(lib)
        if not stat_mod.S_ISREG(lst.st_mode):
            return Result(False, state, step, "L is not a regular file at link step")
        if (lst.st_dev, lst.st_ino) != orig:
            return _abort_offload(db, id, "L is not the inode that was hashed", lib, stub,
                                  manifest_dir, step)
        if os.path.lexists(stub):
            if not _same_inode(os.lstat(stub), lst):
                _fail(db, id, "stub path exists and is a different file")
                return Result(False, "failed", step, "stub path exists and is a different file")
        else:
            cl = os.lstat(lib)
            if cl.st_nlink != 1:  # a hardlink appeared (e.g. a cross-seed) while hashing
                reason = f"L has {cl.st_nlink} hardlinks; offload aborted before linking"
                st.set_state(db, id, "whole", step=0, stub_path=None)
                st.log_event(db, id, "offload_aborted", {"at_step": step, "reason": reason})
                return Result(False, "whole", 0, reason)
            os.link(lib, stub)
            _point("offload.linked")
            _fsync_dir(str(stub_root))
        _record_off(db, id, 2, after_step)
        step = 2

    # ---- step 3: chunk list, then manifest (the stub is still whole)
    if step < 3:
        # Always re-read the still-whole stub: SHA-256 and the chunk list come from
        # the same read, and a stored hash is never trusted in its place.
        try:
            sc = _scan(stub, size, head_len, tail_len)
        except OpsError as e:
            return Result(False, state, step, str(e))
        if (sc["sha256"], sc["head_b3"], sc["tail_b3"]) != (
                cur["sha256"], cur["head_b3"], cur["tail_b3"]):
            return _abort_offload(db, id, "sha256 of the stub differs from the row; nothing swapped",
                                  lib, stub, manifest_dir, step)
        chunks_blob = sc["chunks"]
        write_chunks(manifest_dir, id, chunks_blob)
        _point("offload.chunks_written")
        write_manifest(manifest_dir, {
            "v": 1, "id": id, "ext": ext, "size": size, "mtime_ns": mtime_ns,
            "head_len": head_len, "tail_len": tail_len, "stub_path": stub,
            "remote_path": cur["remote_path"], "sha256": cur["sha256"],
            "head_b3": cur["head_b3"], "tail_b3": cur["tail_b3"],
            "chunk_size": CHUNK_SIZE, "chunks_b3": blake3.blake3(chunks_blob).hexdigest(),
        })
        _point("offload.manifest_written")
        _record_off(db, id, 3, after_step)
        step = 3

    # ---- step 4: symlink at L.tierfs-tmp, exchange it with L, check what came out
    if step < 4:
        res = _offload_swap(db, cur, lib, stub, tmp, target, manifest_dir, mount_wait, step)
        if res is not None:
            return res
        _fsync_dir(os.path.dirname(lib) or ".")
        _record_off(db, id, 4, after_step)
        step = 4

    # ---- step 5: no process may hold the old inode open
    if step < 5:
        if not _is_symlink_to(lib, target):
            _fail(db, id, "L is not the expected symlink after the swap")
            return Result(False, "failed", step, "L is not the expected symlink")
        deadline = time.monotonic() + open_timeout
        while True:
            holders = open_check(stub)
            if not holders:
                break
            if time.monotonic() >= deadline:
                return _stall(db, id, "open_handles",
                              f"old inode still open by pids {sorted(holders)}", "offloading", step)
            time.sleep(open_poll)
        _record_off(db, id, 5, after_step)
        step = 5

    # ---- step 6: re-check, punch holes, restore mtime, verify head and tail
    if step < 6:
        if not _is_symlink_to(lib, target):
            return Result(False, "offloading", step, "L is not the tierfs symlink; not punching")
        _require_real_fs(os.path.dirname(lib) or ".", stub, manifest_dir)
        now_state = (st.get(db, id) or {}).get("state")
        if now_state != "offloading":  # failed meanwhile (bad_chunk): the stub stays whole
            raise _StateMoved(f"row is {now_state!r}, not offloading, at the punch step")
        err = _punch_and_verify(db, id, cur, stub)
        if err:
            return Result(False, "offloading", step, err)
        _record_off(db, id, 6, after_step)
        step = 6

    # ---- step 7
    if not st.update_if(db, id, "offloading", state="offloaded", step=7,
                        last_ok_ts=time.time()):
        raise _StateMoved("row left state offloading before the final write")
    st.log_event(db, id, "offloaded", {"stub": stub})
    if after_step:
        after_step(7)
    return Result(True, "offloaded", 7)


def _offload_swap(db, cur, lib, stub, tmp, target, manifest_dir, mount_wait, step):
    """Returns None when L is the tierfs symlink and tmp is settled, else a Result."""
    id, size, mtime_ns = cur["id"], cur["size"], cur["mtime_ns"]
    orig = (cur["dev"], cur["ino"])
    if not _is_symlink_to(lib, target):
        lst = os.lstat(lib)
        if not stat_mod.S_ISREG(lst.st_mode) or (lst.st_dev, lst.st_ino) != orig:
            return _abort_offload(db, id, "L is no longer the file that was linked to the stub",
                                  lib, stub, manifest_dir, step)
        if lst.st_size != size or lst.st_mtime_ns != mtime_ns:
            return _abort_offload(db, id, "L was modified after hashing; not swapping",
                                  lib, stub, manifest_dir, step)
        if not _wait_for(lambda: _mount_serves(target, size), mount_wait):
            return _stall(db, id, "mount_wait",
                          "tierfs does not serve the entry yet; not swapping", "offloading", step)
        nl = os.lstat(lib).st_nlink
        if nl != 2:  # L plus the stub link; another name appeared since step 2
            return _abort_offload(db, id, f"L has {nl} links (expected 2) right before the swap; "
                                  "offload aborted, the file stays whole",
                                  lib, stub, manifest_dir, step, to_whole=True)
        if os.path.lexists(tmp):
            if not os.path.islink(tmp):
                return Result(False, "offloading", step, f"{tmp} exists and is not a symlink")
            os.unlink(tmp)
        os.symlink(target, tmp)
        _point("offload.tmp_symlink")
        _try_exchange(tmp, lib)  # atomic: L is the symlink, tmp holds whatever was at L
        _point("offload.exchanged")
    return _settle_offload_tmp(db, cur, lib, stub, tmp, manifest_dir, step)


def _settle_offload_tmp(db, cur, lib, stub, tmp, manifest_dir, step):
    id = cur["id"]
    try:
        ts = os.lstat(tmp)
    except FileNotFoundError:
        return None
    ok = (stat_mod.S_ISREG(ts.st_mode) and (ts.st_dev, ts.st_ino) == (cur["dev"], cur["ino"])
          and ts.st_size == cur["size"] and ts.st_mtime_ns == cur["mtime_ns"])
    if ok:
        try:
            ss = os.lstat(stub)
        except OSError:
            ss = None
        if ss is None or not stat_mod.S_ISREG(ss.st_mode) or (ss.st_dev, ss.st_ino) != (
                cur["dev"], cur["ino"]):
            # the stub no longer holds the original: tmp is its last name, keep it
            _exchange(tmp, lib)
            _rm_our_symlink(tmp, id, lib)
            return _abort_offload(db, id, "stub no longer holds the original; swap undone",
                                  lib, stub, manifest_dir, step)
        os.unlink(tmp)  # one more name for data the stub still holds
        _point("offload.tmp_unlinked")
        return None
    if os.path.islink(tmp):
        return Result(False, "offloading", step, f"{tmp} is an unexpected symlink")
    # not the file we hashed (a new import, or an edit): put it back
    _exchange(tmp, lib)
    if os.path.islink(tmp):
        os.unlink(tmp)
    return _abort_offload(db, id, "L was replaced or modified during the swap; swap undone",
                          lib, stub, manifest_dir, step)


def _check_fresh(cur, lib, stub_root, manifest_dir) -> str:
    if not cur.get("remote_path"):
        return "row has no remote_path"
    if not os.path.isdir(str(stub_root)) or not os.path.isdir(str(manifest_dir)):
        return "stub_root or manifest_dir is not a directory"
    try:
        lst = os.lstat(lib)
    except OSError as e:
        return f"cannot stat L: {e}"
    if stat_mod.S_ISLNK(lst.st_mode):
        return "L is already a symlink"
    if not stat_mod.S_ISREG(lst.st_mode):
        return "L is not a regular file"
    if lst.st_nlink != 1:
        return f"L has {lst.st_nlink} hardlinks"
    if _dev(str(stub_root)) != lst.st_dev:
        return "stub_root is on a different filesystem than L"
    try:
        _require_real_fs(os.path.dirname(lib) or ".", stub_root, manifest_dir)
    except OpsError as e:  # a refusal like the others, also in a dry run
        return str(e)
    return ""


def _mount_serves(target, size) -> bool:
    try:
        return os.stat(target).st_size == size
    except OSError:
        return False


def _wait_for(pred, timeout, poll=0.2) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _punch_and_verify(db, id, cur, stub) -> str:
    """Returns "" on success or a refusal reason (nothing punched)."""
    size, head_len, tail_len = cur["size"], cur["head_len"], cur["tail_len"]
    fd = os.open(stub, os.O_RDWR)
    try:
        fs = os.fstat(fd)
        if not stat_mod.S_ISREG(fs.st_mode) or (fs.st_dev, fs.st_ino) != (cur["dev"], cur["ino"]):
            return "stub is not the inode that was offloaded; not punching"
        if fs.st_nlink != 1:
            return f"stub has {fs.st_nlink} links (a live name still points at it); not punching"
        punch_hole(fd, head_len, size - tail_len - head_len)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.utime(stub, ns=(cur["mtime_ns"], cur["mtime_ns"]))
    s = os.stat(stub)
    if s.st_size != size:
        raise OpsError("stub size changed while punching")
    if s.st_blocks * 512 > head_len + tail_len + MIB:
        raise OpsError("hole punching did not release space")
    if (_b3_range(stub, 0, head_len) != cur["head_b3"]
            or _b3_range(stub, size - tail_len, tail_len) != cur["tail_b3"]):
        _fail(db, id, "head/tail hash mismatch after punching holes")
        raise OpsError("head/tail hash mismatch after punching holes")
    return ""


# --------------------------------------------------------------------------
# promote


def promote(
    db,
    id,
    reader: RemoteReader,
    *,
    apply: bool = False,
    chunk: int = CHUNK,
    manifest_dir=None,
    after_step: Callable[[int], None] | None = None,
    path_map=None,
) -> Result:
    """Fill the stub's middle from the remote (every chunk checked against the
    chunk list), verify sha256, swap it over L. On a mismatch nothing is
    swapped: state=failed and the symlink stays."""
    with _id_lock(db, id) as held:
        if not held:
            return Result(False, None, 0, _BUSY)
        return _promote(db, id, reader, apply, chunk, manifest_dir, after_step, path_map)


def _short_remote(db, id, offset, step):
    """A short read is transient the first times; the same offset SHORT_LIMIT times in a
    row means the remote is permanently short: stop (state=failed) and raise an alert."""
    st.log_event(db, id, "promote_short_read", {"offset": offset})
    run = 0
    for e in reversed(st.events(db, id)):
        if e["kind"] == "promote_short_read" and e["detail"].get("offset") == offset:
            run += 1
        else:
            break
    if run < SHORT_LIMIT:
        reason = f"short read at {offset} (attempt {run} of {SHORT_LIMIT})"
        log.warning("%s: %s", id, reason)
        return Result(False, "promoting", step, reason)
    reason = (f"the remote is permanently short at offset {offset} ({run} attempts in a row); "
              "promote cannot finish, use the restore path")
    _fail(db, id, reason, offset=offset)
    st.log_event(db, id, "promote_short_remote", {"offset": offset, "attempts": run})
    return Result(False, "failed", step, reason)


def _swapped(lib, stub, state, step) -> bool:
    """The swap is done: only meaningful for a row that is promoting past step 2.
    L is the stub's inode, or (stub already unlinked, step>=4) a regular file."""
    if state != "promoting" or step < 3:
        return False
    try:
        ls = os.lstat(lib)
    except OSError:
        return False
    if not stat_mod.S_ISREG(ls.st_mode):
        return False
    try:
        return _same_inode(ls, os.lstat(stub))
    except OSError:
        return step >= 4


def _promote(db, id, reader, apply, chunk, manifest_dir, after_step, path_map=None) -> Result:
    row = st.get(db, id)
    if row is None:
        return Result(False, None, 0, "unknown id")
    state, step = row["state"], int(row.get("step") or 0)
    if state == "whole":
        return Result(True, state, step, "already whole", dry_run=not apply)
    if apply and os.environ.get("TIERING_APPLY") != "1":
        return Result(False, state, step, "TIERING_APPLY=1 is not set; refusing to apply")
    if state not in ("offloaded", "promoting", "failed", "offloading"):
        return Result(False, state, step, f"state {state!r} cannot be promoted")
    manifest_dir = manifest_dir or os.environ.get("TIERFS_MANIFESTS")
    if not manifest_dir:
        return Result(False, state, step, "manifest_dir not given and TIERFS_MANIFESTS unset")
    share, stub = row["lib_path"], row["stub_path"]
    lib, reason = _resolve_lib(share, path_map) if share else ("", "")
    if reason:
        return Result(False, state, step, reason)
    size, head_len, tail_len = row["size"], row["head_len"], row["tail_len"]
    mtime_ns = row["mtime_ns"]
    if not (lib and stub and row.get("sha256") and size and head_len and tail_len):
        return Result(False, state, step, "row is missing facts needed to promote")
    ext = os.path.splitext(lib)[1]
    tmp = lib + TMP_SUFFIX
    mpath, cpath = manifest_path(manifest_dir, id), chunks_path(manifest_dir, id)

    def ours_symlink():
        return os.path.islink(lib) and os.path.basename(os.readlink(lib)) == f"{id}{ext}"

    # An offload that is stuck before the swap while L is still the original
    # file: abort it cleanly so the file stays whole and can be played.
    if state == "offloading":
        try:
            ls = os.lstat(lib)
        except OSError as e:
            return Result(False, state, step, f"cannot stat L: {e}")
        if stat_mod.S_ISREG(ls.st_mode):
            if (ls.st_dev, ls.st_ino) != (row.get("dev"), row.get("ino")):
                return Result(False, state, step, "L is not the file that was being offloaded")
            if not apply:
                log.info("dry run: would abort the offload of %s; the file stays whole", id)
                return Result(True, state, step, "dry run", dry_run=True)
            if os.path.lexists(stub) and _same_inode(os.lstat(stub), ls):
                os.unlink(stub)
            _rm_our_symlink(tmp, id, lib)
            _rm(mpath)
            _rm(cpath)
            st.set_state(db, id, "whole", step=0, stub_path=None)
            st.log_event(db, id, "offload_aborted", {"at_step": step})
            return Result(True, "whole", 0, "offload aborted; file is whole")
        if apply and ours_symlink() and os.path.lexists(tmp):
            # died between the exchange and the settle: whatever sits at tmp is
            # either our original (drop the extra link) or an arr upgrade (put it back)
            res = _settle_offload_tmp(db, row, lib, stub, tmp, manifest_dir, step)
            if res is not None:
                return res

    swapped = _swapped(lib, stub, state, step)
    if not swapped:
        if not os.path.isfile(stub) or os.path.islink(stub):
            return Result(False, state, step, "stub is missing")
        if not ours_symlink():
            return Result(False, state, step, "L is not the tierfs symlink for this id")
    reason = _no_exchange() if not swapped else ""
    if reason and apply:
        return Result(False, state, step, reason)

    # the chunk list is needed only while filling
    chunk_digests, chunk_size = None, CHUNK_SIZE
    if not swapped and (state != "promoting" or step < 1):
        try:
            with open(mpath) as f:
                man = json.load(f)
            with open(cpath, "rb") as f:
                blob = f.read()
        except (OSError, ValueError) as e:
            return Result(False, state, step, f"manifest or chunk list unreadable: {e}")
        chunk_size = int(man.get("chunk_size") or 0)
        mid = size - tail_len - head_len
        if (chunk_size <= 0 or blake3.blake3(blob).hexdigest() != man.get("chunks_b3")
                or len(blob) != 32 * -(-mid // chunk_size)):
            return Result(False, state, step, "chunk list is missing or does not match the manifest")
        chunk_digests = [blob[i:i + 32] for i in range(0, len(blob), 32)]

    if not apply:
        log.info("dry run: would promote %s: fill [%d, %d) from remote, check chunks and sha256, "
                 "exchange over %s", id, head_len, size - tail_len, lib)
        return Result(True, state, step, "dry run", dry_run=True)

    if state != "promoting":
        step = 0
        st.set_state(db, id, "promoting", step=0)
        st.log_event(db, id, "promote_start", {"from": state})
        state = "promoting"

    # ---- step 1: fill the middle (only that range, chunk by chunk), fsync
    if step < 1:
        try:
            _fill_middle(reader, row, stub, lib, chunk, chunk_size, chunk_digests)
        except ChunkMismatch as e:
            _fail(db, id, f"chunk mismatch from the remote at offset {e.offset}; nothing swapped",
                  offset=e.offset)
            return Result(False, "failed", step, "chunk mismatch")
        except OpsError as e:  # a refusal about the stub itself: retrying cannot help
            _fail(db, id, f"fill refused: {e}")
            return Result(False, "failed", step, str(e))
        except ShortRead as e:
            return _short_remote(db, id, e.offset, step)
        except Exception as e:  # reader trouble is transient: stay resumable
            return _stall(db, id, "promote_read_error", f"{type(e).__name__}: {e}", "promoting", step)
        _record(db, id, 1, "promoting", after_step)
        step = 1

    # ---- step 2: whole-file sha256
    if step < 2:
        got = _sha256_file(stub)
        if got != row["sha256"]:
            _fail(db, id, "sha256 mismatch after filling the stub; nothing swapped",
                  expected=row["sha256"], got=got)
            return Result(False, "failed", step, "sha256 mismatch")
        _record(db, id, 2, "promoting", after_step)
        step = 2

    # ---- step 3: restore mtime
    if step < 3:
        os.utime(stub, ns=(mtime_ns, mtime_ns))
        _record(db, id, 3, "promoting", after_step)
        step = 3

    # ---- step 4: hardlink stub to L.tierfs-tmp, exchange with L, check what came out
    if step < 4:
        res = _promote_swap(db, row, lib, stub, tmp, ours_symlink)
        if res is not None:
            return res
        _fsync_dir(os.path.dirname(lib) or ".")
        _record(db, id, 4, "promoting", after_step)
        step = 4

    # ---- step 5: drop the stub's extra link, the manifest and the chunk list
    ls = os.lstat(lib)
    if not stat_mod.S_ISREG(ls.st_mode):
        raise OpsError("L is not a regular file after the swap")
    if os.path.lexists(stub):
        if not _same_inode(os.lstat(stub), ls):
            raise OpsError("stub is not the same inode as L; refusing to unlink it")
        os.unlink(stub)
        _point("promote.stub_unlinked")
    _fsync_dir(os.path.dirname(stub) or ".")  # the unlink is durable before state=whole
    _rm(mpath)
    _point("promote.manifest_removed")
    _rm(cpath)
    _fsync_dir(os.path.dirname(lib) or ".")
    st.set_state(db, id, "whole", step=0, stub_path=None, head_b3=None, tail_b3=None,
                 last_ok_ts=time.time())
    st.log_event(db, id, "promoted", {})
    if after_step:
        after_step(5)
    return Result(True, "whole", 5)


def _promote_swap(db, row, lib, stub, tmp, ours_symlink):
    """None when L is the stub's inode and tmp is settled, else a Result."""
    id = row["id"]
    if not _same_regular(lib, stub):
        if not ours_symlink():
            _fail(db, id, "L is not the tierfs symlink at swap time")
            return Result(False, "failed", 3, "L changed before the swap")
        _require_real_fs(os.path.dirname(lib) or ".", stub)
        if os.path.lexists(tmp):
            if not os.path.islink(tmp) and not _same_inode(os.lstat(tmp), os.lstat(stub)):
                return Result(False, "promoting", 3, f"{tmp} exists and is not ours")
            os.unlink(tmp)
        os.link(stub, tmp)
        _point("promote.tmp_linked")
        _try_exchange(tmp, lib)  # atomic: L is the stub's inode, tmp holds whatever was at L
        _point("promote.exchanged")
    try:
        ts = os.lstat(tmp)
    except FileNotFoundError:
        return None
    if os.path.islink(tmp) and ours_tmp_symlink(ts, tmp, id, lib):
        os.unlink(tmp)
        _point("promote.tmp_unlinked")
        return None
    if stat_mod.S_ISREG(ts.st_mode) and _same_inode(ts, os.lstat(stub)):
        os.unlink(tmp)
        return None
    # something else was at L (a new import): put it back, keep the stub
    _exchange(tmp, lib)
    if os.path.lexists(tmp) and _same_regular(tmp, stub):
        os.unlink(tmp)
    reason = (f"L was replaced during the swap; swap undone; the original is kept at {stub}, "
              "and the file at L is unmanaged")
    st.log_event(db, id, "takeover_orphan", {"stub": stub, "lib": lib, "reason": reason})
    _fail(db, id, reason, orphan_stub=stub)
    return Result(False, "failed", 3, "L replaced during the swap")


def ours_tmp_symlink(ts, tmp, id, lib) -> bool:
    ext = os.path.splitext(lib)[1]
    return stat_mod.S_ISLNK(ts.st_mode) and os.path.basename(os.readlink(tmp)) == f"{id}{ext}"


def _same_regular(a, b) -> bool:
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return stat_mod.S_ISREG(sa.st_mode) and _same_inode(sa, sb)


def _fill_middle(reader, row, stub, lib, chunk, chunk_size, digests):
    start, end = row["head_len"], row["size"] - row["tail_len"]
    step_len = max(chunk_size, chunk // chunk_size * chunk_size)
    fd = os.open(stub, os.O_WRONLY)
    try:
        fs = os.fstat(fd)
        if not stat_mod.S_ISREG(fs.st_mode) or fs.st_nlink != 1:
            raise OpsError("stub is not a single-link regular file; refusing to write into it")
        try:
            ls = os.lstat(lib)
        except FileNotFoundError:
            ls = None
        if ls is not None and _same_inode(fs, ls):
            raise OpsError("stub is the same inode as L; refusing to write into the library file")
        off = start
        while off < end:
            n = min(step_len, end - off)
            data = reader.read_range(row["remote_path"], off, n)
            if len(data) != n:
                raise ShortRead(off, n, len(data))
            for p in range(0, n, chunk_size):  # check every chunk before any byte is written
                piece = data[p:p + chunk_size]
                idx = (off + p - start) // chunk_size
                if blake3.blake3(piece).digest() != digests[idx]:
                    raise ChunkMismatch(off + p)
            view = memoryview(data)
            w = 0
            while w < n:
                w += os.pwrite(fd, view[w:], off + w)
            off += n
        os.fsync(fd)
    finally:
        os.close(fd)
