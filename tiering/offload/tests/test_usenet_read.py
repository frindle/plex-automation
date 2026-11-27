"""A chunk AltMount keeps serving wrong is checked against the articles themselves.

Seen live: under concurrent range reads AltMount served the same wrong bytes for a chunk
on every quick reread, while the articles (CRC-checked) held L's bytes. That must not be
recorded as a bad copy, and a copy whose articles really differ must still be caught."""
import json
import os
import zlib

import pytest

from fakes_intake import FakeNntp, make_nzb
from tiering.offload import altmount as am
from tiering.offload import qualify as q
from tiering.offload import state as st
from tiering.offload import usenet_read as ur
from test_qualify import IDS, REL, World
from test_runner_contract import Rig

CH = 4096


def yenc(data: bytes, part: int, total: int, begin: int, size: int, line=64) -> list[bytes]:
    """One yEnc part as article body lines. '.' is not escaped, so lines that start
    with one are dot-stuffed on the wire."""
    out, cur = [], bytearray()
    for b in data:
        e = (b + 42) & 0xFF
        if e in (0, 10, 13, 61) or (e in (9, 32) and not cur):
            cur += bytes([61, (e + 64) & 0xFF])
        else:
            cur.append(e)
        if len(cur) >= line:
            out.append(bytes(cur))
            cur = bytearray()
    if cur:
        out.append(bytes(cur))
    return ([f"=ybegin part={part} total={total} line={line} size={size} name=a b.mkv".encode(),
             f"=ypart begin={begin} end={begin + len(data) - 1}".encode()] + out +
            [f"=yend size={len(data)} part={part} pcrc32={zlib.crc32(data):08x}".encode()])


def post(srv: FakeNntp, data: bytes, ids: list[str]) -> int:
    """Put ``data`` on ``srv`` as len(ids) parts. Returns the part size."""
    p = -(-len(data) // len(ids))
    for k, mid in enumerate(ids):
        srv.bodies[mid] = yenc(data[k * p:(k + 1) * p], k + 1, len(ids), k * p + 1, len(data))
    return p


def nzb_of(*files) -> bytes:
    """An NZB with several files: ``files`` are (name, ids, bytes per segment)."""
    body = "".join(
        f'<file subject="{n}"><segments>' +
        "".join(f'<segment bytes="{b}" number="{i + 1}">{m}</segment>' for i, m in enumerate(ids)) +
        "</segments></file>" for n, ids, b in files)
    return (f'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{body}'
            "</nzb>").encode()


@pytest.fixture
def srv():
    s = FakeNntp([])
    yield s
    s.close()


# ------------------------------------------------------------------ decoding

def test_ydecode_round_trips_every_byte_value():
    data = bytes(range(256)) * 40 + os.urandom(5000)
    info, got = ur.ydecode(yenc(data, 3, 9, 1001, 99999))
    assert got == data and info == {"size": 99999, "begin": 1001, "end": 1000 + len(data)}


def test_ydecode_wrong_crc_cannot_tell():
    lines = yenc(b"hello world" * 50, 1, 1, 1, 550)
    lines[2] = lines[2][:-1] + bytes([(lines[2][-1] + 1) & 0xFF or 1])
    with pytest.raises(ur.CannotTell):
        ur.ydecode(lines)


def test_ydecode_truncated_part_cannot_tell():
    with pytest.raises(ur.CannotTell):
        ur.ydecode(yenc(b"x" * 500, 1, 1, 1, 500)[:-1])


# ------------------------------------------------------------------ reading ranges

def test_ranges_across_parts_come_back_from_the_provider(srv):
    data = os.urandom(50_000)
    ids = [f"m{i}@t" for i in range(7)]
    p = post(srv, data, ids)
    srv.bodies["par@t"] = yenc(b"p" * 300, 1, 1, 1, 300)
    nzb = nzb_of(("x.par2", ["par@t"], 400), ("x.mkv", ids, 9000))
    u = ur.UsenetFile(nzb, srv.cfg(), len(data))
    try:
        for off, n in ((0, 10), (p - 5, 10), (3 * p, p), (len(data) - 7, 7), (0, len(data))):
            assert u.read(off, n) == data[off:off + n], (off, n)
    finally:
        u.close()
    assert srv.connections == 1  # one connection, kept open


def test_the_right_file_is_found_by_its_yenc_size(srv):
    data, other = os.urandom(20_000), os.urandom(30_000)
    a, b = [f"a{i}@t" for i in range(4)], [f"b{i}@t" for i in range(4)]
    post(srv, data, a)
    post(srv, other, b)
    nzb = nzb_of(("other.mkv", b, 9000), ("x.mkv", a, 5000))  # the bigger one is probed first
    u = ur.UsenetFile(nzb, srv.cfg(), len(data))
    try:
        assert u.read(1000, 9000) == data[1000:10_000]
    finally:
        u.close()


def test_a_packed_upload_cannot_tell_and_is_not_asked_again(srv):
    ids = [f"r{i}@t" for i in range(3)]
    post(srv, os.urandom(9000), ids)  # a volume, not the file itself
    u = ur.UsenetFile(nzb_of(("x.r00", ids, 3000)), srv.cfg(), 50_000)
    with pytest.raises(ur.CannotTell):
        u.read(0, 10)
    served = srv.bodies_served
    with pytest.raises(ur.CannotTell):
        u.read(100, 10)
    assert srv.bodies_served == served
    u.close()


def test_a_missing_article_cannot_tell(srv):
    data = os.urandom(10_000)
    ids = [f"m{i}@t" for i in range(4)]
    post(srv, data, ids)
    del srv.bodies[ids[2]]
    u = ur.UsenetFile(nzb_of(("x.mkv", ids, 3000)), srv.cfg(), len(data))
    try:
        assert u.read(0, 100) == data[:100]
        with pytest.raises(ur.CannotTell):
            u.read(5000, 100)  # in the third part
    finally:
        u.close()


def test_a_provider_that_refuses_cannot_tell(srv):
    srv.refuse_auth = True
    u = ur.UsenetFile(make_nzb(["a@t"]), srv.cfg(), 100)
    with pytest.raises(ur.CannotTell):
        u.read(0, 10)
    u.close()


# ------------------------------------------------------------------ compare_remote

class Reader:
    """A remote copy; ``wrong[off]`` is how many reads of that chunk come back zeros."""

    def __init__(self, data, wrong=None):
        self.data, self.wrong, self.calls = data, dict(wrong or {}), []

    def size(self, path):
        return len(self.data)

    def read_range(self, path, off, n):
        self.calls.append(off)
        if self.wrong.get(off, 0) > 0:
            self.wrong[off] -= 1
            return b"\0" * n
        return self.data[off:off + n]


@pytest.fixture
def lib(tmp_path):
    data = os.urandom(10 * CH + 123)
    p = tmp_path / "L.mkv"
    p.write_bytes(data)
    return str(p), data


def cmp(reader, path, usenet, **kw):
    kw.setdefault("workers", 1)
    kw.setdefault("fault_pauses", (30.0, 120.0))  # the default is longer
    naps = []
    r = q.compare_remote(reader, "/r.mkv", path, chunk=CH, sleep=naps.append, usenet=usenet, **kw)
    return r, naps


def test_sticky_wrong_bytes_the_upload_has_right_are_waited_out(lib):
    path, data = lib
    r, naps = cmp(Reader(data, {2 * CH: 4}), path, lambda o, n: data[o:o + n])
    assert r["result"] == "ok" and r["chunks"] == 11
    assert r["read_faults"] == 1 and r["fixed"] == 1
    assert naps[-2:] == [30.0, 120.0]  # right on the read after the long waits


def test_wrong_bytes_that_never_clear_are_a_read_fault_not_a_mismatch(lib):
    path, data = lib
    r, _ = cmp(Reader(data, {3 * CH: 99}), path, lambda o, n: data[o:o + n])
    assert r["result"] == "read_fault" and r["first_bad"] == 3 * CH and r["usenet"] == "same"
    assert r["chunks"] == 3


def test_an_upload_that_differs_is_a_mismatch_even_when_reads_were_unstable(lib):
    path, data = lib

    class Junk(Reader):
        def read_range(self, path, off, n):
            return os.urandom(n) if off == CH else self.data[off:off + n]
    r, naps = cmp(Junk(data), path, lambda o, n: b"\1" * n)
    assert r["result"] == "mismatch" and r["usenet"] == "differ" and r["first_bad"] == CH
    assert 30.0 not in naps  # no long waits for a copy that is not L


def test_cannot_tell_keeps_the_old_verdict(lib):
    path, data = lib

    def no(o, n):
        raise ur.CannotTell("packed")
    r, naps = cmp(Reader(data, {CH: 99}), path, no)
    assert r["result"] == "mismatch" and r["usenet"] is None and 30.0 not in naps


def test_a_broken_arbiter_is_never_a_verdict(lib):
    path, data = lib
    r, _ = cmp(Reader(data, {CH: 99}), path, lambda o, n: 1 / 0)
    assert r["result"] == "mismatch" and r["usenet"] is None


def test_only_a_few_chunks_are_waited_out(lib, monkeypatch):
    path, data = lib
    monkeypatch.setattr(q, "HASH_FAULT_MAX", 1)
    r, naps = cmp(Reader(data, {CH: 3, 4 * CH: 3}), path, lambda o, n: data[o:o + n])
    assert r["result"] == "read_fault" and r["first_bad"] == 4 * CH and r["read_faults"] == 1
    assert naps.count(30.0) == 1


def test_good_reads_never_ask_the_upload(lib):
    path, data = lib
    asked = []
    r, _ = cmp(Reader(data), path, lambda o, n: asked.append(o))
    assert r["result"] == "ok" and not asked


# ------------------------------------------------------------------ qualify and runner

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "HASH_CHUNK", 8192)
    monkeypatch.setattr(q, "HASH_REREAD_PAUSE", 0.0)
    monkeypatch.setattr(q, "HASH_FAULT_PAUSES", (0.0, 0.0, 0.0))
    w = World(tmp_path)
    yield w
    w.close()


def _zeros_at(monkeypatch, off, times):
    real = am.WebDavReader.read_range
    left = [times]

    def read_range(self, path, o, n):
        b = real(self, path, o, n)
        if o == off and left[0] > 0:
            left[0] -= 1
            return b"\0" * len(b)
        return b
    monkeypatch.setattr(am.WebDavReader, "read_range", read_range)


def test_qualify_read_fault_is_not_tonight_and_not_a_mismatch(world, monkeypatch):
    post(world.prim, world.content, IDS)
    _zeros_at(monkeypatch, 16384, 99)
    r = world.run()
    assert r.reasons == ["hash_unsure"]
    h = r.steps["hash"]
    assert h["kind"] == "read_fault" and h["first_bad"] == 16384 and h["usenet"] == "same"


def test_qualify_passes_when_the_fault_clears(world, monkeypatch):
    post(world.prim, world.content, IDS)
    _zeros_at(monkeypatch, 16384, 5)  # three quick reads and two late ones
    r = world.run()
    assert r.ok, r.steps
    assert r.steps["hash"]["read_faults"] == 1


def test_qualify_upload_that_is_not_l_is_a_mismatch(world, monkeypatch):
    bad = bytearray(world.content)
    bad[20_000:20_010] = b"\0" * 10
    post(world.prim, bytes(bad), IDS)
    world.dav.files[f"/movies/{REL}/{REL}.mkv"] = bytes(bad)
    r = world.run()
    assert r.reasons == ["hash_mismatch"]
    assert r.steps["hash"]["usenet"] == "differ" and r.steps["hash"]["first_bad"] == 16384


def test_runner_alerts_on_a_read_fault(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    try:
        row = {"id": "d" * 16, "state": "whole", "lib_path": str(tmp_path / "f.mkv"),
               "release_name": "X", "size": 5, "sha256": "x"}
        open(row["lib_path"], "w").close()
        st.upsert(rig.db, row)

        class Q:
            ok, reasons, fields = False, ["hash_unsure"], {}
            steps = {"hash": {"ok": False, "kind": "read_fault", "first_bad": 16384}}
        rig.deps.qualify = lambda r: Q
        rig.runner.disk[row["id"]] = row["lib_path"]
        rig.runner.durations[row["id"]] = 2 * 3600 * 1000
        rig.runner._demote_one(rig.db, row["id"], rig.clock())
        alerts = [json.loads(x) for x in open(rig.cfg.alerts)]
        assert [a["kind"] for a in alerts] == ["remote_read_fault"]
        assert alerts[0]["first_bad"] == 16384 and alerts[0]["id"] == row["id"]
        assert not [e for e in st.events(rig.db, row["id"]) if e["kind"] == "qualify_failed"]
    finally:
        rig.close()
