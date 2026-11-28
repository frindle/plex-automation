"""ops.relink_to_mount: an offloaded L that points at the old mount directory (before
TIERFS_MOUNT moved to its fs/ subfolder) is pointed at the mount, and nothing else is
ever touched."""
import os

import pytest

from tiering.offload import ops
from tiering.offload import state as st

pytestmark = pytest.mark.skipif(not ops.exchange_available(), reason="no renameat2")

ID = "0123456789abcdef"
SIZE = 12345


class Env:
    def __init__(self, root):
        self.root = root
        for d in ("lib", "old", "old/fs"):
            os.mkdir(root / d)
        self.mount = str(root / "old" / "fs")
        self.old_target = str(root / "old" / f"{ID}.mkv")
        self.new_target = os.path.join(self.mount, f"{ID}.mkv")
        self.lib = str(root / "lib" / "Movie (2020).mkv")
        os.symlink(self.old_target, self.lib)
        self.db = st.open_db(root / "state.db")
        st.upsert(self.db, {"id": ID, "lib_path": self.lib, "size": SIZE, "state": "offloaded"})

    def serve(self, size=SIZE):
        """Stand-in for tierfs: the entry at the new mount with the recorded size."""
        with open(self.new_target, "wb") as f:
            f.truncate(size)

    def relink(self, **kw):
        kw.setdefault("mount_wait", 0.05)
        return ops.relink_to_mount(self.db, st.get(self.db, ID), self.mount, path_map=[], **kw)

    def events(self):
        return [e["kind"] for e in st.events(self.db, ID)]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_relinks_to_the_mount_once_it_serves(env):
    env.serve()
    assert env.relink() == "relinked"
    assert os.readlink(env.lib) == env.new_target
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    assert env.events() == ["relinked"]
    assert env.relink() == "ok"  # a second start changes nothing
    assert env.events() == ["relinked"]


def test_already_at_the_mount_is_ok(env):
    os.unlink(env.lib)
    os.symlink(env.new_target, env.lib)
    assert env.relink() == "ok" and env.events() == []


def test_not_served_or_wrong_size_leaves_L(env):
    assert env.relink().startswith("skip: tierfs does not serve")
    env.serve(SIZE - 1)
    assert env.relink().startswith("skip: tierfs does not serve")
    assert os.readlink(env.lib) == env.old_target and env.events() == []


def test_never_touches_a_regular_file_or_another_ids_link(env):
    env.serve()
    os.unlink(env.lib)
    with open(env.lib, "wb") as f:
        f.write(b"real")
    assert env.relink() == "skip: L is not a symlink"
    with open(env.lib, "rb") as f:
        assert f.read() == b"real"
    os.unlink(env.lib)
    os.symlink(str(env.root / "old" / "fedcba9876543210.mkv"), env.lib)
    assert env.relink() == "skip: L is not this id's tierfs symlink"
    assert os.readlink(env.lib).endswith("fedcba9876543210.mkv")


def test_only_offloaded_rows(env):
    env.serve()
    st.set_state(env.db, ID, "promoting")
    assert env.relink() == "skip: not offloaded"
    assert os.readlink(env.lib) == env.old_target


def test_busy_id_is_skipped(env):
    env.serve()
    with ops._id_lock(env.db, ID) as held:
        assert held
        assert env.relink() == f"skip: {ops._BUSY}"
    assert os.readlink(env.lib) == env.old_target


def test_L_replaced_during_the_swap_is_swapped_back(env, monkeypatch):
    """An import that lands a real file at L between the check and the exchange keeps it."""
    env.serve()
    real = ops._exchange
    done = []

    def racing(a, b):
        if not done:
            done.append(1)
            os.unlink(env.lib)
            with open(env.lib, "wb") as f:
                f.write(b"new import")
        real(a, b)

    monkeypatch.setattr(ops, "_exchange", racing)
    assert env.relink() == "skip: L changed during the relink; left as it was"
    with open(env.lib, "rb") as f:
        assert f.read() == b"new import"
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    assert env.events() == []


def test_leftover_tmp(env):
    env.serve()
    tmp = env.lib + ops.TMP_SUFFIX
    with open(tmp, "wb") as f:
        f.write(b"someone's")
    assert env.relink() == f"skip: L{ops.TMP_SUFFIX} exists and is not our symlink"
    assert os.readlink(env.lib) == env.old_target
    os.unlink(tmp)
    os.symlink(env.old_target, tmp)  # ours, from a relink that crashed
    assert env.relink() == "relinked"
    assert os.readlink(env.lib) == env.new_target and not os.path.lexists(tmp)
