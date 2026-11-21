"""Regression tests for the second review of the file operations (N1-N13)."""
import errno
import hashlib
import os
import sqlite3
import sys

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Crash, Env

TMP = ops.TMP_SUFFIX


def crash_at(monkeypatch, name, before=None):
    def p(n):
        if before and n in before:
            before[n]()
        if n == name:
            raise Crash(n)
    monkeypatch.setattr(ops, "_point", p)


def no_crash(monkeypatch):
    monkeypatch.setattr(ops, "_point", lambda n: None)


def import_upgrade(env, tmp_path, size=60 * MIB):
    new = os.urandom(size)
    imp = str(tmp_path / "lib" / ".import")

    def upgrade():
        with open(imp, "wb") as f:
            f.write(new)
        os.rename(imp, env.lib)
    return new, upgrade


# ---- N4: real file system only

MI = (
    "20 1 0:18 / / rw - ext4 /dev/sda1 rw\n"
    "30 20 0:30 / /mnt/user rw - fuse.shfs shfs rw\n"
    "31 20 0:31 / /mnt/disk1 rw - xfs /dev/md1 rw\n"
    "32 20 0:32 / /mnt/cache rw - btrfs /dev/nvme0n1p1 rw\n"
    "33 20 0:33 / /mnt/with\\040space rw - zfs pool/x rw\n"
    "34 20 0:34 / /merged rw - overlay overlay rw\n"
)


def test_fs_type_of_longest_mount():
    assert ops.fs_type_of("/mnt/disk1/Movies/a.mkv", MI) == "xfs"
    assert ops.fs_type_of("/mnt/user/Movies/a.mkv", MI) == "fuse.shfs"
    assert ops.fs_type_of("/mnt/cache", MI) == "btrfs"
    assert ops.fs_type_of("/mnt/with space/x", MI) == "zfs"
    assert ops.fs_type_of("/etc/x", MI) == "ext4"
    assert ops.fs_type_of("/mnt/disk10/x", MI) == "ext4"  # not under /mnt/disk1
    assert ops.fs_type_of("/x", "garbage") is None


@pytest.mark.parametrize("bad", ["fuse.shfs", "fuse", "fuseblk", "overlay", None])
def test_N4_union_or_unknown_fs_refused(tmp_path, monkeypatch, bad):
    env = Env(tmp_path)
    monkeypatch.setattr(ops, "_fs_type", lambda p: bad)
    r = env.offload()  # a refusal Result, not an exception
    assert not r.ok and "file system" in r.reason
    assert os.path.isfile(env.lib) and not os.path.islink(env.lib)
    assert env.read_lib() == env.data


@pytest.mark.parametrize("good", ["xfs", "btrfs", "ext4", "zfs", "tmpfs"])
def test_N4_real_fs_allowed(good):
    ops._require_real_fs.__wrapped__ if False else None
    real = ops._fs_type
    try:
        ops._fs_type = lambda p: good
        ops._require_real_fs("/a")
    finally:
        ops._fs_type = real


def test_N4_fs_checked_again_before_punching(tmp_path, monkeypatch):
    env = Env(tmp_path)
    calls = {"n": 0}

    def flip(p):
        calls["n"] += 1
        return "xfs" if calls["n"] <= 3 else "fuse.shfs"  # fine at step 1, a union at step 6
    monkeypatch.setattr(ops, "_fs_type", flip)
    with pytest.raises(ops.OpsError):
        env.offload()
    with open(env.stub, "rb") as f:
        assert f.read() == env.data  # nothing punched


# ---- N6: sha256 from qualify is required


def test_N6_offload_without_sha256_refused(tmp_path):
    env = Env(tmp_path)
    st.upsert(env.db, {"id": ID, "sha256": None})
    row = {**env.row, "sha256": None}
    r = ops.offload(env.db, row, **env.kw())
    assert not r.ok and "sha256" in r.reason
    assert not os.path.lexists(env.stub) and not os.path.islink(env.lib)


# ---- N1 / N2 / N8: promote takes over an offload that died after the exchange


def test_N1_promote_takeover_after_exchange_settles(tmp_path, monkeypatch):
    env = Env(tmp_path)
    crash_at(monkeypatch, "offload.exchanged")
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    assert os.stat(env.stub).st_nlink == 2
    r = env.promote()
    assert r.ok and r.state == "whole", r
    assert not os.path.lexists(env.lib + TMP)
    assert env.read_lib() == env.data and os.stat(env.lib).st_nlink == 1


def test_N2_promote_takeover_puts_import_back(tmp_path, monkeypatch):
    env = Env(tmp_path)
    new, upgrade = import_upgrade(env, tmp_path)
    crash_at(monkeypatch, "offload.exchanged", before={"offload.tmp_symlink": upgrade})
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    r = env.promote()
    assert not r.ok
    assert env.read_lib() == new and not os.path.islink(env.lib)
    assert not os.path.lexists(env.lib + TMP)
    assert st.get(env.db, ID)["state"] == "failed"


def test_N1_fill_refusal_marks_failed_not_retried(tmp_path, monkeypatch):
    env = Env(tmp_path)
    assert env.offload().ok
    os.link(env.stub, str(tmp_path / "extra"))  # stub gets a second name
    r = env.promote()
    assert not r.ok and r.state == "failed"
    assert st.get(env.db, ID)["state"] == "failed"
    assert any(e["kind"] == "failed" for e in st.events(env.db, ID))


def test_N8_takeover_leaves_no_tmp_symlink(tmp_path, monkeypatch):
    env = Env(tmp_path)
    crash_at(monkeypatch, "offload.tmp_symlink")
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    r = env.promote()
    assert r.ok and r.state == "whole"
    assert not os.path.lexists(env.lib + TMP)


# ---- N5: directory fsync between the exchange and the record of step 4


def test_N5_dir_fsync_after_exchange_before_step4_offload(tmp_path, monkeypatch):
    env = Env(tmp_path)
    seq = []
    re, rf, rp, rs = ops._exchange, ops._fsync_dir, ops.punch_hole, st.set_state
    monkeypatch.setattr(ops, "_exchange", lambda a, b: (seq.append(("exchange", b)), re(a, b))[1])
    monkeypatch.setattr(ops, "_fsync_dir", lambda d: (seq.append(("fsync_dir", d)), rf(d))[1])
    monkeypatch.setattr(ops, "punch_hole", lambda *a: (seq.append(("punch",)), rp(*a))[1])
    monkeypatch.setattr(st, "set_state",
                        lambda db, i, s, **f: (seq.append(("state", s, f.get("step"))), rs(db, i, s, **f))[1])
    ru = st.update_if  # the offload steps are compare-and-set writes now
    monkeypatch.setattr(st, "update_if", lambda db, i, e, **f: (
        seq.append(("state", f.get("state", e), f.get("step"))), ru(db, i, e, **f))[1])
    assert env.offload().ok
    i, j = seq.index(("exchange", env.lib)), seq.index(("punch",))
    mid = seq[i:j]
    d = ("fsync_dir", os.path.dirname(env.lib))
    assert d in mid and mid.index(d) < mid.index(("state", "offloading", 4))


def test_N5_dir_fsync_after_exchange_before_step4_promote(tmp_path, monkeypatch):
    env = Env(tmp_path)
    assert env.offload().ok
    seq = []
    re, rf, rs = ops._exchange, ops._fsync_dir, st.set_state
    monkeypatch.setattr(ops, "_exchange", lambda a, b: (seq.append(("exchange", b)), re(a, b))[1])
    monkeypatch.setattr(ops, "_fsync_dir", lambda d: (seq.append(("fsync_dir", d)), rf(d))[1])
    monkeypatch.setattr(st, "set_state",
                        lambda db, i, s, **f: (seq.append(("state", s, f.get("step"))), rs(db, i, s, **f))[1])
    assert env.promote().ok
    i = seq.index(("exchange", env.lib))
    mid = seq[i:]
    d = ("fsync_dir", os.path.dirname(env.lib))
    assert d in mid and mid.index(d) < mid.index(("state", "promoting", 4))


# ---- N7 / N11: apply needs the environment


def test_N7_apply_without_env_refused(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.delenv("TIERING_APPLY")
    r = env.offload()
    assert not r.ok and "TIERING_APPLY" in r.reason
    assert not os.path.islink(env.lib) and not os.path.lexists(env.stub)
    assert env.offload.__self__ is env
    monkeypatch.setenv("TIERING_APPLY", "1")
    assert env.offload().ok
    monkeypatch.delenv("TIERING_APPLY")
    r = env.promote()
    assert not r.ok and "TIERING_APPLY" in r.reason and os.path.islink(env.lib)


def test_N11_apply_without_canary_refused(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.delenv("TIERING_PROC_CANARY")
    r = env.offload()
    assert not r.ok and "TIERING_PROC_CANARY" in r.reason
    assert not os.path.lexists(env.stub)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_N11_scan_without_canary_is_not_clear(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.delenv("TIERING_PROC_CANARY")
    assert -1 in ops.scan_open_holders(env.lib, ignore_unreadable=True)


# ---- N9: a hardlink that appears while hashing aborts cleanly, retryable


def test_N9_hardlink_after_step1_aborts_and_retries(tmp_path):
    env = Env(tmp_path)
    xs = str(tmp_path / "xseed.mkv")

    def h(step):
        env.fake_tierfs()
        if step == 1 and not os.path.exists(xs):
            os.link(env.lib, xs)
    r = env.offload(after_step=h)
    assert not r.ok and "hardlinks" in r.reason
    assert not os.path.lexists(env.stub) and not os.path.islink(env.lib)
    row = st.get(env.db, ID)
    assert row["state"] == "whole"
    os.unlink(xs)
    assert env.offload().ok  # the next pass can retry


# ---- N10: no RENAME_EXCHANGE


@pytest.mark.parametrize("e", [errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP])
def test_N10_exchange_unsupported_leaves_l_untouched_offload(tmp_path, monkeypatch, e):
    env = Env(tmp_path)

    def bad(a, b):
        raise OSError(e, "renameat2 failed", a, b)
    monkeypatch.setattr(ops, "_exchange", bad)
    with pytest.raises(ops.OpsError, match="RENAME_EXCHANGE"):
        env.offload()
    assert not os.path.lexists(env.lib + TMP)
    assert os.path.isfile(env.lib) and not os.path.islink(env.lib)
    assert env.read_lib() == env.data


def test_N10_exchange_unsupported_leaves_l_untouched_promote(tmp_path, monkeypatch):
    env = Env(tmp_path)
    assert env.offload().ok
    link_target = os.readlink(env.lib)

    def bad(a, b):
        raise OSError(errno.EINVAL, "renameat2 failed", a, b)
    monkeypatch.setattr(ops, "_exchange", bad)
    with pytest.raises(ops.OpsError, match="RENAME_EXCHANGE"):
        env.promote()
    assert os.readlink(env.lib) == link_target and not os.path.lexists(env.lib + TMP)


# ---- N12: stale head/tail hashes do not block a later offload


def test_N12_upgrade_after_promote_can_be_demoted(tmp_path):
    env = Env(tmp_path)
    assert env.offload().ok and env.promote().ok
    row = st.get(env.db, ID)
    assert row["head_b3"] is None and row["tail_b3"] is None
    new = os.urandom(55 * MIB)
    imp = str(tmp_path / "lib" / ".import")
    with open(imp, "wb") as f:
        f.write(new)
    os.rename(imp, env.lib)
    s = os.stat(env.lib)
    st.upsert(env.db, {"id": ID, "size": s.st_size, "mtime_ns": s.st_mtime_ns,
                       "sha256": hashlib.sha256(new).hexdigest()})
    # even a stale head_b3 left in the row (older databases) must not block
    st.upsert(env.db, {"id": ID, "head_b3": "0" * 64, "tail_b3": "0" * 64})
    os.unlink(env.entry)
    r = ops.demote(env.db, st.get(env.db, ID), **env.kw(after_step=env.hook()))
    assert r.ok, r


# ---- N13: the stub must still hold the original before tmp is dropped


def test_N13_settle_keeps_last_name_if_stub_vanished(tmp_path, monkeypatch):
    env = Env(tmp_path)
    crash_at(monkeypatch, "offload.exchanged")
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    os.unlink(env.stub)
    r = env.offload(after_step=lambda s: None)
    assert not r.ok
    assert not os.path.lexists(env.lib + TMP)
    assert os.path.isfile(env.lib) and not os.path.islink(env.lib)
    assert env.read_lib() == env.data


# ---- N3: last_stat_ok_ts is REAL


def test_N3_last_stat_ok_ts_real_and_migrated(tmp_path):
    p = tmp_path / "old.db"
    st.open_db(p).close()
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE files DROP COLUMN last_stat_ok_ts")
    c.execute("ALTER TABLE files ADD COLUMN last_stat_ok_ts TEXT")
    c.execute("INSERT INTO files (id, created_ts, updated_ts, last_stat_ok_ts) "
              "VALUES ('b' * 1, 1, 1, '123.5')".replace("'b' * 1", "'" + "b" * 16 + "'"))
    c.commit()
    c.close()
    db = st.open_db(p)
    decl = {r[1]: r[2] for r in db.execute("PRAGMA table_info(files)")}
    assert decl["last_stat_ok_ts"] == "REAL"
    v = st.get(db, "b" * 16)["last_stat_ok_ts"]
    assert isinstance(v, float) and v == 123.5
    st.upsert(db, {"id": "a" * 16, "state": "offloaded"})
    assert st.update_if(db, "a" * 16, "offloaded", last_stat_ok_ts=1000.5)
    assert isinstance(st.get(db, "a" * 16)["last_stat_ok_ts"], float)


def test_N3_fresh_schema_real(tmp_path):
    db = st.open_db(tmp_path / "n.db")
    assert {r[1]: r[2] for r in db.execute("PRAGMA table_info(files)")}["last_stat_ok_ts"] == "REAL"
