"""Share path -> disk path mapping, and the ops that use it (Path namespaces)."""
import os

import pytest

from tiering.offload import ops, paths
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, Env

SHARE = "/share/Movies"
NAME = "Movie (2020).mkv"


def test_parse_orders_longest_first_and_keeps_both_disks():
    p = paths.parse_path_map("/s=/d1, /s/Movies=/d1/M ,/s/Movies=/d2/M,,")
    assert p == [("/s/Movies", "/d1/M"), ("/s/Movies", "/d2/M"), ("/s", "/d1")]
    assert paths.parse_path_map("") == [] and paths.parse_path_map(None) == []
    with pytest.raises(ValueError):
        paths.parse_path_map("/a")
    with pytest.raises(ValueError):
        paths.parse_path_map("/a=")


def test_disk_paths_only_existing_count():
    pairs = paths.parse_path_map("/s=/d1,/s=/d2")
    have = {"/d2/M/a.mkv"}
    ex = have.__contains__
    assert paths.disk_paths("/s/M/a.mkv", pairs, ex) == ["/d2/M/a.mkv"]
    have.add("/d1/M/a.mkv")
    assert paths.disk_paths("/s/M/a.mkv", pairs, ex) == ["/d1/M/a.mkv", "/d2/M/a.mkv"]
    assert paths.disk_paths("/s/M/none.mkv", pairs, ex) == []
    assert paths.disk_paths("/other/a.mkv", pairs, ex) == []
    # already a disk path: kept as is, when it exists
    assert paths.disk_paths("/d1/M/a.mkv", pairs, ex) == ["/d1/M/a.mkv"]
    assert paths.disk_paths("/d1/M/zz.mkv", pairs, ex) == []
    # "/sx" is not under "/s"
    assert paths.disk_paths("/sx/M/a.mkv", pairs, lambda p: True) == []


def test_disk_paths_longest_prefix_wins():
    pairs = paths.parse_path_map("/s=/d1,/s/Movies=/d2/Mov")
    ex = {"/d2/Mov/a.mkv", "/d1/Movies/a.mkv"}.__contains__
    assert paths.disk_paths("/s/Movies/a.mkv", pairs, ex) == ["/d2/Mov/a.mkv"]
    # the longer prefix has no file: fall back to the shorter one
    assert paths.disk_paths("/s/Movies/b.mkv", pairs, {"/d1/Movies/b.mkv"}.__contains__) == [
        "/d1/Movies/b.mkv"]


class Two:
    """Two fake disks under tmp that share one share prefix."""

    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.d1 = os.path.dirname(self.env.lib)  # <tmp>/lib is disk 1
        self.d2 = str(tmp_path / "lib2")
        os.mkdir(self.d2)
        self.share = f"{SHARE}/{NAME}"
        self.map = f"{SHARE}={self.d1},{SHARE}={self.d2}"
        self.env.row = {**self.env.row, "lib_path": self.share}
        st.upsert(self.env.db, {"id": ID, "lib_path": self.share})

    def move(self, to):
        new = os.path.join(to, NAME)
        os.rename(self.env.lib, new)
        self.env.lib = new

    def offload(self, **kw):
        return self.env.offload(path_map=self.map, **kw)

    def promote(self, **kw):
        return self.env.promote(path_map=self.map, **kw)


def test_offload_on_one_disk_keeps_share_path_in_db(tmp_path):
    t = Two(tmp_path)
    assert t.offload().ok
    assert os.path.islink(t.env.lib)
    assert st.get(t.env.db, ID)["lib_path"] == t.share  # never the disk path
    assert t.promote().ok
    assert st.get(t.env.db, ID)["lib_path"] == t.share
    assert t.env.read_lib() == t.env.data


def test_db_share_path_wins_over_callers_disk_path(tmp_path):
    t = Two(tmp_path)
    t.env.row = {**t.env.row, "lib_path": t.env.lib}  # a caller handing the disk path
    assert t.offload().ok
    assert st.get(t.env.db, ID)["lib_path"] == t.share


def test_duplicate_on_both_disks_refuses(tmp_path):
    t = Two(tmp_path)
    with open(os.path.join(t.d2, NAME), "wb") as f:
        f.write(b"x" * 1024)
    r = t.offload()
    assert not r.ok and "2 disk paths" in r.reason
    assert not os.path.lexists(t.env.stub) and not os.path.islink(t.env.lib)
    assert st.get(t.env.db, ID)["state"] == "whole"


def test_duplicate_refuses_promote_too(tmp_path):
    t = Two(tmp_path)
    assert t.offload().ok
    os.symlink("/nowhere", os.path.join(t.d2, NAME))  # even a stray link counts
    r = t.promote()
    assert not r.ok and "2 disk paths" in r.reason
    assert st.get(t.env.db, ID)["state"] == "offloaded"


def test_file_not_on_any_disk_refuses(tmp_path):
    t = Two(tmp_path)
    os.unlink(t.env.lib)
    r = t.offload()
    assert not r.ok and "0 disk paths" in r.reason


def test_whole_file_moved_between_disks_between_runs_still_resolves(tmp_path):
    t = Two(tmp_path)
    assert t.offload(apply=False).ok  # dry run resolves on disk 1
    t.move(t.d2)
    assert t.offload().ok  # now resolved on disk 2
    assert os.path.islink(t.env.lib) and t.env.lib.startswith(t.d2)
    assert st.get(t.env.db, ID)["lib_path"] == t.share


def test_offloaded_link_moved_between_disks_promotes(tmp_path):
    t = Two(tmp_path)
    assert t.offload().ok
    t.move(t.d2)  # the symlink L moves with its directory entry
    assert t.promote().ok
    assert t.env.read_lib() == t.env.data and not os.path.islink(t.env.lib)


def test_no_map_uses_path_as_is(tmp_path, monkeypatch):
    monkeypatch.delenv("TIERING_PATH_MAP", raising=False)
    env = Env(tmp_path)
    assert env.offload().ok and env.promote().ok


def test_env_map_is_used(tmp_path, monkeypatch):
    t = Two(tmp_path)
    monkeypatch.setenv("TIERING_PATH_MAP", t.map)
    assert ops.offload(t.env.db, t.env.row, **t.env.kw(after_step=t.env.hook())).ok
    assert st.get(t.env.db, ID)["lib_path"] == t.share


def test_bad_map_refuses(tmp_path):
    env = Env(tmp_path)
    r = env.offload(path_map="nonsense")
    assert not r.ok and "TIERING_PATH_MAP" in r.reason


# ---- stub root picked from TIERFS_STUB_ROOTS by st_dev


def test_stub_root_picked_by_device(tmp_path, monkeypatch):
    env = Env(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    real = ops._dev
    monkeypatch.setattr(ops, "_dev", lambda p: -7 if str(p) == str(other) else real(p))
    monkeypatch.setenv("TIERFS_STUB_ROOTS", f"{other},{tmp_path / 'stubs'}")
    kw = env.kw()
    del kw["stub_root"]
    assert ops.offload(env.db, env.row, after_step=env.hook(), **kw).ok
    assert os.path.exists(env.stub)


def test_stub_root_none_or_several_refuse(tmp_path, monkeypatch):
    env = Env(tmp_path)
    kw = env.kw()
    del kw["stub_root"]
    monkeypatch.delenv("TIERFS_STUB_ROOTS", raising=False)
    r = ops.offload(env.db, env.row, **kw)
    assert not r.ok and "TIERFS_STUB_ROOTS" in r.reason
    extra = tmp_path / "stubs2"
    extra.mkdir()
    monkeypatch.setenv("TIERFS_STUB_ROOTS", f"{tmp_path / 'stubs'},{extra}")  # same disk twice
    r = ops.offload(env.db, env.row, **kw)
    assert not r.ok and "need exactly one" in r.reason
    real = ops._dev
    monkeypatch.setattr(ops, "_dev", lambda p: -1 if "stubs" in str(p) else real(p))
    r = ops.offload(env.db, env.row, **kw)  # none on L's disk
    assert not r.ok and "need exactly one" in r.reason
    assert not os.path.lexists(env.stub) and not os.path.islink(env.lib)


def test_given_stub_root_keeps_same_device_check(tmp_path, monkeypatch):
    env = Env(tmp_path)
    real = ops._dev
    monkeypatch.setattr(ops, "_dev", lambda p: -1 if "stubs" in str(p) else real(p))
    r = env.offload()
    assert not r.ok and "different filesystem" in r.reason
