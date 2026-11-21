"""Crash-safe file operations for the offload tier.

offload / demote / promote follow "Offload order" and "Promote order" in
tiering/OFFLOAD_DESIGN.md. The library path L is never unlinked: it is only
ever replaced by an atomic rename, so it resolves at every step boundary.
Each step is idempotent and the last finished step is recorded in the state
store, so a crashed run resumes by calling the same function again.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import secrets
import stat as stat_mod
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Protocol

import blake3

from . import state as st

log = logging.getLogger("tiering.offload")

MIB = 1024 * 1024
TAIL_LEN = 32 * MIB
CHUNK = 8 * MIB
TMP_SUFFIX = ".tierfs-tmp"

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02


class RemoteReader(Protocol):
    def read_range(self, remote_path: str, offset: int, length: int) -> bytes: ...


class LayoutError(ValueError):
    pass


class OpsError(RuntimeError):
    pass


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


# --------------------------------------------------------------------------
# layout, ids, manifest


def plan_layout(lib_path, size: int, head_frac: float = 0.20) -> Layout:
    """head_len = ceil(size * head_frac) rounded up to 1 MiB; tail_len = 32 MiB."""
    if size <= 0:
        raise LayoutError(f"{lib_path}: empty file")
    head = -(-int(size * head_frac * 1000000) // 1000000)  # ceil, tolerant of float noise
    head = max(1, head)
    head = -(-head // MIB) * MIB
    if head + TAIL_LEN >= size:
        raise LayoutError(
            f"{lib_path}: size {size} too small (head {head} + tail {TAIL_LEN} >= size)"
        )
    return Layout(head, TAIL_LEN)


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


def manifest_path(manifest_dir, id) -> str:
    return os.path.join(str(manifest_dir), f"{id}.json")


def write_manifest(manifest_dir, manifest: dict) -> str:
    """Atomic: temp file in the same dir, fsync, rename, fsync of the dir."""
    final = manifest_path(manifest_dir, manifest["id"])
    tmp = f"{final}.tmp.{os.getpid()}"
    data = json.dumps(manifest, sort_keys=True).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, final)
    _fsync_dir(str(manifest_dir))
    return final


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


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def scan_open_holders(path, ignore_comm=("tierfs",)) -> list[int]:
    """Pids holding `path`'s inode open, from /proc/*/fd. Ignores this process
    and processes named in ignore_comm (tierfs reads head and tail via pread).
    Raises OSError if /proc is unavailable: an unknown answer is never "clear"."""
    target = os.stat(path)
    me = os.getpid()
    holders, unreadable = [], 0
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/comm") as f:
                if f.read().strip() in ignore_comm:
                    continue
            fds = os.listdir(f"/proc/{name}/fd")
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue  # process went away
        for fd in fds:
            try:
                s = os.stat(f"/proc/{name}/fd/{fd}")
            except OSError:
                continue
            if _same_inode(s, target):
                holders.append(int(name))
                break
    if unreadable:
        log.warning("open check: %d process(es) unreadable in /proc; run with enough privilege", unreadable)
    return holders


def _is_symlink_to(path, target) -> bool:
    return os.path.islink(path) and os.readlink(path) == str(target)


def _record(db, id, step, state, hook):
    st.set_state(db, id, state, step=step)
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


# --------------------------------------------------------------------------
# offload / demote


def offload(
    db,
    row: dict,
    *,
    stub_root,
    manifest_dir,
    tierfs_mount,
    apply: bool = False,
    open_check: Callable[[str], list] = scan_open_holders,
    open_timeout: float = 60.0,
    open_poll: float = 1.0,
    mount_wait: float = 30.0,
    head_frac: float = 0.20,
    after_step: Callable[[int], None] | None = None,
    _demote: bool = False,
) -> Result:
    """Offload one file. Dry run unless apply=True. Re-run to resume."""
    id = row.get("id")
    if not id:
        return Result(False, None, 0, "row has no id")
    dbrow = st.get(db, id) or {}
    cur = {**row, **{k: v for k, v in dbrow.items() if v is not None}}
    state = cur.get("state") or "whole"
    step = int(cur.get("step") or 0)
    lib = cur.get("lib_path")
    if not lib:
        return Result(False, state, step, "row has no lib_path")
    if state == "offloaded":
        return Result(True, state, step, "already offloaded", dry_run=not apply)
    if state not in ("whole", "offloading"):
        return Result(False, state, step, f"state {state!r} cannot be offloaded")
    ext = os.path.splitext(lib)[1]
    stub = os.path.join(str(stub_root), f"{id}{ext}")
    target = os.path.join(str(tierfs_mount), f"{id}{ext}")
    tmp = lib + TMP_SUFFIX
    resume = state == "offloading" and step >= 1

    if _demote:
        if not cur.get("sha256") or not (cur.get("nzb_path") or cur.get("nzb_path2")):
            return Result(False, state, step, "demote needs a verified sha256 and an NZB")

    if not resume:
        # ---- step 1: preconditions and facts (read-only until recorded)
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
            log.info("dry run: would offload %s (%s): link stub %s, manifest, symlink -> %s, "
                     "punch [%d, %d)", id, lib, stub, target, lay.head_len, lst.st_size - lay.tail_len)
            return Result(True, state, step, "dry run", dry_run=True)
        head_b3 = _b3_range(lib, 0, lay.head_len)
        tail_b3 = _b3_range(lib, lst.st_size - lay.tail_len, lay.tail_len)
        for k, v in (("head_b3", head_b3), ("tail_b3", tail_b3)):
            if cur.get(k) and cur[k] != v:
                return Result(False, state, step, f"{k} differs from recorded value; file changed")
        sha = cur.get("sha256") or _sha256_file(lib)
        facts = {
            **{k: v for k, v in cur.items() if k in st.COLUMNS},
            "id": id, "lib_path": lib, "stub_path": stub, "remote_path": cur["remote_path"],
            "size": lst.st_size, "mtime_ns": lst.st_mtime_ns, "sha256": sha,
            "head_len": lay.head_len, "tail_len": lay.tail_len,
            "head_b3": head_b3, "tail_b3": tail_b3,
            "state": "offloading", "step": 1,
        }
        st.upsert(db, facts)
        st.log_event(db, id, "offload_start", {"demote": _demote})
        cur = st.get(db, id)
        step = 1
        if after_step:
            after_step(1)
    else:
        if cur.get("stub_path") != stub:
            return Result(False, state, step, "stub_path differs from stub_root; refusing to resume")
        if not apply:
            log.info("dry run: would resume offload of %s after step %d", id, step)
            return Result(True, state, step, "dry run", dry_run=True)

    size, mtime_ns = cur["size"], cur["mtime_ns"]
    head_len, tail_len = cur["head_len"], cur["tail_len"]

    # ---- step 2: hardlink L to the stub path
    if step < 2:
        lst = os.lstat(lib)
        if not stat_mod.S_ISREG(lst.st_mode):
            return Result(False, state, step, "L is not a regular file at link step")
        if os.path.lexists(stub):
            if not _same_inode(os.lstat(stub), lst):
                _fail(db, id, "stub path exists and is a different file")
                return Result(False, "failed", step, "stub path exists and is a different file")
        else:
            os.link(lib, stub)
            _fsync_dir(str(stub_root))
        _record(db, id, 2, "offloading", after_step)
        step = 2

    # ---- step 3: manifest (the stub is still whole)
    if step < 3:
        write_manifest(manifest_dir, {
            "v": 1, "id": id, "ext": ext, "size": size, "mtime_ns": mtime_ns,
            "head_len": head_len, "tail_len": tail_len, "stub_path": stub,
            "remote_path": cur["remote_path"], "sha256": cur["sha256"],
            "head_b3": cur["head_b3"], "tail_b3": cur["tail_b3"],
        })
        _record(db, id, 3, "offloading", after_step)
        step = 3

    # ---- step 4: symlink at L.tierfs-tmp, rename over L (atomic)
    if step < 4:
        if not _is_symlink_to(lib, target):
            lst = os.lstat(lib)
            if not stat_mod.S_ISREG(lst.st_mode) or not _same_inode(lst, os.lstat(stub)):
                _fail(db, id, "L is no longer the file that was linked to the stub")
                return Result(False, "failed", step, "L changed before the swap")
            if lst.st_size != size or lst.st_mtime_ns != mtime_ns:
                _fail(db, id, "L was modified after hashing; not swapping")
                return Result(False, "failed", step, "L modified before the swap")
            if not _wait_for(lambda: _mount_serves(target, size), mount_wait):
                return _stall(db, id, "mount_wait",
                              "tierfs does not serve the entry yet; not swapping", "offloading", step)
            if os.path.lexists(tmp):
                if not os.path.islink(tmp):
                    return Result(False, state, step, f"{tmp} exists and is not a symlink")
                os.unlink(tmp)
            os.symlink(target, tmp)
            os.rename(tmp, lib)
            _fsync_dir(os.path.dirname(lib) or ".")
        _record(db, id, 4, "offloading", after_step)
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
        _record(db, id, 5, "offloading", after_step)
        step = 5

    # ---- step 6: punch holes, restore mtime, verify head and tail
    if step < 6:
        _punch_and_verify(db, id, cur, stub)
        _record(db, id, 6, "offloading", after_step)
        step = 6

    # ---- step 7
    st.set_state(db, id, "offloaded", step=7, last_ok_ts=time.time())
    st.log_event(db, id, "offloaded", {"stub": stub})
    if after_step:
        after_step(7)
    return Result(True, "offloaded", 7)


def demote(db, row: dict, **kw) -> Result:
    """Offload a file that already has a verified sha256 and an NZB."""
    return offload(db, row, _demote=True, **kw)


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


def _punch_and_verify(db, id, cur, stub):
    size, head_len, tail_len = cur["size"], cur["head_len"], cur["tail_len"]
    fd = os.open(stub, os.O_RDWR)
    try:
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
) -> Result:
    """Fill the stub's middle from the remote, verify sha256, swap it over L.
    On a hash mismatch nothing is swapped: state=failed, the symlink stays."""
    row = st.get(db, id)
    if row is None:
        return Result(False, None, 0, "unknown id")
    state, step = row["state"], int(row.get("step") or 0)
    if state == "whole":
        return Result(True, state, step, "already whole", dry_run=not apply)
    if state not in ("offloaded", "promoting", "failed"):
        return Result(False, state, step, f"state {state!r} cannot be promoted")
    manifest_dir = manifest_dir or os.environ.get("TIERFS_MANIFESTS")
    if not manifest_dir:
        return Result(False, state, step, "manifest_dir not given and TIERFS_MANIFESTS unset")
    lib, stub = row["lib_path"], row["stub_path"]
    size, head_len, tail_len = row["size"], row["head_len"], row["tail_len"]
    mtime_ns = row["mtime_ns"]
    if not (lib and stub and row.get("sha256") and size and head_len and tail_len):
        return Result(False, state, step, "row is missing facts needed to promote")
    ext = os.path.splitext(lib)[1]
    tmp = lib + TMP_SUFFIX
    mpath = manifest_path(manifest_dir, id)

    if not _swapped(lib, stub, step if state == "promoting" else 0):
        if not os.path.isfile(stub) or os.path.islink(stub):
            return Result(False, state, step, "stub is missing")
        if not (os.path.islink(lib) and os.path.basename(os.readlink(lib)) == f"{id}{ext}"):
            return Result(False, state, step, "L is not the tierfs symlink for this id")
    if not apply:
        log.info("dry run: would promote %s: fill [%d, %d) from remote, verify sha256, swap %s",
                 id, head_len, size - tail_len, lib)
        return Result(True, state, step, "dry run", dry_run=True)

    if state != "promoting":
        step = 0
        st.set_state(db, id, "promoting", step=0)
        st.log_event(db, id, "promote_start", {"from": state})

    # ---- step 1: fill the middle (only that range is written), fsync
    if step < 1:
        try:
            _fill_middle(reader, row, stub, chunk)
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

    # ---- step 4: link stub to L.tierfs-tmp, rename over L
    if step < 4:
        if not _swapped(lib, stub, 0):
            if not (os.path.islink(lib) and os.path.basename(os.readlink(lib)) == f"{id}{ext}"):
                _fail(db, id, "L is not the tierfs symlink at swap time")
                return Result(False, "failed", step, "L changed before the swap")
            if os.path.lexists(tmp):
                if not os.path.islink(tmp) and not _same_inode(os.lstat(tmp), os.lstat(stub)):
                    return Result(False, "promoting", step, f"{tmp} exists and is not ours")
                os.unlink(tmp)
            os.link(stub, tmp)
            os.rename(tmp, lib)
            _fsync_dir(os.path.dirname(lib) or ".")
        _record(db, id, 4, "promoting", after_step)
        step = 4

    # ---- step 5: drop the stub's extra link and the manifest
    ls = os.lstat(lib)
    if not stat_mod.S_ISREG(ls.st_mode):
        raise OpsError("L is not a regular file after the swap")
    if os.path.lexists(stub):
        if not _same_inode(os.lstat(stub), ls):
            raise OpsError("stub is not the same inode as L; refusing to unlink it")
        os.unlink(stub)
    if os.path.lexists(mpath):
        os.unlink(mpath)
    _fsync_dir(os.path.dirname(lib) or ".")
    st.set_state(db, id, "whole", step=0, stub_path=None, last_ok_ts=time.time())
    st.log_event(db, id, "promoted", {})
    if after_step:
        after_step(5)
    return Result(True, "whole", 5)


def _swapped(lib, stub, step) -> bool:
    """L is a regular file that is the stub's inode (swap done). Once the swap
    step is recorded, a regular L with the stub already unlinked also counts."""
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


def _fill_middle(reader, row, stub, chunk):
    start, end = row["head_len"], row["size"] - row["tail_len"]
    fd = os.open(stub, os.O_WRONLY)
    try:
        off = start
        while off < end:
            n = min(chunk, end - off)
            data = reader.read_range(row["remote_path"], off, n)
            if len(data) != n:
                raise OpsError(f"short read at {off}: wanted {n}, got {len(data)}")
            view = memoryview(data)
            w = 0
            while w < n:
                w += os.pwrite(fd, view[w:], off + w)
            off += n
        os.fsync(fd)
    finally:
        os.close(fd)
