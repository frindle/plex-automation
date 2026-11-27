"""qualify step 3 compares the remote copy with L chunk by chunk, and reads a chunk
that differs again before it counts: AltMount zero-fills a segment it could not fetch
in time and still answers 206, so one wrong read is not a bad copy."""
import os

import pytest

from fakes_intake import sha
from tiering.offload import altmount as am
from tiering.offload import qualify as q
from tiering.offload import state as st
from test_qualify import REL, World
from test_runner_contract import Rig
from test_weekly import rotation_env, DAY3

CH = 4096


class Reader:
    """A remote copy whose reads can be scripted per chunk offset."""

    def __init__(self, data, script=None):
        self.data, self.script, self.calls = data, dict(script or {}), []

    def size(self, path):
        return len(self.data)

    def read_range(self, path, off, n):
        self.calls.append(off)
        plan = self.script.get(off)
        if plan:
            step = plan.pop(0)
            if step == "err":
                raise am.AltMountError("read failed: timeout")
            if step == "zero":
                return b"\0" * n
            if step == "junk":
                return os.urandom(n)
        return self.data[off:off + n]


@pytest.fixture
def lib(tmp_path):
    data = os.urandom(10 * CH + 123)
    p = tmp_path / "L.mkv"
    p.write_bytes(data)
    return str(p), data


def cmp(reader, path, **kw):
    kw.setdefault("workers", 3)
    return q.compare_remote(reader, "/r.mkv", path, chunk=CH, **kw)


def test_all_chunks_match(lib):
    path, data = lib
    r = cmp(Reader(data), path)
    assert r["result"] == "ok" and r["chunks"] == 11 and r["rereads"] == 0 and r["fixed"] == 0


def test_a_zero_filled_read_that_comes_right_is_a_fluke(lib):
    path, data = lib
    rd = Reader(data, {3 * CH: ["zero"], 7 * CH: ["junk", "err"]})
    r = cmp(rd, path)
    assert r["result"] == "ok" and r["chunks"] == 11
    assert r["fixed"] == 2 and r["rereads"] == 3
    assert rd.calls.count(3 * CH) == 2 and rd.calls.count(7 * CH) == 3


def test_the_same_zeros_every_time_is_a_mismatch(lib):
    path, data = lib
    rd = Reader(data, {5 * CH: ["zero"] * 3})
    r = cmp(rd, path)
    assert r["result"] == "mismatch" and r["first_bad"] == 5 * CH and r["zero_filled"]
    assert rd.calls.count(5 * CH) == 3  # the first read and two rereads


def test_a_remote_copy_that_is_other_bytes_is_a_mismatch_not_zero_filled(tmp_path, lib):
    path, data = lib
    other = bytearray(data)
    other[2 * CH + 10] ^= 0xFF
    r = cmp(Reader(bytes(other)), path)
    assert r["result"] == "mismatch" and r["first_bad"] == 2 * CH and not r["zero_filled"]
    assert r["chunks"] == 2  # the chunks before it matched


def test_different_wrong_bytes_every_time_is_unstable(lib):
    path, data = lib
    r = cmp(Reader(data, {CH: ["junk"] * 3}), path)
    assert r["result"] == "unstable" and r["first_bad"] == CH


def test_one_wrong_read_among_errors_is_unstable_not_a_mismatch(lib):
    path, data = lib
    r = cmp(Reader(data, {CH: ["zero", "err", "err"]}), path)
    assert r["result"] == "unstable"


def test_read_errors_that_outlast_the_rereads_raise(lib):
    path, data = lib
    with pytest.raises(am.AltMountError):
        cmp(Reader(data, {4 * CH: ["err"] * 3}), path)


def test_no_rereads_means_one_wrong_read_is_a_mismatch(lib):
    path, data = lib
    r = cmp(Reader(data, {0: ["zero"]}), path, rereads=0)
    assert r["result"] == "mismatch" and r["first_bad"] == 0


def test_a_size_that_differs_is_not_read(lib):
    path, data = lib
    rd = Reader(data + b"x")
    r = cmp(rd, path)
    assert r["result"] == "size" and rd.calls == []


def test_the_first_bad_chunk_ends_the_compare(lib):
    path, data = lib
    rd = Reader(data, {0: ["zero"] * 3})
    r = cmp(rd, path, workers=1)
    assert r["result"] == "mismatch" and r["first_bad"] == 0
    assert rd.calls.count(0) == 3 and r["chunks"] == 0  # no chunk after it is counted


def test_empty_file(tmp_path):
    p = tmp_path / "e"
    p.write_bytes(b"")
    assert cmp(Reader(b""), str(p))["result"] == "ok"


# ------------------------------------------------------------------ through qualify

@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _flaky_first_read(monkeypatch, how):
    real = am.WebDavReader.read_range
    seen = []

    def read_range(self, path, off, n):
        b = real(self, path, off, n)
        seen.append(off)
        if len(seen) == 1:
            return how(b)
        return b
    monkeypatch.setattr(am.WebDavReader, "read_range", read_range)
    return seen


def test_qualify_passes_after_a_zero_filled_fluke(world, monkeypatch):
    seen = _flaky_first_read(monkeypatch, lambda b: b"\0" * len(b))
    r = world.run()
    assert r.ok, r.steps
    assert r.steps["hash"]["fixed"] == 1 and r.steps["hash"]["rereads"] == 1
    assert r.fields["sha256"] == sha(world.content)
    assert len(seen) >= 2


def test_qualify_mismatch_carries_the_detail(world):
    bad = bytearray(world.content)
    bad[20_000:30_000] = b"\0" * 10_000
    world.dav.files[f"/movies/{REL}/{REL}.mkv"] = bytes(bad)
    monkey = q.HASH_CHUNK
    try:
        q.HASH_CHUNK = 8192
        r = world.run()
    finally:
        q.HASH_CHUNK = monkey
    assert not r.ok and r.reasons == ["hash_mismatch"]
    h = r.steps["hash"]
    assert h["kind"] == "mismatch" and h["first_bad"] == 16384 and h["chunks"] == 2


def test_qualify_unstable_reads_are_not_tonight(world, monkeypatch):
    real = am.WebDavReader.read_range
    monkeypatch.setattr(am.WebDavReader, "read_range",
                        lambda self, p, o, n: os.urandom(len(real(self, p, o, n))))
    r = world.run()
    assert r.reasons == ["hash_unsure"]  # the runner's "_unsure": nothing is recorded
    assert world.sec.stats == 0


def test_qualify_size_mismatch(world):
    world.dav.files[f"/movies/{REL}/{REL}.mkv"] = world.content + b"x"
    r = world.run()
    assert r.reasons == ["hash_mismatch"] and r.steps["hash"]["kind"] == "size"


def test_qualify_failed_event_carries_the_failed_step(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    try:
        row = {"id": "c" * 16, "state": "whole", "lib_path": str(tmp_path / "f.mkv"),
               "release_name": "X", "size": 5, "sha256": "x"}
        open(row["lib_path"], "w").close()
        st.upsert(rig.db, row)

        class Q:
            ok, reasons, fields = False, ["hash_mismatch"], {}
            steps = {"nzb": {"ok": True}, "hash": {"ok": False, "first_bad": 16384}}
        rig.deps.qualify = lambda r: Q
        rig.runner.disk[row["id"]] = row["lib_path"]
        rig.runner.durations[row["id"]] = 2 * 3600 * 1000
        rig.runner._demote_one(rig.db, row["id"], rig.clock())
        ev = [e for e in st.events(rig.db, row["id"]) if e["kind"] == "qualify_failed"]
        assert ev
        import json
        d = json.loads(ev[0]["detail"]) if isinstance(ev[0]["detail"], str) else ev[0]["detail"]
        assert d["steps"] == {"hash": {"ok": False, "first_bad": 16384}}
    finally:
        rig.close()


# ------------------------------------------------------------------ weekly full read

class TwoReads:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def sha256_full(self, path):
        self.calls.append(path)
        a = self.answers.get(path)
        if a:
            v = a.pop(0) if len(a) > 1 else a[0]
            if v == "err":
                raise am.AltMountError("boom")
            return v
        raise am.AltMountError("boom")


def _weekly(tmp_path, first, second):
    e = rotation_env(tmp_path, 2)
    row = e.state.rows[f"{1:016x}"]
    good = row["sha256"]
    seq = [good if x == "good" else x for x in (first, second)]
    rd = TwoReads({row["remote_path"]: seq})
    return e, row, rd


def test_weekly_fluke_is_not_failed(tmp_path):
    e, row, rd = _weekly(tmp_path, "0" * 64, "good")
    try:
        e.run(reader=rd, now=DAY3)
        assert row["state"] == "offloaded"
        kinds = [k for _, k, _ in e.state.events]
        assert "full_read_fluke" in kinds and "full_read_mismatch" not in kinds
        assert rd.calls.count(row["remote_path"]) == 2
    finally:
        e.close()


@pytest.mark.parametrize("second", ["1" * 64, "err"])
def test_weekly_unstable_is_not_failed(tmp_path, second):
    e, row, rd = _weekly(tmp_path, "0" * 64, second)
    try:
        e.run(reader=rd, now=DAY3)
        assert row["state"] == "offloaded"
        kinds = [k for _, k, _ in e.state.events]
        assert "full_read_unstable" in kinds and "full_read_mismatch" not in kinds
    finally:
        e.close()


def test_weekly_same_wrong_hash_twice_is_failed(tmp_path):
    e, row, rd = _weekly(tmp_path, "0" * 64, "0" * 64)
    try:
        e.run(reader=rd, now=DAY3)
        assert row["state"] == "failed"
        assert "full_read_mismatch" in [k for _, k, _ in e.state.events]
    finally:
        e.close()
