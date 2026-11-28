"""The twin: a second upload of the same bytes, found at qualify, that a promote reads
when the first copy fails, checked every week."""

import os
import sqlite3
import threading

import pytest

from fakes_intake import make_nzb
from tiering.offload import ops
from tiering.offload import qualify as q
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Env, FakeReader
from tiering.offload.tests.test_qualify import REL, World
from tiering.offload.tests.test_weekly import Env as WeeklyEnv

PRIMARY, TWIN = "/r/Movie.mkv", "/r2/Movie.mkv"
CS = 4 * MIB


class Copies(FakeReader):
    """The upload at PRIMARY and its twin at TWIN, both env.data unless told otherwise."""

    def __init__(self, data, dead=(), corrupt=None, hold=None):
        super().__init__(data)
        self.dead = set(dead)  # copies that answer with errors
        self.corrupt = dict(corrupt or {})  # copy -> offset of a flipped byte
        self.hold = dict(hold or {})  # (copy, offset) -> an Event the read waits on
        self.by = []
        self.lock = threading.Lock()

    def read_range(self, remote_path, offset, length):
        with self.lock:
            self.by.append((remote_path, offset))
        ev = self.hold.get((remote_path, offset))
        if ev is not None:
            ev.wait(10)
        if remote_path in self.dead:
            raise ConnectionError("upload gone")
        b = bytearray(self.data[offset:offset + length])
        c = self.corrupt.get(remote_path)
        if c is not None and offset <= c < offset + length:
            b[c - offset] ^= 0xFF
        return bytes(b)

    def asked(self, path):
        return [o for p, o in self.by if p == path]


def twin_env(tmp_path, size=50 * MIB):
    env = Env(tmp_path, size=size)
    assert env.offload().ok
    st.set_state(env.db, ID, "offloaded", twin_remote_path=TWIN)
    return env, st.get(env.db, ID)["head_len"]


# ---- the promote

def test_a_dead_upload_is_promoted_from_the_twin(tmp_path):
    env, head = twin_env(tmp_path)
    reader = Copies(env.data, dead={PRIMARY})
    r = env.promote(reader)
    assert r.ok and r.state == "whole"
    assert env.read_lib() == env.data
    # the first chunk found the upload dead; every chunk after it went to the twin first
    assert reader.asked(PRIMARY) == [head]
    assert reader.asked(TWIN) == [head, head + CS]


def test_bad_bytes_on_the_upload_are_read_from_the_twin(tmp_path):
    env, head = twin_env(tmp_path)
    reader = Copies(env.data, corrupt={PRIMARY: head + CS + 9})
    r = env.promote(reader)
    assert r.ok and r.state == "whole"
    assert env.read_lib() == env.data
    assert reader.asked(PRIMARY) == [head, head + CS, head + CS]  # a mismatch is asked twice
    assert reader.asked(TWIN) == [head + CS]


def test_both_copies_dead_stays_resumable(tmp_path):
    env, head = twin_env(tmp_path)
    r = env.promote(Copies(env.data, dead={PRIMARY, TWIN}))
    assert not r.ok and r.state == "promoting"
    assert env.promote().ok  # the remote is back


def test_both_copies_bad_at_one_offset_fails_the_row(tmp_path):
    env, head = twin_env(tmp_path)
    bad = head + 3
    r = env.promote(Copies(env.data, corrupt={PRIMARY: bad, TWIN: bad}))
    assert r.state == "failed"
    assert st.get(env.db, ID)["state"] == "failed"


def test_a_slow_chunk_is_asked_of_the_twin_while_someone_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("TIERING_PROMOTE_HEDGE_S", "0.3")
    env, head = twin_env(tmp_path, size=300 * MIB)
    slow = head + 3 * CS
    stuck = threading.Event()
    reader = Copies(env.data, hold={(PRIMARY, slow): stuck})
    try:
        r = env.promote(reader, urgent=lambda: True)
        assert r.ok and r.state == "whole"
        assert not stuck.is_set()  # finished without the stuck read
        assert slow in reader.asked(TWIN)
        # a hedge, not a switch: every chunk the twin was asked for was asked of the upload
        # first (a busy machine may make another chunk slow enough too)
        assert set(reader.asked(TWIN)) <= set(reader.asked(PRIMARY))
        assert env.read_lib() == env.data
    finally:
        stuck.set()


def test_nobody_watching_means_no_hedge(tmp_path, monkeypatch):
    monkeypatch.setenv("TIERING_PROMOTE_HEDGE_S", "0.05")
    env, head = twin_env(tmp_path)
    late = threading.Event()
    threading.Timer(0.4, late.set).start()
    reader = Copies(env.data, hold={(PRIMARY, head): late})
    r = env.promote(reader, urgent=lambda: False)
    assert r.ok and reader.asked(TWIN) == []


def test_without_a_twin_a_slow_chunk_just_waits(tmp_path, monkeypatch):
    monkeypatch.setenv("TIERING_PROMOTE_HEDGE_S", "0.05")
    env = Env(tmp_path)
    assert env.offload().ok
    head = st.get(env.db, ID)["head_len"]
    late = threading.Event()
    threading.Timer(0.3, late.set).start()
    reader = Copies(env.data, hold={(PRIMARY, head): late})
    r = env.promote(reader, urgent=lambda: True)
    assert r.ok and reader.asked(PRIMARY).count(head) == 1


def test_the_switch_waits_switch_s_on_the_upload_before_the_twin(tmp_path, monkeypatch):
    """With a twin behind it the upload gets TIERING_PROMOTE_SWITCH_S of retries, not
    the whole retry budget."""
    monkeypatch.setenv("TIERING_PROMOTE_RETRY_S", "600")
    monkeypatch.setenv("TIERING_PROMOTE_SWITCH_S", "3")
    clock, slept = [0.0], []
    monkeypatch.setattr(ops, "_mono", lambda: clock[0])

    def sleep(d):
        slept.append(d)
        clock[0] += d

    monkeypatch.setattr(ops, "_sleep", sleep)
    env, head = twin_env(tmp_path)
    reader = Copies(env.data, dead={PRIMARY})
    assert env.promote(reader).ok
    assert sum(slept) <= 3 and reader.asked(PRIMARY).count(head) >= 2


def test_sources():
    s = ops._Sources(["/a", None, "/b", "/a", ""])
    assert len(s) == 2 and s.order() == ["/a", "/b"] and s.order(True) == ["/b", "/a"]
    assert s.demote("/a") and s.order() == ["/b", "/a"]
    assert not s.demote("/a")  # already last
    assert not ops._Sources(["/a"]).demote("/a")


# ---- qualify

def twin_world(tmp_path, content=None, live=True, same_ids=False):
    w = World(tmp_path)
    ids = [f"s{i}@t" for i in range(40)] if same_ids else [f"tw{i}@t" for i in range(8)]
    nzb = make_nzb(ids, name="twin", seg_bytes=len(w.content) * 21 // 20 // len(ids) + 1)
    w.nz.releases["twin"] = {"title": REL, "nzb": nzb}
    w.am.storage_by_nzb[nzb] = f"/mnt/am/movies/{REL}-twin"
    w.dav.files[f"/movies/{REL}-twin/{REL}.mkv"] = w.content if content is None else content
    if live:
        w.prim.ids.update(ids)
        w.sec.ids.update(ids)
    return w


@pytest.fixture
def tw(tmp_path):
    made = []

    def make(**kw):
        made.append(twin_world(tmp_path, **kw))
        return made[-1]

    yield make
    for w in made:
        w.close()


def test_qualify_keeps_a_twin_with_the_same_bytes(tw):
    w = tw()
    r = w.run()
    assert r.ok and r.steps["twin"] == {"ok": True, "found": True}
    f = r.fields
    assert f["twin_remote_path"] == f"/movies/{REL}-twin/{REL}.mkv"
    assert os.path.exists(f["twin_nzb_path"]) and f["twin_nzb_path"] != f["nzb_path"]
    assert len(w.am.uploads) == 2
    assert w.nz.gets.count("exact") == 1  # our own listing is not grabbed again


def test_qualify_drops_a_twin_with_other_bytes(tw):
    w = tw()
    w.dav.files[f"/movies/{REL}-twin/{REL}.mkv"] = bytes([w.content[0] ^ 1]) + w.content[1:]
    r = w.run()
    assert r.ok and r.fields["twin_remote_path"] is None and r.fields["twin_nzb_path"] is None


def test_qualify_drops_a_twin_of_another_size(tw):
    w = tw()
    w.dav.files[f"/movies/{REL}-twin/{REL}.mkv"] = w.content + b"x"
    r = w.run()
    assert r.ok and r.fields["twin_remote_path"] is None


def test_qualify_never_imports_a_twin_with_missing_articles(tw):
    w = tw(live=False)
    r = w.run()
    assert r.ok and r.fields["twin_remote_path"] is None
    assert len(w.am.uploads) == 1  # only our own upload


def test_the_same_upload_listed_twice_is_no_twin(tw):
    w = tw(same_ids=True)
    r = w.run()
    assert r.ok and r.fields["twin_remote_path"] is None
    assert len(w.am.uploads) == 1


def test_same_samples_covers_both_ends(tmp_path):
    data = bytearray(os.urandom(5 * MIB))
    lib = tmp_path / "f"
    lib.write_bytes(bytes(data))

    class R:
        def __init__(self, b):
            self.b = b

        def read_range(self, p, off, n):
            return bytes(self.b[off:off + n])

    assert q._same_samples(R(bytes(data)), "/x", str(lib), len(data))
    for at in (0, len(data) - 1):
        other = bytearray(data)
        other[at] ^= 1
        assert not q._same_samples(R(bytes(other)), "/x", str(lib), len(data))


# ---- weekly

def test_weekly_keeps_a_live_twin_and_drops_a_dead_one(tmp_path):
    from tiering.offload import newznab as nz
    e = WeeklyEnv(tmp_path, ["Alpha", "Bravo"])
    try:
        arch = str(tmp_path / "arch")
        live = [f"twA{i}@t" for i in range(4)]
        dead = [f"twB{i}@t" for i in range(4)]
        e.prim.ids.update(live + dead)
        e.sec.ids.update(live + dead[:3])  # one article of Bravo's twin is gone
        rows = e.state.rows
        a, b = f"{0:016x}", f"{1:016x}"
        rows[a].update(twin_nzb_path=nz.archive_nzb(make_nzb(live), arch, "Alpha twin"),
                       twin_remote_path="/a2.mkv")
        rows[b].update(twin_nzb_path=nz.archive_nzb(make_nzb(dead), arch, "Bravo twin"),
                       twin_remote_path="/b2.mkv")
        promote = e.run()
        assert promote == []  # a lost twin alone promotes nothing
        assert rows[a]["twin_remote_path"] == "/a2.mkv"
        assert rows[b]["twin_remote_path"] is None and rows[b]["twin_nzb_path"] is None
        assert (b, "twin_lost", {}) in e.state.events
        assert "Files with a twin (a second upload of the same bytes): 1; twins lost this run: 1." in e.text()
    finally:
        e.close()


def test_weekly_drops_a_twin_whose_nzb_file_is_gone(tmp_path):
    e = WeeklyEnv(tmp_path, ["Alpha"])
    try:
        row = e.state.rows[f"{0:016x}"]
        row.update(twin_nzb_path=str(tmp_path / "nope.nzb.gz"), twin_remote_path="/a2.mkv")
        e.run()
        assert row["twin_remote_path"] is None
    finally:
        e.close()


# ---- state

def test_an_older_database_gets_the_twin_columns(tmp_path):
    p = tmp_path / "old.db"
    old = st._SCHEMA.replace("    twin_nzb_path TEXT,\n", "").replace("    twin_remote_path TEXT,\n", "")
    db = sqlite3.connect(p)
    db.executescript(old)
    db.close()
    db = st.open_db(p)
    cols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
    assert {"twin_nzb_path", "twin_remote_path"} <= cols
