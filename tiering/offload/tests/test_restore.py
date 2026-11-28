"""ops.restore_from_path: a file comes back as a different release (the restore path).
Fakes for AltMount's importer and the WebDAV reader; synthetic paths only."""
import hashlib
import json
import os
import stat

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Env, tree  # noqa: F401
from tiering.offload.tests.test_ops import _punch_supported

pytestmark = pytest.mark.skipif(not _punch_supported(), reason="no PUNCH_HOLE")

REMOTE = "/restore/Other.Release.2020.1080p/Other.Release.2020.1080p.mkv"


class Importer:
    def __init__(self, remote=REMOTE, exc=None):
        self.remote, self.exc, self.calls = remote, exc, []

    def import_nzb(self, raw, name):
        self.calls.append((raw, name))
        if self.exc:
            raise self.exc
        return self.remote


class Remote:
    """The WebDAV reader: size, ranges, a full-read hash."""

    def __init__(self, data, fail_reads=0, second=None):
        self.data, self.fail_reads, self.second = data, fail_reads, second
        self.reads = []

    def size(self, remote):
        return len(self.data)

    def read_range(self, remote, offset, length):
        self.reads.append(offset)
        if self.fail_reads:
            self.fail_reads -= 1
            raise ConnectionError("down")
        return self.data[offset:offset + length]

    def sha256_full(self, remote):
        return self.second or hashlib.sha256(self.data).hexdigest()


class Crash(Exception):
    pass


@pytest.fixture
def e(tmp_path):
    env = Env(tmp_path)
    r = env.offload()
    assert r.state == "offloaded", r
    nzb = tmp_path / "restore.nzb"
    nzb.write_bytes(b"<nzb/>")
    st.upsert(env.db, {"id": ID, "nzb_path2": str(nzb)})
    env.new = os.urandom(41 * MIB + 777)  # a different release: other size, other bytes
    yield env
    env.db.close()


def restore(e, remote=None, importer=None, **kw):
    kw.setdefault("apply", True)
    kw.setdefault("keep_name", False)
    return ops.restore_from_path(e.db, ID, importer or Importer(), remote or Remote(e.new), **kw)


def snap(e):
    """Every non-directory entry (a directory's mtime moves with a temp file)."""
    return {p: v for p, v in tree(e.root).items() if not stat.S_ISDIR(v[0])}


def held(e):
    """Everything the old file needs that a restore must not touch."""
    return {p: v for p, v in tree(e.root).items()
            if p in (e.stub, e.manifest, e.chunks)}


def kinds(e):
    return [x["kind"] for x in st.events(e.db, ID)]


def test_happy_path_other_name_and_size(e):
    notified = []
    before = held(e)
    res = restore(e, notify=lambda row, p: notified.append((row["id"], p)) or {"arr": "ok"})
    assert res.ok and res.state == "failed" and res.reason == ops.RESTORED_NOTE
    new = e.root / "lib" / "Other.Release.2020.1080p.mkv"
    assert new.read_bytes() == e.new
    assert not os.path.lexists(e.lib)  # the symlink is gone
    assert not os.path.lexists(e.lib + ops.RESTORE_TMP_SUFFIX)
    assert stat.S_ISREG(os.lstat(new).st_mode) and os.lstat(new).st_nlink == 1
    row = st.get(e.db, ID)
    assert row["state"] == "failed" and row["restore_note"] == ops.RESTORED_NOTE
    assert row["step"] == 0
    assert held(e) == before  # stub, manifest and chunk list untouched
    assert notified == [(ID, os.path.join(os.path.dirname(e.lib), new.name))]
    assert kinds(e)[-3:] == ["restore_start", "restore_done", "restore_notify"]
    assert st.kv_get(e.db, f"restore:{ID}") is None


def test_keep_name_same_extension_replaces_l_atomically(e):
    res = restore(e, keep_name=True)
    assert res.ok
    assert os.path.basename(e.lib) == "Movie (2020).mkv"
    assert stat.S_ISREG(os.lstat(e.lib).st_mode) and e.read_lib() == e.new
    assert sorted(os.listdir(e.root / "lib")) == ["Movie (2020).mkv"]


def test_keep_name_takes_the_restore_extension(e):
    res = restore(e, keep_name=True, importer=Importer("/r/x/y.mp4"))
    assert res.ok
    assert sorted(os.listdir(e.root / "lib")) == ["Movie (2020).mp4"]


def test_open_tierfs_handle_keeps_reading_the_old_file(e):
    """A player holds the old stub; the restore never touches it, so it reads on."""
    fh = open(e.stub, "rb")
    head = fh.read(4096)
    assert head == e.data[:4096]
    assert restore(e).ok
    fh.seek(0)
    assert fh.read(4096) == head
    fh.close()
    assert os.path.exists(e.manifest) and os.path.exists(e.chunks)


def test_failed_row_with_a_stub_can_be_restored(e):
    st.set_state(e.db, ID, "failed", restore_note="bad_chunk at offset 4096")
    assert restore(e).ok
    assert st.get(e.db, ID)["restore_note"] == ops.RESTORED_NOTE


def test_import_failure_changes_nothing(e):
    before = snap(e)
    row = st.get(e.db, ID)
    res = restore(e, importer=Importer(exc=RuntimeError("boom")))
    assert not res.ok and "import failed" in res.reason
    assert snap(e) == before
    after = st.get(e.db, ID)
    assert after["state"] == "offloaded" and after["step"] == row["step"]
    assert "restore_failed" in kinds(e)
    assert os.path.islink(e.lib)


def test_copy_failure_keeps_l_and_resumes(e, monkeypatch):
    monkeypatch.setattr(ops, "RESTORE_SYNC_EVERY", MIB)
    monkeypatch.setattr(ops, "_sleep", lambda s: None)
    imp = Importer()
    bad = Remote(e.new)
    real = bad.read_range

    def flaky(remote, off, n):
        if off >= 8 * MIB:
            raise ConnectionError("down")
        return real(remote, off, n)
    bad.read_range = flaky
    res = restore(e, remote=bad, importer=imp, chunk=MIB)
    assert not res.ok and "remote read failed" in res.reason
    assert os.path.islink(e.lib) and st.get(e.db, ID)["state"] == "offloaded"
    tmp = e.lib + ops.RESTORE_TMP_SUFFIX
    assert os.path.exists(tmp)  # kept for the resume
    good = Remote(e.new)
    res = restore(e, remote=good, importer=imp, chunk=MIB)
    assert res.ok and len(imp.calls) == 1  # no second import
    assert min(good.reads) >= 7 * MIB  # it went on from the noted offset
    assert (e.root / "lib" / "Other.Release.2020.1080p.mkv").read_bytes() == e.new


def test_verify_failure_when_the_second_read_differs(e):
    before = snap(e)
    step0 = st.get(e.db, ID)["step"]
    res = restore(e, remote=Remote(e.new, second="0" * 64))
    assert not res.ok and "second read" in res.reason
    assert snap(e) == before  # the temp file is gone, L and the rest untouched
    assert st.get(e.db, ID)["step"] == step0 and st.get(e.db, ID)["state"] == "offloaded"
    assert st.kv_get(e.db, f"restore:{ID}") is None


def test_verify_failure_on_a_zero_filled_hole(e):
    holey = bytearray(e.new)
    holey[5 * MIB:6 * MIB] = bytes(MIB)
    holey = bytes(holey)
    before = snap(e)
    res = restore(e, remote=Remote(holey))
    assert not res.ok and "zero" in res.reason
    assert snap(e) == before


def test_target_name_taken_is_refused_before_any_copy(e):
    (e.root / "lib" / "Other.Release.2020.1080p.mkv").write_bytes(b"mine")
    before = snap(e)
    rd = Remote(e.new)
    res = restore(e, remote=rd)
    assert not res.ok and "already exists" in res.reason
    assert rd.reads == [] and snap(e) == before


def test_file_appeared_at_l_meanwhile_is_never_removed(e, monkeypatch):
    def point(name):
        if name == "restore.verified":
            os.unlink(e.lib)
            with open(e.lib, "wb") as f:
                f.write(b"arr upgrade")
    monkeypatch.setattr(ops, "_point", point)
    res = restore(e, keep_name=True)
    assert not res.ok and "no longer the tierfs symlink" in res.reason
    assert e.read_lib() == b"arr upgrade"
    assert not os.path.lexists(e.lib + ops.RESTORE_TMP_SUFFIX)


@pytest.mark.parametrize("crash_at", ["restore.imported", "restore.copied", "restore.verified",
                                      "restore.exchanged", "restore.swapped"])
def test_crash_then_resume_same_name(e, monkeypatch, crash_at):
    real = ops._point

    def point(name):
        if name == crash_at:
            raise Crash(name)
    monkeypatch.setattr(ops, "_point", point)
    imp = Importer()
    with pytest.raises(Crash):
        restore(e, keep_name=True, importer=imp)
    assert os.path.exists(e.stub) and os.path.exists(e.manifest)
    monkeypatch.setattr(ops, "_point", real)
    res = restore(e, keep_name=True, importer=imp)
    assert res.ok
    assert len(imp.calls) == 1
    assert e.read_lib() == e.new and stat.S_ISREG(os.lstat(e.lib).st_mode)
    assert sorted(os.listdir(e.root / "lib")) == ["Movie (2020).mkv"]
    assert st.get(e.db, ID)["restore_note"] == ops.RESTORED_NOTE


@pytest.mark.parametrize("crash_at", ["restore.verified", "restore.linked", "restore.swapped"])
def test_crash_then_resume_other_name(e, monkeypatch, crash_at):
    real = ops._point
    monkeypatch.setattr(ops, "_point", lambda n: (_ for _ in ()).throw(Crash(n)) if n == crash_at else None)
    with pytest.raises(Crash):
        restore(e)
    monkeypatch.setattr(ops, "_point", real)
    assert restore(e).ok
    assert sorted(os.listdir(e.root / "lib")) == ["Other.Release.2020.1080p.mkv"]
    assert (e.root / "lib" / "Other.Release.2020.1080p.mkv").read_bytes() == e.new


def test_refusals(e, monkeypatch):
    assert not ops.restore_from_path(e.db, "nope", Importer(), Remote(e.new), apply=True).ok
    res = restore(e, apply=False)
    assert res.ok and res.dry_run and os.path.islink(e.lib)
    monkeypatch.delenv("TIERING_APPLY", raising=False)
    res = restore(e)
    assert not res.ok and "TIERING_APPLY" in res.reason
    monkeypatch.setenv("TIERING_APPLY", "1")
    st.upsert(e.db, {"id": ID, "nzb_path2": None})
    res = restore(e)
    assert not res.ok and "no restore path" in res.reason
    st.upsert(e.db, {"id": ID, "nzb_path2": "/n/x.nzb", "state": "whole"})
    assert "cannot be restored" in restore(e).reason


def test_a_second_restore_of_the_same_id_is_busy(e):
    with ops._id_lock(e.db, f"{ID}.restore") as held_:
        assert held_
        res = restore(e)
    assert not res.ok and res.reason == ops._BUSY


def test_notify_error_never_fails_the_restore(e):
    def boom(row, p):
        raise RuntimeError("arr down")
    res = restore(e, notify=boom)
    assert res.ok
    ev = [x for x in st.events(e.db, ID) if x["kind"] == "restore_notify"][0]
    assert ev["detail"] == {"error": "RuntimeError"}


def test_events_carry_no_secret_shaped_text(e):
    restore(e)
    blob = json.dumps(st.events(e.db, ID))
    assert "apikey" not in blob.lower() and "http" not in blob.lower()
