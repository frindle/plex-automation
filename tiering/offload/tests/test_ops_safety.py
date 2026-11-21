"""Crash points inside the swaps, step-6 re-check, locking, takeover, chunk list."""
import os
import subprocess
import sys

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import (  # noqa: F401
    ID, MIB, Crash, Env, FakeReader, assert_offloaded, assert_whole,
)

from tiering.offload.tests.test_ops import pytestmark  # noqa: E402,F401

OFFLOAD_POINTS = ["offload.linked", "offload.chunks_written", "offload.manifest_written",
                  "offload.tmp_symlink", "offload.exchanged", "offload.tmp_unlinked"]
PROMOTE_POINTS = ["promote.tmp_linked", "promote.exchanged", "promote.tmp_unlinked",
                  "promote.stub_unlinked", "promote.manifest_removed"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


def crash_at_point(monkeypatch, env, name):
    def p(n):
        assert os.path.exists(env.lib), f"L missing at {n}"  # resolves at every syscall boundary
        if n == name:
            raise Crash(n)
    monkeypatch.setattr(ops, "_point", p)


def watch_points(monkeypatch, env):
    def p(n):
        assert os.path.exists(env.lib), f"L missing at {n}"
    monkeypatch.setattr(ops, "_point", p)


@pytest.mark.parametrize("name", OFFLOAD_POINTS)
def test_offload_crash_between_syscalls(env, monkeypatch, name):
    crash_at_point(monkeypatch, env, name)
    with pytest.raises(Crash):
        env.offload()
    assert os.path.exists(env.lib)
    watch_points(monkeypatch, env)
    assert env.offload().ok
    assert_offloaded(env)
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)


@pytest.mark.parametrize("name", PROMOTE_POINTS)
def test_promote_crash_between_syscalls(env, monkeypatch, name):
    env.offload()
    crash_at_point(monkeypatch, env, name)
    with pytest.raises(Crash):
        env.promote()
    assert os.path.exists(env.lib)
    watch_points(monkeypatch, env)
    assert env.promote().ok
    assert_whole(env)


def test_new_import_during_offload_swap_is_put_back(env, monkeypatch):
    new = os.urandom(60 * MIB)
    imp = str(env.root / "lib" / ".import")

    def p(n):
        if n == "offload.tmp_symlink":  # arr upgrade lands right before the exchange
            with open(imp, "wb") as f:
                f.write(new)
            os.rename(imp, env.lib)
    monkeypatch.setattr(ops, "_point", p)
    r = env.offload()
    assert not r.ok and st.get(env.db, ID)["state"] == "failed"
    assert open(env.lib, "rb").read() == new and not os.path.islink(env.lib)
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    assert os.path.getsize(env.stub) == len(env.data)  # old data kept in the stub
    assert not os.path.lexists(env.manifest) and not os.path.lexists(env.chunks)


def test_new_import_during_promote_swap_is_put_back(env, monkeypatch):
    env.offload()
    new = os.urandom(60 * MIB)
    imp = str(env.root / "lib" / ".import")

    def p(n):
        if n == "promote.tmp_linked":
            with open(imp, "wb") as f:
                f.write(new)
            os.rename(imp, env.lib)
    monkeypatch.setattr(ops, "_point", p)
    r = env.promote()
    assert not r.ok
    assert open(env.lib, "rb").read() == new
    assert not os.path.lexists(env.lib + ops.TMP_SUFFIX)
    assert os.path.exists(env.stub)


def test_exchange_unavailable_refuses_apply(env, monkeypatch):
    monkeypatch.setattr(ops, "exchange_available", lambda: False)
    r = env.offload()
    assert not r.ok and "renameat2" in r.reason
    assert not os.path.lexists(env.stub) and not os.path.islink(env.lib)
    assert env.offload(apply=False).ok  # dry run still fine


def test_resume_requires_original_inode(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(1))
    new = os.urandom(len(env.data))
    imp = str(env.root / "lib" / ".import")
    with open(imp, "wb") as f:
        f.write(new)
    os.rename(imp, env.lib)  # new inode at L
    r = env.offload()
    assert not r.ok
    assert open(env.lib, "rb").read() == new and not os.path.lexists(env.stub)


# ---- step 6 re-check


def test_step6_resume_refuses_when_L_replaced(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(5))
    os.unlink(env.lib)
    os.link(env.stub, env.lib)  # a regular name for the stub inode appears at L
    r = env.offload()
    assert not r.ok
    assert os.stat(env.stub).st_blocks * 512 >= len(env.data)  # not punched
    assert os.path.isfile(env.lib) and not os.path.islink(env.lib)


def test_step6_resume_refuses_when_stub_has_another_name(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(5))
    alias = str(env.root / "lib" / "alias.mkv")
    os.link(env.stub, alias)
    r = env.offload()
    assert not r.ok and "links" in r.reason
    assert os.stat(env.stub).st_blocks * 512 >= len(env.data)
    os.unlink(alias)
    assert env.offload().ok
    assert_offloaded(env)


# ---- lock


def test_second_run_on_same_id_is_busy(env):
    seen = {}

    def h(step):
        env.fake_tierfs()
        if step == 2:
            seen["off"] = ops.offload(env.db, env.row, **env.kw())
            seen["pro"] = ops.promote(env.db, ID, FakeReader(env.data), apply=True,
                                      manifest_dir=str(env.root / "manifests"))
    assert env.offload(after_step=h).ok
    assert not seen["off"].ok and "busy" in seen["off"].reason
    assert not seen["pro"].ok and "busy" in seen["pro"].reason
    assert_offloaded(env)
    assert env.promote().ok  # lock released afterwards


# ---- promote takes over a stuck offload


@pytest.mark.parametrize("crash_at", [1, 2, 3])
def test_promote_aborts_stuck_offload_when_L_is_original(env, crash_at):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(crash_at))
    reader = FakeReader(env.data)
    r = env.promote(reader)
    assert r.ok and r.state == "whole"
    assert reader.calls == []
    assert_whole(env)
    assert st.events(env.db, ID)[-1]["kind"] == "offload_aborted"


def test_promote_dry_run_on_stuck_offload_changes_nothing(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(3))
    stubs, mans = os.listdir(env.root / "stubs"), os.listdir(env.root / "manifests")
    r = ops.promote(env.db, ID, FakeReader(env.data), apply=False,
                    manifest_dir=str(env.root / "manifests"))
    assert r.ok and r.dry_run
    assert (stubs, mans) == (os.listdir(env.root / "stubs"), os.listdir(env.root / "manifests"))
    assert st.get(env.db, ID)["state"] == "offloading"


def test_promote_of_offloading_row_past_swap_works(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(4))
    assert env.promote().ok
    assert_whole(env)


# ---- chunk list


def test_promote_refuses_without_chunk_list(env):
    env.offload()
    os.unlink(env.chunks)
    r = env.promote()
    assert not r.ok and "chunk list" in r.reason
    assert os.path.islink(env.lib) and st.get(env.db, ID)["state"] == "offloaded"


def test_promote_zero_remote_fails_on_first_chunk_nothing_written(env):
    env.offload()

    class Zero:
        calls = 0

        def read_range(self, p, o, n):
            Zero.calls += 1
            return bytes(n)
    r = env.promote(Zero(), chunk=4 * MIB)
    assert not r.ok and r.state == "failed"
    assert Zero.calls == 1
    assert os.stat(env.stub).st_blocks * 512 <= 10 * MIB + 32 * MIB + MIB
    assert os.path.islink(env.lib)


def test_odd_size_round_trip(tmp_path):
    env = Env(tmp_path, size=50 * MIB + 12345)
    assert env.offload().ok
    row = st.get(env.db, ID)
    assert row["tail_len"] == 32 * MIB + 12345
    assert (row["size"] - row["tail_len"]) % MIB == 0
    assert env.promote().ok
    assert_whole(env)
    env.db.close()


def test_chunk_list_removed_on_promote(env):
    env.offload()
    assert os.path.exists(env.chunks)
    env.promote()
    assert not os.path.exists(env.chunks)


# ---- /proc scan fails closed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_canary_missing_means_not_clear(env, monkeypatch):
    monkeypatch.setenv("TIERING_PROC_CANARY", "no-such-comm-xyz")
    assert -1 in ops.scan_open_holders(env.lib, ignore_unreadable=True)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_canary_visible_is_clear(env, monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        comm = open(f"/proc/{p.pid}/comm").read().strip()
        monkeypatch.setenv("TIERING_PROC_CANARY", comm)
        assert ops.scan_open_holders(env.lib, ignore_unreadable=True) == []
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_canary_list_needs_every_name_visible(env, monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        comm = open(f"/proc/{p.pid}/comm").read().strip()
        monkeypatch.setenv("TIERING_PROC_CANARY", f"{comm}, no-such-comm-xyz")
        assert -1 in ops.scan_open_holders(env.lib, ignore_unreadable=True)
        monkeypatch.setenv("TIERING_PROC_CANARY", f"{comm},{comm}")
        assert ops.scan_open_holders(env.lib, ignore_unreadable=True) == []
    finally:
        p.kill()
        p.wait()


def test_step3_rehashes_stub_and_refuses_on_mismatch(env):
    with pytest.raises(Crash):
        env.offload(after_step=env.hook(2))
    st.upsert(env.db, {"id": ID, "sha256": "0" * 64})  # stale or wrong stored hash
    r = env.offload()
    assert not r.ok and "sha256" in r.reason
    assert st.get(env.db, ID)["state"] == "failed"
    assert not os.path.islink(env.lib) and os.path.isfile(env.lib)
    assert not os.path.lexists(env.manifest) and not os.path.lexists(env.chunks)
    assert env.read_lib() == env.data
