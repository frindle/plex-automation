"""Regression tests for the third review (M1, L1-L5, guard in a dry run)."""
import os
import sqlite3
import sys
import types

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Crash, Env, FakeReader
from tiering.offload.tests.test_ops_review2 import crash_at, import_upgrade, no_crash

TMP = ops.TMP_SUFFIX


# ---- M1: resume with the mount spelled another way

@pytest.mark.parametrize("spell", ["dot", "alias", "slashes"])
def test_M1_resume_with_other_mount_spelling_keeps_L_resolving(tmp_path, monkeypatch, spell):
    env = Env(tmp_path)
    crash_at(monkeypatch, "offload.exchanged")
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    mount = str(tmp_path / "mount")
    if spell == "alias":
        os.symlink(mount, tmp_path / "alias")
        other = str(tmp_path / "alias")
    elif spell == "dot":
        other = mount + "/."
    else:
        other = mount + "//"
    r = ops.offload(env.db, env.row, **env.kw(tierfs_mount=other, after_step=env.hook()))
    assert r.ok and r.state == "offloaded", r
    assert os.path.exists(env.manifest) and os.path.islink(env.lib)
    assert os.path.exists(env.lib)  # resolves through the entry


def test_M1_abort_never_removes_manifest_while_L_points_at_it(tmp_path):
    env = Env(tmp_path)
    assert env.offload(after_step=lambda s: env.fake_tierfs()).ok
    # L is the tierfs symlink: an abort must not drop the manifest under it
    ops._abort_offload(env.db, ID, "test", env.lib, env.stub, str(tmp_path / "manifests"), 4)
    assert os.path.exists(env.manifest) and os.path.exists(env.chunks)
    assert os.path.exists(env.lib)


# ---- L1: the TEXT -> REAL migration is one transaction

def _text_db(p):
    db = st.open_db(p)
    db.execute("ALTER TABLE files DROP COLUMN last_stat_ok_ts")
    db.execute("ALTER TABLE files ADD COLUMN last_stat_ok_ts TEXT")
    for i in range(3):
        st.upsert(db, {"id": f"{i:016x}", "lib_path": f"/l/{i}.mkv", "state": "offloaded",
                       "size": 10 + i, "sha256": "ab" * 32})
        db.execute("UPDATE files SET last_stat_ok_ts=? WHERE id=?",
                   (str(1000.5 + i), f"{i:016x}"))
    db.commit()
    db.close()


def _decl(db):
    return {r[1]: r[2] for r in db.execute("PRAGMA table_info(files)")}


def test_L1_text_db_migrates(tmp_path):
    p = tmp_path / "old.db"
    _text_db(p)
    db = st.open_db(p)
    assert _decl(db)["last_stat_ok_ts"] == "REAL" and "last_stat_ok_ts_old" not in _decl(db)
    assert [r["last_stat_ok_ts"] for r in st.list_files(db)] == [1000.5, 1001.5, 1002.5]
    st.open_db(p).close()  # idempotent


@pytest.mark.parametrize("die_on", ["ADD COLUMN last_stat_ok_ts REAL", "DROP COLUMN",
                                    "UPDATE files SET last_stat_ok_ts"])
def test_L1_crash_mid_migration_loses_nothing(tmp_path, monkeypatch, die_on):
    p = tmp_path / "old.db"
    _text_db(p)
    real = sqlite3.connect

    class Dying(sqlite3.Connection):
        def execute(self, sql, *a):
            if die_on in sql:
                raise Crash(sql)
            return super().execute(sql, *a)

    monkeypatch.setattr(st, "sqlite3", types.SimpleNamespace(
        connect=lambda *a, **k: real(*a, factory=Dying, **k), Row=sqlite3.Row))
    with pytest.raises(Crash):
        st.open_db(p)
    monkeypatch.undo()
    db = st.open_db(p)
    assert _decl(db)["last_stat_ok_ts"] == "REAL" and "last_stat_ok_ts_old" not in _decl(db)
    assert [r["last_stat_ok_ts"] for r in st.list_files(db)] == [1000.5, 1001.5, 1002.5]
    st.upsert(db, st.get(db, f"{0:016x}"))  # the get -> upsert round trip works


@pytest.mark.parametrize("stage", ["renamed", "added", "copied"])
def test_L1_half_done_migration_is_finished_on_open(tmp_path, stage):
    """A database an earlier version left half-way (each ALTER had autocommitted)."""
    p = tmp_path / "half.db"
    _text_db(p)
    db = sqlite3.connect(p, isolation_level=None)
    db.execute("ALTER TABLE files RENAME COLUMN last_stat_ok_ts TO last_stat_ok_ts_old")
    if stage in ("added", "copied"):
        db.execute("ALTER TABLE files ADD COLUMN last_stat_ok_ts REAL")
    if stage == "copied":
        db.execute("UPDATE files SET last_stat_ok_ts = CAST(last_stat_ok_ts_old AS REAL)")
    db.close()
    db = st.open_db(p)
    assert _decl(db)["last_stat_ok_ts"] == "REAL" and "last_stat_ok_ts_old" not in _decl(db)
    assert [r["last_stat_ok_ts"] for r in st.list_files(db)] == [1000.5, 1001.5, 1002.5]


# ---- L2: a permanently short remote stops

def test_L2_short_remote_fails_after_three_attempts_at_same_offset(tmp_path):
    env = Env(tmp_path)
    assert env.offload().ok
    trunc = FakeReader(env.data[:14 * MIB])  # the middle is [10, 18) MiB
    for i in (1, 2):
        r = env.promote(trunc)
        assert not r.ok and r.state == "promoting"
        assert st.get(env.db, ID)["state"] == "promoting"
    r = env.promote(trunc)
    assert not r.ok and r.state == "failed"
    row = st.get(env.db, ID)
    assert row["state"] == "failed"
    # chunks are fetched one at a time: the second one is the short one
    assert str(14 * MIB) in row["restore_note"] and "restore path" in row["restore_note"]
    kinds = [e["kind"] for e in st.events(env.db, ID)]
    assert "promote_short_remote" in kinds
    assert os.path.islink(env.lib) and os.path.exists(env.lib)  # L still resolves


def test_L2_streak_is_broken_by_other_trouble_or_other_offset(tmp_path):
    env = Env(tmp_path)
    assert env.offload().ok
    trunc = FakeReader(env.data[:14 * MIB])
    assert not env.promote(trunc).ok and not env.promote(trunc).ok
    assert not env.promote(FakeReader(env.data, fail_times=1)).ok  # a network error
    assert st.get(env.db, ID)["state"] == "promoting"
    assert not env.promote(trunc).ok
    assert st.get(env.db, ID)["state"] == "promoting"  # the run restarted
    assert env.promote().ok  # a healthy remote still completes


def test_L2_corrupt_remote_still_fails_at_once(tmp_path):
    env = Env(tmp_path)
    assert env.offload().ok
    r = env.promote(FakeReader(env.data, corrupt_at=12 * MIB))
    assert not r.ok and st.get(env.db, ID)["state"] == "failed"


# ---- L3: nlink re-checked right before the exchange

def test_L3_hardlink_before_exchange_aborts_cleanly(tmp_path, monkeypatch):
    env = Env(tmp_path)
    xs = str(tmp_path / "xseed.mkv")
    crash_at_hook = {"offload.manifest_written": lambda: os.link(env.lib, xs)}
    monkeypatch.setattr(ops, "_point", lambda n: crash_at_hook.get(n, lambda: None)())
    r = env.offload()
    assert not r.ok and "links" in r.reason
    assert not os.path.islink(env.lib) and env.read_lib() == env.data
    assert not os.path.lexists(env.lib + TMP) and not os.path.lexists(env.stub)
    assert not os.path.exists(env.manifest) and not os.path.exists(env.chunks)
    row = st.get(env.db, ID)
    assert row["state"] == "whole" and row["stub_path"] is None
    assert open(xs, "rb").read() == env.data
    assert any(e["kind"] == "offload_aborted" for e in st.events(env.db, ID))


# ---- L4: the stub's directory is fsynced after promote unlinks it

def test_L4_stub_dir_fsynced_after_unlink(tmp_path, monkeypatch):
    env = Env(tmp_path)
    assert env.offload().ok
    seq = []
    ru, rf = os.unlink, ops._fsync_dir
    monkeypatch.setattr(os, "unlink", lambda p, *a, **k: (seq.append(("unlink", str(p))),
                                                        ru(p, *a, **k))[1])
    monkeypatch.setattr(ops, "_fsync_dir", lambda d: (seq.append(("fsync", str(d))), rf(d))[1])
    assert env.promote().ok
    i = seq.index(("unlink", env.stub))
    assert ("fsync", str(tmp_path / "stubs")) in seq[i:]


# ---- L5: take-over with an upgrade names the orphan stub

def test_L5_takeover_with_upgrade_names_orphan_and_keeps_it(tmp_path, monkeypatch):
    env = Env(tmp_path)
    new, upgrade = import_upgrade(env, tmp_path)
    crash_at(monkeypatch, "offload.exchanged", before={"offload.tmp_symlink": upgrade})
    with pytest.raises(Crash):
        env.offload()
    no_crash(monkeypatch)
    r = env.promote()
    assert not r.ok and env.read_lib() == new
    row = st.get(env.db, ID)
    assert row["state"] == "failed" and env.stub in row["restore_note"]
    ev = st.events(env.db, ID)
    assert env.stub in [e for e in ev if e["kind"] == "failed"][-1]["detail"]["reason"]
    orphan = [e for e in ev if e["kind"] == "takeover_orphan"]
    assert len(orphan) == 1 and orphan[0]["detail"]["stub"] == env.stub
    assert open(env.stub, "rb").read() == env.data  # never deleted


# ---- guard: a refusal Result in a dry run; the real mountinfo, no fakes

@pytest.mark.parametrize("apply", [False, True])
def test_guard_refuses_with_a_result_even_in_a_dry_run(tmp_path, monkeypatch, apply):
    env = Env(tmp_path)
    monkeypatch.setattr(ops, "_fs_type", lambda p: "fuse.shfs")
    r = env.offload(apply=apply)
    assert not r.ok and "fuse.shfs" in r.reason
    assert os.path.isfile(env.lib) and not os.path.lexists(env.stub)


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.exists(
    "/proc/self/mountinfo"), reason="needs /proc/self/mountinfo")
def test_guard_against_the_real_mountinfo(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_fs_type", ops.fs_type_of)  # undo the conftest fake
    t = ops.fs_type_of(tmp_path)
    assert t  # the real file's mount was found
    with pytest.raises(ops.OpsError):
        ops._require_real_fs("/proc/self")  # proc is never a real disk fs
    if t in ops.REAL_FS:
        ops._require_real_fs(tmp_path)
        env = Env(tmp_path)
        assert env.offload().ok and os.path.islink(env.lib)
    else:  # e.g. an overlay root: the guard refuses with a Result
        env = Env(tmp_path)
        r = env.offload()
        assert not r.ok and t in r.reason
