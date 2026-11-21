"""Fifth review of PR #82 (7704fe3): regressions the round-4 fixes could cause.

PASS confirms safe behaviour; FAIL demonstrates a defect. Fakes only: no real indexer,
provider or arr.
"""
import json
import os

import pytest

from fakes_intake import APIKEY, FakeArr, FakeNewznab
from test_qualify import REL, World
from test_review_fixes4 import (Clock, _kinds, _real, _store_alt, _try_search, _weekly)
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q
from tiering.offload import register as rg
from tiering.offload import state as st

DAY = 86400.0


def _c(srv, mono, wall, sp, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("grab_interval", 0)
    return nz.NewznabClient([nz.Indexer("one", srv.url, srv.key)], clock=mono, wall=wall,
                            sleep=lambda s: None, state_path=sp, **kw)


# =============================================================== persisted state

@pytest.mark.parametrize("body", [
    "{not json", "", "[]", "null", '{"spent": []}', '{"spent": {"search|one": ["x"]}}',
    '{"spent": {"search|one": 5}}', '{"dropped": {"one": "soon"}}', '{"errors": {"one": "x"}}',
    "\x00\x01garbage",
])
def test_P1_corrupt_state_file_is_ignored_and_rewritten(tmp_path, body):
    sp = tmp_path / "b.json"
    sp.write_text(body)
    s = FakeNewznab({})
    try:
        ix = _c(s, Clock(10.0), Clock(5e6), str(sp), search_budget=3)
        got = [_try_search(ix) for _ in range(4)]
    finally:
        s.close()
    assert got == ["ok"] * 3 + ["SearchBudgetSpent"]
    assert len(json.loads(sp.read_text())["spent"]["search|one"]) == 3


def test_P2_restart_inside_a_retry_after_window_stays_dropped(tmp_path):
    sp = str(tmp_path / "b.json")
    s = FakeNewznab({})
    wall = Clock(5e6)
    try:
        s.fail, s.fail_code, s.retry_after = True, 429, 3600
        assert _try_search(_c(s, Clock(10.0), wall, sp)) == "RateLimited"
        s.fail = False
        wall.t += 600  # a reboot: monotonic starts again near 0
        ix = _c(s, Clock(0.5), wall, sp)
        assert _try_search(ix) == "IndexerDropped" and len(s.queries) == 1
        wall.t += 3001
        ix = _c(s, Clock(0.7), wall, sp)
        assert _try_search(ix) == "ok" and len(s.queries) == 2
    finally:
        s.close()


def test_P3_wall_clock_set_back_at_restart_does_not_block_for_weeks(tmp_path):
    """The budget file is written with the right time; the next start has the wall clock
    30 days behind (RTC/NTP not synced yet, or a manual fix). The stored times now lie in
    the future, and a future time never leaves the rolling window."""
    sp = str(tmp_path / "b.json")
    s = FakeNewznab({})
    try:
        ix = _c(s, Clock(10.0), Clock(5e6), sp, search_budget=2)
        assert [_try_search(ix) for _ in range(2)] == ["ok", "ok"]
        mono, wall = Clock(1.0), Clock(5e6 - 30 * DAY)
        ix = _c(s, mono, wall, sp, search_budget=2)
        mono.t += DAY + 60  # a full window later on both clocks
        wall.t += DAY + 60
        got = _try_search(ix)
    finally:
        s.close()
    print("25 h after a restart with the wall clock 30 d behind:", got)
    assert got == "ok", "budget entries from the 'future' block the indexer until the clock catches up"


def test_P3b_wall_clock_set_back_keeps_a_drop_for_weeks(tmp_path):
    sp = str(tmp_path / "b.json")
    s = FakeNewznab({})
    try:
        s.fail, s.fail_code, s.retry_after = True, 429, 60
        assert _try_search(_c(s, Clock(10.0), Clock(5e6), sp)) == "RateLimited"
        s.fail = False
        mono, wall = Clock(1.0), Clock(5e6 - 30 * DAY)
        ix = _c(s, mono, wall, sp)
        mono.t += 3600
        wall.t += 3600
        got = _try_search(ix)
    finally:
        s.close()
    print("1 h after a 60 s Retry-After, wall clock 30 d behind at restart:", got)
    assert got == "ok"


def test_P4_two_clients_sharing_a_state_path_keep_one_budget(tmp_path):
    """e.g. the runner's qualify client and a weekly (or CLI) client on one budget file."""
    sp = str(tmp_path / "b.json")
    s = FakeNewznab({})
    try:
        a = _c(s, Clock(10.0), Clock(5e6), sp, search_budget=3)
        b = _c(s, Clock(10.0), Clock(5e6), sp, search_budget=3)
        got = [_try_search(a), _try_search(a), _try_search(b), _try_search(a), _try_search(b)]
        after = _c(s, Clock(11.0), Clock(5e6 + 1), sp, search_budget=3)
        more = [_try_search(after) for _ in range(3)]
    finally:
        s.close()
    print("a,a,b,a,b:", got, "then after a restart:", more, "requests:", len(s.queries))
    assert len(s.queries) <= 3, f"{len(s.queries)} searches against a shared budget of 3"


# =============================================================== nzb_unsure loops

def _qualify(w, ix):
    reader = am.WebDavReader(w.dav.url, "u", "p")
    importer = am.ImportClient(w.am.url, APIKEY, reader=reader, mount_path="/mnt/am",
                               poll_interval=0, sleep=lambda s: None)
    p, s = ns.Checker(w.prim.cfg()), ns.Checker(w.sec.cfg())
    try:
        return q.qualify(w.row, reader=reader, importer=importer, indexers=ix,
                         stat_primary=p, stat_secondary=s, archive_dir=w.archive)
    finally:
        p.close()
        s.close()


def test_U1_single_indexer_out_of_search_budget_is_nzb_unsure(tmp_path):
    """Contract (OFFLOAD_DESIGN 'Exact NZB: unsure is not a miss'): an indexer out of budget
    is 'try later'. With one indexer the search raises 'all indexers failed'."""
    w = World(tmp_path)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], search_budget=0,
                              min_interval=0, grab_interval=0)
        r = _qualify(w, ix)
    finally:
        w.close()
    print(r.reasons, r.steps.get("nzb"))
    assert r.reasons == ["nzb_unsure"], f"budget spent on the only indexer reads as {r.reasons}"


def test_U1b_single_indexer_dropped_is_nzb_unsure(tmp_path):
    w = World(tmp_path)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], min_interval=0,
                              grab_interval=0)
        ix._dropped_until["one"] = ix._clock() + 3600
        r = _qualify(w, ix)
    finally:
        w.close()
    print(r.reasons)
    assert r.reasons == ["nzb_unsure"]


def test_U2_a_removed_release_is_unsure_every_night_forever(tmp_path):
    """The exact release is still listed but its NZB is gone (the grab answers 404 /
    'no such item'). Nothing ever turns this into a verdict: each night costs a search and
    a grab, and the row is retried for ever."""
    sp = str(tmp_path / "b.json")
    mono, wall = Clock(10.0), Clock(5e6)
    out = []
    for night in range(5):
        d = tmp_path / f"n{night}"
        d.mkdir()
        w = World(d, alt_title=None)
        w.nz.releases["exact"]["nzb"] = b'<?xml version="1.0"?><error code="300" description="No such item"/>'
        try:
            ix = _c(w.nz, mono, wall, sp)
            r = _qualify(w, ix)
            out.append((r.reasons[0], len(w.nz.queries), len(w.nz.gets)))
        finally:
            w.close()
        mono.t += DAY + 1
        wall.t += DAY + 1
    print(out)
    assert any(x[0] != "nzb_unsure" for x in out), "a release whose NZB is gone stays 'try later' for ever"


def test_U3_one_row_with_dead_dupes_drops_the_indexer_for_everyone(tmp_path):
    """Three listings of the exact name, all answering a non-NZB page, drop the only
    indexer for 24 h; every other row that night is then unsure (or an error)."""
    w = World(tmp_path, alt_title=None)
    page = b"<html><body>This NZB was removed</body></html>"
    rels = {f"d{i}": {"title": REL, "nzb": page} for i in range(3)}
    w.nz.releases.clear()
    w.nz.releases.update(rels)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], min_interval=0,
                              grab_interval=0)
        r1 = _qualify(w, ix)
        w.nz.releases["exact"] = {"title": REL, "nzb": w.nzb}  # a healthy row's release
        r2 = _qualify(w, ix)
    finally:
        w.close()
    print("row A:", r1.reasons, "row B:", r2.reasons, "dropped:", ix._dropped_until)
    assert r2.ok or "one" not in ix._dropped_until, \
        "one row's dead listings dropped the indexer for every other row"


# =============================================================== disk-path rewrite

MAP = [("/mnt/user/media", "/mnt/disk1/media"), ("/mnt/user/media", "/mnt/disk2/media")]


class _St:
    st_size, st_mtime_ns = 100, 1


@pytest.fixture
def arrs():
    made = []

    def make(paths):
        a = FakeArr(movies=[{"title": f"T{i}", "year": 2020,
                             "movieFile": {"path": p, "size": 1}} for i, p in enumerate(paths)])
        made.append(a)
        return rg.ArrClient(a.url, a.key)

    yield make
    for a in made:
        a.close()


def _reg(tmp_path, clients, disk_map=MAP, resolve=None):
    db = st.open_db(tmp_path / "t.db")
    asked = []

    def res(p):
        asked.append(p)
        return [p.replace("/mnt/user/", "/mnt/disk1/", 1)] if resolve is None else resolve(p)

    out = rg.register(db, True, state=st, radarrs=clients, sonarrs=[], arr_map="",
                      resolve=res, stat=lambda p: _St, disk_map=disk_map)
    return out, sorted(r["lib_path"] for r in st.list_files(db)), asked


@pytest.mark.parametrize("path,want", [
    ("/mnt/disk1/media/m.mkv", "/mnt/user/media/m.mkv"),
    ("/mnt/disk2/media/a/b.mkv", "/mnt/user/media/a/b.mkv"),
    ("/mnt/disk10/media/m.mkv", None),      # disk1 must not match disk10
    ("/mnt/disk1/mediaX/m.mkv", None),      # nor media match mediaX
    ("/mnt/disk1/media/../../disk10/media/m.mkv", None),
    ("/mnt/cache/media/m.mkv", None),
    ("/mnt/cache_nvme/media/m.mkv", None),
    ("/mnt/disks/ud1/m.mkv", None),
])
def test_D1_rewrite_only_at_a_path_boundary(tmp_path, arrs, path, want):
    out, libs, _ = _reg(tmp_path, [arrs([path])])
    print(path, "->", libs, out["disk_path"], out["disk_path_mapped"])
    assert libs == ([want] if want else [])
    assert (out["disk_path"], out["disk_path_mapped"]) == ((0, 1) if want else (1, 0))


@pytest.mark.parametrize("path", [
    "/mnt/nvme/media/m.mkv",            # a named pool (unraid 6.9+), not called cache*
    "/mnt/disks/usb/media/m.mkv",       # unassigned devices
    "/mnt/remotes/nas/media/m.mkv",
])
def test_D2_other_unraid_mounts_never_become_lib_path(tmp_path, arrs, path):
    """With TIERING_PATH_MAP unset (resolve = the path itself when it exists)."""
    out, libs, _ = _reg(tmp_path, [arrs([path])], disk_map=[], resolve=lambda p: [p])
    print(path, "->", libs, out)
    assert libs == [], f"{path} stored as lib_path"
    assert out["disk_path"] == 1


def test_D2b_user0_is_rewritten_to_the_user_share(tmp_path, arrs):
    out, libs, _ = _reg(tmp_path, [arrs(["/mnt/user0/media/m.mkv"])], disk_map=[],
                        resolve=lambda p: [p])
    assert libs == ["/mnt/user/media/m.mkv"] and out["disk_path"] == 0


def test_D3_mapped_and_native_listing_of_one_file_is_one_row(tmp_path, arrs):
    a = arrs(["/mnt/disk1/media/m.mkv"])
    b = arrs(["/mnt/user/media/m.mkv"])
    out, libs, _ = _reg(tmp_path, [a, b])
    print(out, libs)
    assert libs == ["/mnt/user/media/m.mkv"] and out["listed"] == 1 and out["new"] == 1


def test_D3b_existing_share_row_then_arr_reports_the_disk_path(tmp_path, arrs):
    db = st.open_db(tmp_path / "t.db")
    common = dict(state=st, sonarrs=[], arr_map="", stat=lambda p: _St, disk_map=MAP,
                  resolve=lambda p: [p.replace("/mnt/user/", "/mnt/disk1/", 1)])
    rg.register(db, True, radarrs=[arrs(["/mnt/user/media/m.mkv"])], **common)
    out = rg.register(db, True, radarrs=[arrs(["/mnt/disk1/media/m.mkv"])], **common)
    libs = [r["lib_path"] for r in st.list_files(db)]
    print(out, libs)
    assert libs == ["/mnt/user/media/m.mkv"] and out["unchanged"] == 1


def test_D4_unnormalised_share_path_is_not_a_second_row(tmp_path, arrs):
    a = arrs(["/mnt/user/media/m.mkv"])
    b = arrs(["/mnt/user/media//m.mkv"])
    out, libs, _ = _reg(tmp_path, [a, b])
    print(out, libs)
    assert len(libs) == 1, f"one file, two rows: {libs}"


def test_D5_symlink_to_a_disk_is_not_stored_when_the_map_is_set(tmp_path, arrs, monkeypatch):
    share, d1 = tmp_path / "user" / "movies", tmp_path / "disk1" / "movies"
    share.mkdir(parents=True)
    d1.mkdir(parents=True)
    (d1 / "m.mkv").write_bytes(b"x" * 10)
    (tmp_path / "link").symlink_to(d1)
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1}")
    monkeypatch.delenv("TIERING_ARR_PATH_MAP", raising=False)
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[arrs([f"{tmp_path}/link/m.mkv"])], sonarrs=[])
    libs = [r["lib_path"] for r in st.list_files(db)]
    print(out, libs)
    assert libs == [] or libs == [f"{share}/m.mkv"]


# =============================================================== weekly unsure + dead

@pytest.mark.parametrize("how", ["missing", "gone", "same_upload"])
def test_W6_dead_restore_nzb_with_unsure_search_never_flags(tmp_path, monkeypatch, how):
    monkeypatch.setenv("NEWZNAB_SEARCH_BUDGET", "0")  # the search cannot complete
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    before = st.get(db, rid)
    if how == "missing":
        e.prim.ids.discard("alt-Alpha@t")
    elif how == "gone":
        os.unlink(n2)
    else:
        st.update_if(db, rid, "offloaded", nzb_path2=before["nzb_path"])
    seen = []
    try:
        flagged = _weekly(e, db, on_promote=seen.append)
    finally:
        e.close()
    row = st.get(db, rid)
    ev = st.events(db, rid)
    print(how, [(x["kind"], x["detail"]) for x in ev], row["restore_score"], row["restore_note"])
    assert flagged == [] and seen == [] and "weekly_flag" not in _kinds(db, rid)
    assert row["state"] == "offloaded" and row["restore_score"] == before["restore_score"]
    assert ("restore_nzb_lost", {"unsure": True}) in [(x["kind"], x["detail"]) for x in ev]
    assert row["nzb_path2"] is not None  # kept until a complete search says otherwise
