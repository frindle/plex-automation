import hashlib
import json
import os
import stat
import sys
import tempfile

import pytest

from tiering.offload import ops
from tiering.offload import state as st

MIB = 1024 * 1024
ID = "0123456789abcdef"


def _punch_supported():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "probe")
    try:
        with open(p, "wb") as f:
            f.write(b"x" * (4 * MIB))
        fd = os.open(p, os.O_RDWR)
        try:
            ops.punch_hole(fd, MIB, MIB)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(p)
            os.rmdir(d)
        except OSError:
            pass


pytestmark = pytest.mark.skipif(
    not _punch_supported(), reason="filesystem does not support FALLOC_FL_PUNCH_HOLE"
)

MTIME_NS = 1711393377123456789


class Crash(Exception):
    pass


class FakeReader:
    def __init__(self, data, corrupt_at=None, fail_times=0):
        self.data, self.corrupt_at, self.fail_times = data, corrupt_at, fail_times
        self.calls = []

    def read_range(self, remote_path, offset, length):
        self.calls.append((offset, length))
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("remote down")
        b = bytearray(self.data[offset:offset + length])
        if self.corrupt_at is not None and offset <= self.corrupt_at < offset + length:
            b[self.corrupt_at - offset] ^= 0xFF
        return bytes(b)


class Env:
    def __init__(self, root, size=50 * MIB):
        self.root = root
        for d in ("lib", "stubs", "manifests", "mount"):
            os.mkdir(root / d)
        self.lib = str(root / "lib" / "Movie (2020).mkv")
        self.data = os.urandom(size)
        with open(self.lib, "wb") as f:
            f.write(self.data)
        os.utime(self.lib, ns=(MTIME_NS, MTIME_NS))
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.stub = str(root / "stubs" / f"{ID}.mkv")
        self.entry = str(root / "mount" / f"{ID}.mkv")
        self.manifest = str(root / "manifests" / f"{ID}.json")
        self.db = st.open_db(root / "state.db")
        self.row = {"id": ID, "lib_path": self.lib, "remote_path": "/r/Movie.mkv",
                    "rating_key": "1", "kind": "movie"}
        st.upsert(self.db, self.row)

    def kw(self, **extra):
        kw = dict(stub_root=str(self.root / "stubs"), manifest_dir=str(self.root / "manifests"),
                  tierfs_mount=str(self.root / "mount"), apply=True,
                  open_check=lambda p: [], open_poll=0.01, mount_wait=1.0)
        kw.update(extra)
        return kw

    def fake_tierfs(self):
        """Stand-in for tierfs: once a manifest exists, expose <id><ext>."""
        if os.path.exists(self.manifest) and not os.path.lexists(self.entry):
            os.symlink(self.stub, self.entry)

    def hook(self, crash_at=None, expect_L=True):
        self.boundaries = getattr(self, "boundaries", [])

        def h(step):
            self.fake_tierfs()
            if expect_L:
                # L must resolve (not dangle, not missing) at every boundary
                assert os.path.exists(self.lib), f"L missing after step {step}"
                self.boundaries.append(step)
            if step == crash_at:
                raise Crash(step)
        return h

    def offload(self, **extra):
        extra.setdefault("after_step", self.hook())
        return ops.offload(self.db, self.row, **self.kw(**extra))

    def promote(self, reader=None, **extra):
        extra.setdefault("after_step", self.hook())
        reader = reader or FakeReader(self.data)
        return ops.promote(self.db, ID, reader, apply=True,
                           manifest_dir=str(self.root / "manifests"), **extra)

    def read_lib(self):
        with open(self.lib, "rb") as f:
            return f.read()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


def tree(root):
    out = {}
    for dp, dns, fns in os.walk(root, followlinks=False):
        for n in dns + fns:
            if n.startswith("state.db"):
                continue
            p = os.path.join(dp, n)
            s = os.lstat(p)
            out[p] = (s.st_mode, s.st_size, s.st_mtime_ns, s.st_ino, s.st_nlink,
                      os.readlink(p) if stat.S_ISLNK(s.st_mode) else None)
    return out


def row_hl_tl(env):
    r = st.get(env.db, ID)
    return r["head_len"] + r["tail_len"]


def assert_offloaded(env):
    s = os.lstat(env.lib)
    assert stat.S_ISLNK(s.st_mode)
    assert os.readlink(env.lib) == env.entry
    assert env.read_lib()[:4] == env.data[:4]
    ss = os.stat(env.stub)
    assert ss.st_size == len(env.data) and ss.st_mtime_ns == MTIME_NS
    assert ss.st_blocks * 512 <= row_hl_tl(env) + MIB  # middle is a hole
    row = st.get(env.db, ID)
    hl, tl = row["head_len"], row["tail_len"]
    with open(env.stub, "rb") as f:
        assert f.read(hl) == env.data[:hl]
        f.seek(len(env.data) - tl)
        assert f.read() == env.data[-tl:]


def assert_whole(env):
    s = os.lstat(env.lib)
    assert stat.S_ISREG(s.st_mode) and s.st_nlink == 1
    assert s.st_size == len(env.data) and s.st_mtime_ns == MTIME_NS
    assert env.read_lib() == env.data
    assert not os.path.lexists(env.stub)
    assert not os.path.lexists(env.manifest)
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    r = st.get(env.db, ID)
    assert r["state"] == "whole" and r["step"] == 0


# ---------------------------------------------------------------- layout


def test_plan_layout():
    lay = ops.plan_layout("x", 50 * MIB)
    assert lay.head_len == 10 * MIB and lay.tail_len == 32 * MIB
    lay = ops.plan_layout("x", 8_370_000_000)
    assert lay.head_len % MIB == 0 and lay.head_len >= 0.2 * 8_370_000_000
    assert lay.head_len - 0.2 * 8_370_000_000 < MIB
    head, tail = ops.plan_layout("x", 1000 * MIB)
    assert (head, tail) == (200 * MIB, 32 * MIB)


@pytest.mark.parametrize("size", [0, 1, 40 * MIB, 41 * MIB])
def test_plan_layout_refuses_small(size):
    with pytest.raises(ops.LayoutError):
        ops.plan_layout("x", size)


def test_new_id_format(env):
    i = ops.new_id(env.db)
    assert len(i) == 16 and i == i.lower() and int(i, 16) >= 0


def test_write_manifest_atomic(tmp_path):
    ops.write_manifest(tmp_path, {"id": "a" * 16, "v": 1})
    assert json.load(open(tmp_path / ("a" * 16 + ".json"))) == {"id": "a" * 16, "v": 1}
    assert os.listdir(tmp_path) == ["a" * 16 + ".json"]


# ---------------------------------------------------------------- offload


def test_offload_result_matches_contract(env):
    r = env.offload()
    assert r.ok and r.state == "offloaded"
    assert_offloaded(env)
    assert env.boundaries == [1, 2, 3, 4, 5, 6, 7]
    m = json.load(open(env.manifest))
    assert set(m) == {"v", "id", "ext", "size", "mtime_ns", "head_len", "tail_len", "stub_path",
                      "remote_path", "sha256", "head_b3", "tail_b3"}
    assert m["v"] == 1 and m["id"] == ID and m["ext"] == ".mkv"
    assert m["size"] == len(env.data) and m["mtime_ns"] == MTIME_NS
    assert m["head_len"] == 10 * MIB and m["tail_len"] == 32 * MIB
    assert m["stub_path"] == env.stub and m["remote_path"] == "/r/Movie.mkv"
    assert m["sha256"] == env.sha
    import blake3
    assert m["head_b3"] == blake3.blake3(env.data[:m["head_len"]]).hexdigest()
    assert m["tail_b3"] == blake3.blake3(env.data[-m["tail_len"]:]).hexdigest()
    row = st.get(env.db, ID)
    assert row["state"] == "offloaded" and row["step"] == 7 and row["stub_path"] == env.stub


def test_offload_again_is_noop(env):
    env.offload()
    t = tree(env.root)
    r = env.offload()
    assert r.ok and tree(env.root) == t


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4, 5, 6, 7])
def test_offload_crash_and_resume(env, crash_at):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(crash_at))
    assert os.path.exists(env.lib)  # L resolves right after the crash
    row = st.get(env.db, ID)
    assert row["step"] == crash_at
    assert row["state"] == ("offloaded" if crash_at == 7 else "offloading")
    r = env.offload()
    assert r.ok and r.state == "offloaded"
    assert os.path.exists(env.lib)
    assert_offloaded(env)


@pytest.mark.parametrize("crash_at", [2, 3, 4, 5, 6])
def test_offload_redo_of_unrecorded_step_is_idempotent(env, crash_at):
    """Crash after doing a step but before recording it: the step runs twice."""
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(crash_at))
    st.set_state(env.db, ID, "offloading", step=crash_at - 1)
    assert env.offload().ok
    assert_offloaded(env)


def test_offload_open_handles_blocks_punching(env):
    r = env.offload(open_check=lambda p: [1234], open_timeout=0.05)
    assert not r.ok and "open" in r.reason
    row = st.get(env.db, ID)
    assert row["state"] == "offloading" and row["step"] == 4
    assert os.path.exists(env.lib)
    assert os.stat(env.stub).st_blocks * 512 >= len(env.data)  # not punched
    # holder goes away, same call resumes
    assert env.offload().ok
    assert_offloaded(env)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_default_open_check_sees_open_fd(env, tmp_path):
    import subprocess
    p = subprocess.Popen([sys.executable, "-c",
                          "import sys,time; f=open(sys.argv[1],'rb'); print('r',flush=True); time.sleep(30)",
                          env.lib], stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b"r"
        assert p.pid in ops.scan_open_holders(env.lib)
        kw = env.kw()
        del kw["open_check"]
        r = ops.offload(env.db, env.row, **kw, open_timeout=0.3, after_step=env.hook())
        assert not r.ok and "open" in r.reason
        assert st.get(env.db, ID)["state"] == "offloading"
    finally:
        p.kill()
        p.wait()
    kw = env.kw()
    del kw["open_check"]
    assert ops.offload(env.db, env.row, **kw, after_step=env.hook()).ok


def test_offload_waits_for_tierfs_entry(env):
    r = ops.offload(env.db, env.row, **env.kw(mount_wait=0.05))  # no fake tierfs
    assert not r.ok and "tierfs" in r.reason
    assert not os.path.islink(env.lib)  # L untouched
    assert st.get(env.db, ID)["step"] == 3


def test_offload_refuses_modified_file(env):
    def h(step):
        env.fake_tierfs()
        if step == 3:
            with open(env.lib, "r+b") as f:  # writer touches L through the shared inode
                f.write(b"Z")
    r = env.offload(after_step=h)
    assert not r.ok
    assert st.get(env.db, ID)["state"] == "failed"
    assert not os.path.islink(env.lib)


# ---------------------------------------------------------------- refusals


def _refused(env, reason_part, **extra):
    before = tree(env.root)
    r = ops.offload(env.db, env.row, **env.kw(**extra))
    assert not r.ok and reason_part in r.reason
    assert tree(env.root) == before  # nothing created or touched
    assert st.get(env.db, ID)["state"] == "whole"


def test_refuse_nlink(env):
    os.link(env.lib, str(env.root / "lib" / "other.mkv"))
    _refused(env, "hardlinks")


def test_refuse_other_filesystem(env, monkeypatch):
    real = ops._dev
    monkeypatch.setattr(ops, "_dev", lambda p: real(p) + 1 if str(p) == str(env.root / "stubs") else real(p))
    _refused(env, "different filesystem")


def test_refuse_too_small(env, tmp_path):
    small = str(env.root / "lib" / "small.mkv")
    with open(small, "wb") as f:
        f.write(os.urandom(40 * MIB))
    st.upsert(env.db, {"id": "f" * 16, "lib_path": small, "remote_path": "/r/s"})
    before = tree(env.root)
    r = ops.offload(env.db, {"id": "f" * 16, "lib_path": small, "remote_path": "/r/s"}, **env.kw())
    assert not r.ok and "too small" in r.reason
    assert tree(env.root) == before


def test_refuse_symlink(env):
    real = str(env.root / "lib" / "real.mkv")
    os.rename(env.lib, real)
    os.symlink(real, env.lib)
    _refused(env, "symlink")


def test_refuse_missing_remote_path(env):
    env.row = {**env.row, "remote_path": None}
    st.upsert(env.db, {"id": ID, "remote_path": None})
    _refused(env, "remote_path")


def test_refuse_recorded_hash_mismatch(env):
    st.upsert(env.db, {"id": ID, "head_b3": "00" * 32})
    _refused(env, "changed")


# ---------------------------------------------------------------- dry run


def test_dry_run_changes_nothing(env):
    before = tree(env.root)
    ev = st.events(env.db)
    row = st.get(env.db, ID)
    r = ops.offload(env.db, env.row, **env.kw(apply=False))
    assert r.ok and r.dry_run
    assert tree(env.root) == before
    assert st.get(env.db, ID) == row and st.events(env.db) == ev


def test_dry_run_promote_and_resume_change_nothing(env):
    env.offload()
    before = tree(env.root)
    row = st.get(env.db, ID)
    reader = FakeReader(env.data)
    r = ops.promote(env.db, ID, reader, apply=False, manifest_dir=str(env.root / "manifests"))
    assert r.ok and r.dry_run and reader.calls == []
    assert tree(env.root) == before and st.get(env.db, ID) == row
    # dry run of a half-finished offload too
    st.set_state(env.db, ID, "offloading", step=3)
    row = st.get(env.db, ID)
    r = ops.offload(env.db, env.row, **env.kw(apply=False))
    assert r.ok and r.dry_run and tree(env.root) == before and st.get(env.db, ID) == row


# ---------------------------------------------------------------- promote


def test_round_trip(env):
    assert env.offload().ok
    assert_offloaded(env)
    r = env.promote()
    assert r.ok and r.state == "whole"
    assert_whole(env)
    assert env.boundaries[-5:] == [1, 2, 3, 4, 5]


def test_promote_writes_only_the_middle(env):
    env.offload()
    reader = FakeReader(env.data)
    env.promote(reader, chunk=4 * MIB)
    hl, tl = 10 * MIB, 32 * MIB
    assert reader.calls[0][0] == hl
    assert all(o >= hl and o + n <= len(env.data) - tl for o, n in reader.calls)
    assert all(n <= 4 * MIB for _, n in reader.calls)


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4, 5])
def test_promote_crash_and_resume(env, crash_at):
    env.offload()
    with pytest.raises(Crash):
        env.promote(after_step=env.hook(crash_at))
    assert os.path.exists(env.lib)
    row = st.get(env.db, ID)
    assert row["step"] == (0 if crash_at == 5 else crash_at)
    assert row["state"] == ("whole" if crash_at == 5 else "promoting")
    r = env.promote()
    assert r.ok
    assert os.path.exists(env.lib)
    assert_whole(env)


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4])
def test_promote_redo_of_unrecorded_step_is_idempotent(env, crash_at):
    env.offload()
    with pytest.raises(Crash):
        env.promote(after_step=env.hook(crash_at))
    st.set_state(env.db, ID, "promoting", step=crash_at - 1)
    assert env.promote().ok
    assert_whole(env)


def test_promote_crash_after_unlink_before_state(env):
    """Stub and manifest already gone but state not yet updated."""
    env.offload()
    with pytest.raises(Crash):
        env.promote(after_step=env.hook(4))
    os.unlink(env.stub)
    os.unlink(env.manifest)
    assert env.promote().ok
    assert_whole(env)


def test_promote_corrupt_byte_fails_without_swap(env):
    env.offload()
    link_before = os.readlink(env.lib)
    ino_before = os.lstat(env.lib).st_ino
    reader = FakeReader(env.data, corrupt_at=12 * MIB + 5)
    r = env.promote(reader)
    assert not r.ok and r.state == "failed"
    row = st.get(env.db, ID)
    assert row["state"] == "failed"
    assert os.path.islink(env.lib) and os.readlink(env.lib) == link_before
    assert os.lstat(env.lib).st_ino == ino_before
    assert os.path.exists(env.stub) and os.path.exists(env.manifest)
    assert os.stat(env.stub).st_nlink == 1  # never linked into L
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    ev = st.events(env.db, ID)
    assert ev[-1]["kind"] == "failed" and "sha256" in ev[-1]["detail"]["reason"]
    # failed is retryable with a good reader and only then swaps
    assert env.promote().ok
    assert_whole(env)


def test_promote_corrupt_head_is_caught_too(env):
    env.offload()
    # damage the stub's head on disk: the whole-file hash must notice
    with open(env.stub, "r+b") as f:
        b = f.read(1)
        f.seek(0)
        f.write(bytes([b[0] ^ 0xFF]))
    r = env.promote()
    assert not r.ok and os.path.islink(env.lib)


def test_promote_reader_error_is_resumable(env):
    env.offload()
    r = env.promote(FakeReader(env.data, fail_times=1))
    assert not r.ok and r.state == "promoting"
    assert os.path.islink(env.lib)
    assert env.promote().ok
    assert_whole(env)


def test_promote_short_read_never_swaps(env):
    env.offload()

    class Short(FakeReader):
        def read_range(self, remote_path, offset, length):
            return super().read_range(remote_path, offset, length)[:-1]

    r = env.promote(Short(env.data))
    assert not r.ok and os.path.islink(env.lib)
    assert st.get(env.db, ID)["state"] == "promoting"


def test_promote_refuses_if_L_was_replaced(env):
    env.offload()
    os.unlink(env.lib)
    with open(env.lib, "wb") as f:
        f.write(b"user data")
    r = env.promote()
    assert not r.ok
    assert open(env.lib, "rb").read() == b"user data"


# ---------------------------------------------------------------- demote


def test_demote_after_promote(env):
    env.offload()
    env.promote()
    assert_whole(env)
    row = st.get(env.db, ID)
    st.upsert(env.db, {"id": ID, "nzb_path": "/n/x.nzb"})
    # entry left by the previous cycle is fine; fake tierfs recreates it if missing
    os.unlink(env.entry)
    r = ops.demote(env.db, st.get(env.db, ID), **env.kw(after_step=env.hook()))
    assert r.ok and r.state == "offloaded"
    assert_offloaded(env)
    assert st.get(env.db, ID)["sha256"] == row["sha256"] == env.sha


def test_demote_needs_nzb_and_sha(env):
    r = ops.demote(env.db, env.row, **env.kw())
    assert not r.ok and "NZB" in r.reason
    assert not os.path.islink(env.lib)
