import hashlib
import os
import subprocess
import sys

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Env, FakeReader  # noqa: F401


class ZeroReader:
    """AltMount-style: HTTP 206 with zeros for missing segments."""
    def read_range(self, remote_path, offset, length):
        return bytes(length)


def test_A_promote_of_failed_offload_writes_into_live_L(tmp_path):
    env = Env(tmp_path)

    def h(step):
        env.fake_tierfs()
        if step == 3:  # in-place tag edit (mkvpropedit-style) touches the head
            with open(env.lib, "r+b") as f:
                f.write(b"Z")
    r = env.offload(after_step=h)
    assert r.state == "failed" and not os.path.islink(env.lib)
    live = env.read_lib()
    ops.promote(env.db, ID, ZeroReader(), apply=True, manifest_dir=str(tmp_path / "manifests"))
    assert env.read_lib() == live, "promote overwrote the middle of the live library file"


def test_B_L_replaced_during_mount_wait_is_destroyed(tmp_path, monkeypatch):
    env = Env(tmp_path)
    newfile = str(tmp_path / "lib" / ".import")
    new = os.urandom(60 * MIB)
    with open(newfile, "wb") as f:
        f.write(new)
    real = ops._mount_serves
    def serves(target, size):  # Radarr upgrade lands during the tierfs wait
        if os.path.exists(newfile):
            os.rename(newfile, env.lib)
            env.fake_tierfs()
        return real(target, size)
    monkeypatch.setattr(ops, "_mount_serves", serves)
    env.offload()
    found = False
    for dp, _, fns in os.walk(tmp_path):
        for n in fns:
            p = os.path.join(dp, n)
            if os.path.isfile(p) and os.path.getsize(p) == len(new):
                found |= open(p, "rb").read() == new
    assert found, "upgraded file's only link was replaced by the offload rename"


def test_C_recorded_sha256_is_trusted_without_rehash(tmp_path):
    env = Env(tmp_path)
    hl, tl = 10 * MIB, 32 * MIB
    zeroed = env.data[:hl] + bytes(len(env.data) - hl - tl) + env.data[-tl:]
    st.upsert(env.db, {"id": ID, "sha256": hashlib.sha256(zeroed).hexdigest()})
    r = env.offload()
    assert not r.ok, "offload accepted a sha256 that does not match the local file"


def test_D_unreadable_proc_fd_counts_as_clear(tmp_path, monkeypatch):
    env = Env(tmp_path)
    p = subprocess.Popen([sys.executable, "-c",
                          "import sys,time; f=open(sys.argv[1],'rb'); print('r',flush=True); time.sleep(30)",
                          env.lib], stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b"r"
        real = os.listdir
        def ld(path):
            if str(path) == f"/proc/{p.pid}/fd":
                raise PermissionError(13, "denied")
            return real(path)
        monkeypatch.setattr(ops.os, "listdir", ld)
        try:
            holders = ops.scan_open_holders(env.lib)
        except OSError:
            return  # fail closed: acceptable
        assert holders, "unreadable process treated as not holding the file"
    finally:
        p.kill()
        p.wait()


def test_C2_stale_sha256_matching_zero_filled_remote_is_refused(tmp_path):
    env = Env(tmp_path)
    hl, tl = 10 * MIB, 32 * MIB
    zeroed = env.data[:hl] + bytes(len(env.data) - hl - tl) + env.data[-tl:]
    st.upsert(env.db, {"id": ID, "sha256": hashlib.sha256(zeroed).hexdigest()})
    before = open(env.lib, "rb").read()
    r = env.offload()
    assert not r.ok and "sha256" in r.reason
    assert not os.path.islink(env.lib) and open(env.lib, "rb").read() == before
    assert not os.path.lexists(env.stub) and not os.path.lexists(env.manifest)
    assert st.get(env.db, ID)["state"] == "whole"
