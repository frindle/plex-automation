"""Restub: rebuild an offloaded file's local stub (and its library symlink) from the
remote, reading only the head and the tail.

After a data loss the stub (and maybe L, which lives on the same disk) is gone,
but the manifest and chunk list and the database survive. Everything the stub
holds is [0, head_len) and [size - tail_len, size), and the manifest holds the
hashes of both, so the stub can be rebuilt from two ranged reads of the remote
instead of downloading the whole release again. See "Restub" in
tiering/OFFLOAD_DESIGN.md.

  python -m offload.restub                 dry run: list what is wrong
  python -m offload.restub --apply         rebuild (needs TIERING_APPLY=1)
  python -m offload.restub --cap N         rebuild at most N files per run
  python -m offload.runner restub ...      the same command through the runner

Settings come from the same environment as the runner: TIERING_DB, TIERFS_MANIFESTS,
TIERFS_MOUNT, TIERFS_STUB_ROOTS, TIERING_PATH_MAP, TIERING_ALERTS, plus the AltMount
reader's TIERFS_REMOTE_BASE / _USER / _PASS. TIERING_RESTUB_CAP (default 10) and
TIERING_RESTUB_RETRY_S (default 60, per piece) tune the run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat as stat_mod
import sys
import time

import blake3

from . import ops, paths
from . import state as st

log = logging.getLogger("tiering.restub")

DEFAULT_CAP = 10
DEFAULT_RETRY_S = 60.0
ATTEMPTS = 2  # fetches of a range per copy before it counts as unreadable
TMP_SUFFIX = ".restub-tmp"


class _Down(Exception):
    """The remote cannot be reached (or refuses our login): stop the whole run."""


class _Unreadable(Exception):
    """The remote answered but the bytes are not there (gone articles, short, wrong)."""


def _is_down(e: BaseException) -> bool:
    if isinstance(e, _Down):
        return True
    try:
        from . import altmount
    except ImportError:  # pragma: no cover
        altmount = None
    if altmount is not None and isinstance(e, altmount.AltMountError):
        if isinstance(e, altmount.AltMountAuthError):
            return True
        return str(e).startswith(("request failed", "read failed"))
    return isinstance(e, OSError)  # ConnectionError, TimeoutError, ...


# --------------------------------------------------------------------------
# inspection (local only, never writes)


def _resolve(share, path_map, stub_root):
    """(disk path of L, "") or ("", refusal). Like ops, exactly one disk path. L may be
    gone: then the folder it lived in counts, and when several disks have that folder
    the one on the stub's disk is taken."""
    try:
        pairs = ops._path_pairs(path_map)
    except ValueError as e:
        return "", f"bad TIERING_PATH_MAP: {e}"
    if not pairs:
        return str(share), ""
    cands = paths.disk_paths(share, pairs)
    if len(cands) == 1:
        return cands[0], ""
    if len(cands) > 1:
        return "", f"{share}: maps to {len(cands)} disk paths; refusing"
    folders = paths.disk_paths(
        share, pairs, exists=lambda p: os.path.isdir(os.path.dirname(p)))
    try:
        want = os.stat(stub_root).st_dev
        folders = [p for p in folders if os.stat(os.path.dirname(p)).st_dev == want]
    except OSError as e:
        return "", f"cannot stat the stub root: {e}"
    if len(folders) != 1:
        return "", (f"{share}: library file is gone and {len(folders)} disk folders on the "
                    "stub's disk could hold it; refusing")
    return folders[0], ""


def _load_manifest(manifest_dir, row):
    """(manifest, "") or (None, reason). The chunk list must exist and match."""
    id = row["id"]
    try:
        with open(ops.manifest_path(manifest_dir, id)) as f:
            man = json.load(f)
        with open(ops.chunks_path(manifest_dir, id), "rb") as f:
            blob = f.read()
    except (OSError, ValueError) as e:
        return None, f"manifest or chunk list missing or unreadable: {type(e).__name__}"
    try:
        size, head, tail = int(man["size"]), int(man["head_len"]), int(man["tail_len"])
        chunk_size = int(man.get("chunk_size") or 0)
    except (KeyError, TypeError, ValueError):
        return None, "manifest is damaged"
    mid = size - head - tail
    if (chunk_size <= 0 or mid <= 0 or man.get("id") != id
            or blake3.blake3(blob).hexdigest() != man.get("chunks_b3")
            or len(blob) != 32 * -(-mid // chunk_size)):
        return None, "chunk list does not match the manifest"
    for k in ("head_b3", "tail_b3", "remote_path", "stub_path", "mtime_ns"):
        if not man.get(k):
            return None, f"manifest has no {k}"
    for k in ("size", "head_len", "tail_len", "head_b3", "tail_b3"):
        if row.get(k) is not None and row[k] != man[k]:
            return None, f"manifest {k} differs from the database row"
    return man, ""


def _stub_problem(stub, man) -> str:
    """"" when the stub is fine, a short cause when it must be rebuilt, or "!<reason>"
    when something unexpected sits at its path (never replaced)."""
    try:
        ls = os.lstat(stub)
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return f"!cannot stat the stub: {e}"
    if not stat_mod.S_ISREG(ls.st_mode):
        return "!the stub path is not a regular file"
    if ls.st_size != man["size"]:
        return f"wrong size {ls.st_size}"
    try:
        if ops._b3_range(stub, 0, man["head_len"]) != man["head_b3"]:
            return "head does not match"
        if ops._b3_range(stub, man["size"] - man["tail_len"], man["tail_len"]) != man["tail_b3"]:
            return "tail does not match"
    except (OSError, ops.OpsError) as e:
        return f"unreadable: {type(e).__name__}"
    return ""


def _l_problem(lib, id, ext) -> str:
    """"" when L is the tierfs symlink for this id, "missing", or "!<reason>"."""
    try:
        ls = os.lstat(lib)
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(lib) or "."):
            return "!the library folder is gone"
        return "missing"
    except OSError as e:
        return f"!cannot stat L: {e}"
    if stat_mod.S_ISLNK(ls.st_mode):
        if os.path.basename(os.readlink(lib)) == f"{id}{ext}":
            return ""
        return "!L is a symlink to something else"
    return "!L is not a symlink (a regular file sits there); not touched"


def inspect(row, *, manifest_dir, tierfs_mount, stub_roots, path_map) -> dict:
    """Classify one offloaded row without touching anything. Keys: `kind` is one of
    ok, rebuild, no_manifest, refused; plus the facts the rebuild needs."""
    id = row["id"]
    out = {"id": id, "kind": "refused", "reason": ""}
    share, stub = row.get("lib_path"), row.get("stub_path")
    if not share or not stub:
        out["reason"] = "row has no lib_path or stub_path"
        return out
    man, why = _load_manifest(manifest_dir, row)
    if man is None:
        out.update(kind="no_manifest", reason=why)
        return out
    ext = os.path.splitext(share)[1]
    stub_root = os.path.dirname(stub)
    roots = [os.path.normpath(r) for r in stub_roots]
    if man["stub_path"] != stub or os.path.basename(stub) != f"{id}{ext}":
        out["reason"] = "manifest stub_path differs from the row"
        return out
    if roots and os.path.normpath(stub_root) not in roots:
        out["reason"] = "stub path is not under a TIERFS_STUB_ROOTS entry"
        return out
    if not os.path.isdir(stub_root):
        out["reason"] = "stub root is not present (disk not mounted?); not creating it"
        return out
    lib, why = _resolve(share, path_map, stub_root)
    if why:
        out["reason"] = why
        return out
    try:
        if os.stat(os.path.dirname(lib)).st_dev != os.stat(stub_root).st_dev:
            out["reason"] = "stub root and the library folder are on different disks"
            return out
        ops._require_real_fs(stub_root, os.path.dirname(lib))
    except (OSError, ops.OpsError) as e:
        out["reason"] = f"stub root is not usable: {e}"
        return out
    sp, lp = _stub_problem(stub, man), _l_problem(lib, id, ext)
    for p in (sp, lp):
        if p.startswith("!"):
            out["reason"] = p[1:]
            return out
    sources = [man["remote_path"]]
    if row.get("twin_remote_path") and row["twin_remote_path"] not in sources:
        sources.append(row["twin_remote_path"])
    out.update(kind="rebuild" if (sp or lp) else "ok", stub_problem=sp, l_problem=lp,
               man=man, lib=lib, stub=stub, ext=ext, sources=sources,
               target=os.path.join(str(tierfs_mount), f"{id}{ext}"))
    return out


# --------------------------------------------------------------------------
# fetching


def _retry_s() -> float:
    return ops._env_s("TIERING_RESTUB_RETRY_S", DEFAULT_RETRY_S)


def _piece(reader, remote, off, n) -> bytes:
    """One ranged read, retried with backoff within the budget. _Down for a transport
    trouble, _Unreadable for anything else (short answer, status error)."""
    deadline = time.monotonic() + _retry_s()
    delay = 0.5
    while True:
        try:
            b = reader.read_range(remote, off, n)
            if len(b) == n:
                return b
            err: BaseException = _Unreadable(f"short read at {off} ({len(b)} of {n} bytes)")
        except Exception as e:  # noqa: BLE001 (classified below)
            err = _Down(str(e)) if _is_down(e) else _Unreadable(f"{type(e).__name__} at {off}")
        if time.monotonic() >= deadline:
            raise err
        ops._sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 10.0)


def _fetch_range(reader, remote, start, length, expect_b3, fd) -> None:
    """Read [start, start+length) of the remote into fd at the same offset and check its
    BLAKE3. Raises _Down or _Unreadable. Written bytes of a failed range are not trusted
    (the caller drops the temp file)."""
    last = None
    for _ in range(ATTEMPTS):
        h = blake3.blake3()
        off, end = start, start + length
        while off < end:
            n = min(ops.CHUNK, end - off)
            b = _piece(reader, remote, off, n)
            os.pwrite(fd, b, off)
            h.update(b)
            off += n
        if h.hexdigest() == expect_b3:
            return
        last = _Unreadable(f"bytes at [{start}, {start + length}) do not match the hash "
                           "(the articles are gone or damaged)")
    raise last


def _build_stub(reader, plan) -> None:
    """Sparse temp file, head and tail from the first copy that verifies, mtime, fsync,
    atomic rename onto stub_path. Raises _Down / _Unreadable; the temp is removed then."""
    man, stub = plan["man"], plan["stub"]
    size, head, tail = man["size"], man["head_len"], man["tail_len"]
    tmp = stub + TMP_SUFFIX
    ops._rm(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.ftruncate(fd, size)
        errs = []
        for remote in plan["sources"]:
            try:
                _fetch_range(reader, remote, size - tail, tail, man["tail_b3"], fd)
                _fetch_range(reader, remote, 0, head, man["head_b3"], fd)
                break
            except _Unreadable as e:
                errs.append(str(e))
        else:
            raise _Unreadable("; ".join(errs))
        os.fsync(fd)
        os.utime(fd, ns=(man["mtime_ns"], man["mtime_ns"]))
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        ops._rm(tmp)
        raise
    os.close(fd)
    try:
        os.rename(tmp, stub)
    except BaseException:
        ops._rm(tmp)
        raise
    ops._fsync_dir(os.path.dirname(stub))


def _restore_l(plan) -> None:
    """Symlink L to the tierfs entry. os.symlink fails if anything appeared at L since
    the check, so nothing is ever replaced."""
    lib = plan["lib"]
    os.symlink(plan["target"], lib)
    ops._fsync_dir(os.path.dirname(lib))


# --------------------------------------------------------------------------
# the run


def _alert(path, kind, id, **detail):
    rec = {"ts": time.time(), "kind": kind, "id": id, **detail}
    log.error("ALERT %s id=%s %s", kind, id, json.dumps(detail, sort_keys=True, default=str))
    if not path:
        return
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except OSError as e:
        log.error("cannot write alerts file: %s", type(e).__name__)


def restub(db, reader, *, apply=False, cap=None, manifest_dir=None, tierfs_mount=None,
           stub_roots=None, path_map=None, alerts=None, probe=None) -> dict:
    """Find offloaded rows with a missing or damaged stub (or a missing L) and, with
    `apply`, rebuild them. Returns a summary dict:
      ok, rebuilt (applied) / would_rebuild (dry run), deferred (over the cap),
      no_manifest, refused (id -> reason), failed (id -> reason), error (run stopped).
    `probe(remote_path)` checks that the remote answers; the default is a one-byte read."""
    manifest_dir = manifest_dir or os.environ.get("TIERFS_MANIFESTS")
    tierfs_mount = tierfs_mount or os.environ.get("TIERFS_MOUNT")
    if stub_roots is None:
        stub_roots = os.environ.get("TIERFS_STUB_ROOTS", "")
    if isinstance(stub_roots, str):
        stub_roots = [r.strip() for r in stub_roots.split(",") if r.strip()]
    if cap is None:
        try:
            cap = int(os.environ.get("TIERING_RESTUB_CAP", DEFAULT_CAP))
        except ValueError:
            cap = DEFAULT_CAP
    s = {"ok": [], "rebuilt": [], "would_rebuild": [], "deferred": [], "no_manifest": {},
         "refused": {}, "failed": {}, "error": "", "dry_run": not apply}
    if not manifest_dir or not tierfs_mount:
        s["error"] = "TIERFS_MANIFESTS and TIERFS_MOUNT are needed"
        return s
    if apply and os.environ.get("TIERING_APPLY") != "1":
        s["error"] = "TIERING_APPLY=1 is not set; refusing to apply"
        return s
    if apply and not stub_roots:
        s["error"] = "TIERFS_STUB_ROOTS is not set; refusing to apply"
        return s
    if path_map is None:
        path_map = os.environ.get("TIERING_PATH_MAP", "")
    if alerts is None:
        alerts = os.environ.get("TIERING_ALERTS") or None
        if alerts is None:
            p = db.execute("PRAGMA database_list").fetchone()
            if p and p[2]:
                alerts = os.path.join(os.path.dirname(os.path.abspath(p[2])), "alerts.jsonl")

    plans = []
    for row in st.list_files(db, "offloaded"):
        p = inspect(row, manifest_dir=manifest_dir, tierfs_mount=tierfs_mount,
                    stub_roots=stub_roots, path_map=path_map)
        if p["kind"] == "ok":
            s["ok"].append(p["id"])
        elif p["kind"] == "no_manifest":
            s["no_manifest"][p["id"]] = p["reason"]
            log.warning("%s: %s; cannot rebuild without it", p["id"], p["reason"])
        elif p["kind"] == "refused":
            s["refused"][p["id"]] = p["reason"]
            log.warning("%s: refused: %s", p["id"], p["reason"])
        else:
            plans.append((row, p))
    for _row, p in plans:
        log.info("%s: %s", p["id"], "; ".join(
            x for x in (f"stub {p['stub_problem']}" if p["stub_problem"] else "",
                        f"L {p['l_problem']}" if p["l_problem"] else "") if x))

    todo, s["deferred"] = plans[:max(0, cap)], [p["id"] for _r, p in plans[max(0, cap):]]
    if not apply:
        s["would_rebuild"] = [p["id"] for _r, p in todo]
        return s

    # the remote must answer before anything is written (only if a stub needs bytes)
    first = next((p for _r, p in todo if p["stub_problem"]), None)
    if first is not None:
        try:
            (probe or (lambda rp: reader.read_range(rp, 0, 1)))(first["sources"][0])
        except Exception as e:  # noqa: BLE001
            if _is_down(e):
                s["error"] = f"the remote is down ({type(e).__name__}); nothing written"
                return s
            # a missing file is that row's own trouble, found below

    for row, p in todo:
        id = row["id"]
        try:
            with ops._id_lock(db, id) as held:
                if not held:
                    s["refused"][id] = "busy (another operation holds this file)"
                    continue
                cur = st.get(db, id)
                if cur is None or cur["state"] != "offloaded":
                    s["refused"][id] = "row is no longer offloaded"
                    continue
                if p["stub_problem"]:
                    try:
                        _build_stub(reader, p)
                    except _Down as e:
                        s["error"] = f"the remote went away ({e}); stopped, rerun to resume"
                        return s
                    except _Unreadable as e:
                        reason = ("restub: head or tail cannot be read from the remote: "
                                  f"{e}")
                        ops._fail(db, id, reason[:500])
                        _alert(alerts, "restub_unreadable", id, reason=str(e),
                               lib_path=row.get("lib_path"))
                        s["failed"][id] = str(e)
                        continue
                if p["l_problem"]:
                    _restore_l(p)
                st.log_event(db, id, "restubbed", {
                    "stub": p["stub_problem"] or "ok", "l": p["l_problem"] or "ok"})
                s["rebuilt"].append(id)
        except (OSError, ops.OpsError) as e:
            s["refused"][id] = f"{type(e).__name__}: {e}"
            log.error("%s: restub stopped: %s", id, e)
    return s


# --------------------------------------------------------------------------
# command line


def _report(s) -> None:
    for k in ("ok", "rebuilt", "would_rebuild", "deferred"):
        print(f"{k}: {len(s[k])}")
        if k != "ok":
            for i in s[k]:
                print(f"  {i}")
    for k in ("no_manifest", "refused", "failed"):
        print(f"{k}: {len(s[k])}")
        for i, why in s[k].items():
            print(f"  {i}: {why}")
    if s["error"]:
        print(f"error: {s['error']}")
    print("dry run: nothing written (use --apply)" if s["dry_run"] else "applied")


def main(argv=None, env=None, reader=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "restub":
        argv = argv[1:]
    apply, cap = "--apply" in argv, None
    if "--cap" in argv:
        try:
            cap = int(argv[argv.index("--cap") + 1])
        except (IndexError, ValueError):
            print("--cap needs a number", file=sys.stderr)
            return 2
    if env is not None:
        os.environ.update(env)
    db_path = os.environ.get("TIERING_DB")
    if not db_path:
        print("TIERING_DB is not set", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if reader is None:
        from . import altmount
        try:
            reader = altmount.WebDavReader()
        except altmount.AltMountError as e:
            print(f"remote reader: {e}", file=sys.stderr)
            return 2
    db = st.open_db(db_path)
    try:
        s = restub(db, reader, apply=apply, cap=cap)
    finally:
        with contextlib.suppress(Exception):
            db.close()
    _report(s)
    return 1 if (s["error"] or s["failed"] or s["refused"]) else 0


if __name__ == "__main__":
    sys.exit(main())
