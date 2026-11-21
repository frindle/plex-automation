"""Regression tests for the fourth review of PR #82: weekly loss handling, indexer
etiquette (Retry-After, error pages, budgets across restarts), unsure vs "no exact NZB",
register disk paths, and keys in exception chains. Fakes only."""
import email.utils
import logging
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fakes_intake import APIKEY, FakeArr, FakeNewznab, make_nzb
from test_qualify import World
from test_weekly import DAY15, Env
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q
from tiering.offload import register as rg
from tiering.offload import state as st
from tiering.offload import weekly


def _real(tmp_path, names, **kw):
    e = Env(tmp_path, names, **kw)
    db = st.open_db(tmp_path / "t.db")
    for r in e.state.rows.values():
        st.upsert(db, dict(r))
    return e, db


class Wrap:
    """A checker that raises ``exc`` for one path, ``times`` times, then delegates."""

    def __init__(self, inner, bad_path=None, exc=None, times=1):
        self.inner, self.bad, self.exc, self.left = inner, bad_path, exc, times

    def __call__(self, path):
        if self.bad is not None and path == self.bad and self.left > 0:
            self.left -= 1
            raise self.exc
        return self.inner(path)

    def close(self):
        self.inner.close()


def _weekly(e, db, wrap_p=None, **kw):
    p, s = e.checkers()
    if wrap_p is not None:
        p = wrap_p(p)
    try:
        return weekly.run_weekly(db, stat_primary=p, stat_secondary=s, indexers=e.indexers,
                                 report_path=e.report, state=st, now=kw.pop("now", DAY15),
                                 monthly=False, **kw)
    finally:
        p.close()
        s.close()


def _store_alt(tmp_path, db, title):
    row = [r for r in st.list_files(db) if r["title"] == title][0]
    n2 = nz.archive_nzb(make_nzb([f"alt-{title}@t"]), str(tmp_path / "arch"),
                        f"{title}.2020.1080p.WEB-DL-ALT")
    assert st.update_if(db, row["id"], "offloaded", nzb_path2=n2)
    return row["id"], n2


def _kinds(db, rid):
    return [ev["kind"] for ev in st.events(db, rid)]


# =============================================================== weekly loss handling

def test_W1_transient_network_error_on_restore_stat_keeps_nzb_path2(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    try:
        flagged = _weekly(e, db, wrap_p=lambda p: Wrap(p, n2, ns.NntpUnavailable("network")))
    finally:
        e.close()
    row = st.get(db, rid)
    print(row["restore_note"], row["nzb_path2"], _kinds(db, rid))
    assert flagged == [] and row["nzb_path2"] == n2 and "weekly_flag" not in _kinds(db, rid)


def test_W2_non_nntp_error_on_restore_stat_is_not_a_loss(tmp_path):
    """The same OSError on nzb_path is 'failed' (no flag). On nzb_path2 it is 'missing':
    the restore NZB is cleared and a promote is flagged."""
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    try:
        flagged = _weekly(e, db, wrap_p=lambda p: Wrap(p, n2, OSError(5, "EIO")))
    finally:
        e.close()
    row = st.get(db, rid)
    print("flagged:", [x.get("promote_reasons") for x in flagged], "nzb_path2:", row["nzb_path2"],
          "events:", _kinds(db, rid))
    assert flagged == [] and row["nzb_path2"] == n2, \
        "one local read error on the restore NZB cleared nzb_path2 and flagged a promote"


def test_W2b_same_error_on_nzb_path_is_only_failed(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    nzb = st.get(db, rid)["nzb_path"]
    try:
        flagged = _weekly(e, db, wrap_p=lambda p: Wrap(p, nzb, OSError(5, "EIO")))
    finally:
        e.close()
    assert flagged == [] and st.get(db, rid)["nzb_path2"] == n2


def test_W3_cleared_nzb_path2_recovers_next_week(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    try:
        e.prim.ids.discard("alt-Alpha@t")  # the restore upload loses its articles
        f1 = _weekly(e, db)
        r1 = st.get(db, rid)
        e.prim.ids.add("alt-Alpha@t")  # it was transient (or a re-post with the same ids)
        f2 = _weekly(e, db, now=DAY15 + 7 * 86400)
        r2 = st.get(db, rid)
    finally:
        e.close()
    print("run1", [x["promote_reasons"] for x in f1], r1["nzb_path2"], r1["restore_score"])
    print("run2", [x["promote_reasons"] for x in f2], r2["nzb_path2"], r2["restore_score"],
          r2["restore_note"])
    assert f1 and r1["nzb_path2"] is None and r1["restore_score"] == 0
    assert f2 == [] and r2["nzb_path2"] == n2 and r2["restore_score"] == 1


def test_W4_what_weekly_writes_for_a_flag(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    seen = []
    try:
        e.prim.ids.discard("alt-Alpha@t")
        f1 = _weekly(e, db, on_promote=seen.append)
        r1 = st.get(db, rid)
        ev1 = [(x["kind"], x["detail"]) for x in st.events(db, rid)]
        e.prim.ids.add("alt-Alpha@t")
        _weekly(e, db, now=DAY15 + 7 * 86400)
        r2 = st.get(db, rid)
    finally:
        e.close()
    print("returned:", [(x["id"], x["promote_reasons"]) for x in f1])
    print("on_promote:", [(x["id"], x["promote_reasons"]) for x in seen])
    print("events:", ev1)
    print("row after flag:", {k: r1[k] for k in ("state", "restore_score", "restore_note", "nzb_path2")})
    print("row next week:", {k: r2[k] for k in ("state", "restore_score", "restore_note")})
    assert [x["promote_reasons"] for x in f1] == [["no_restore_path"]] and seen == f1
    assert ("weekly_flag", {"reasons": ["no_restore_path"]}) in ev1
    assert "restore_nzb_lost" in [k for k, _ in ev1] and r1["state"] == "offloaded"
    assert r1["restore_note"].startswith("promote flagged:")
    # restore_note is overwritten by the next OK run: not a durable flag
    assert not (r2["restore_note"] or "").startswith("promote flagged:")


def test_W5_known_dead_restore_nzb_with_spent_budget_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWZNAB_GRAB_BUDGET", "0")  # the day's grabs are gone
    e, db = _real(tmp_path, ["Alpha"])
    rid, n2 = _store_alt(tmp_path, db, "Alpha")
    try:
        e.prim.ids.discard("alt-Alpha@t")
        flagged = _weekly(e, db)
    finally:
        e.close()
    row = st.get(db, rid)
    print("flagged:", flagged, "nzb_path2 kept:", row["nzb_path2"] == n2, "note:",
          row["restore_note"], "events:", _kinds(db, rid))
    assert flagged == []  # contract: unsure never promotes
    assert {"alert", "restore_nzb_lost"} & set(_kinds(db, rid)), \
        "the stored restore NZB is known dead, but nothing records it while the search is unsure"


# =============================================================== indexer etiquette

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _client(srv, clock, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("grab_interval", 0)
    return nz.NewznabClient([nz.Indexer("one", srv.url, srv.key)], clock=clock,
                            sleep=lambda s: None, **kw)


def _try_search(ix):
    try:
        ix.search_indexer(ix.indexers[0], title="Movie")
        return "ok"
    except nz.NewznabError as e:
        return type(e).__name__


def test_E1_429_retry_after_seconds_is_honoured():
    s = FakeNewznab({})
    s.fail, s.fail_code, s.retry_after = True, 429, 120
    c = Clock()
    ix = _client(s, c)
    try:
        assert _try_search(ix) == "RateLimited"
        c.t += 60
        assert _try_search(ix) == "IndexerDropped" and len(s.queries) == 1
        s.fail = False
        c.t += 61
        assert _try_search(ix) == "ok" and len(s.queries) == 2
    finally:
        s.close()


def test_E2_429_retry_after_http_date_is_honoured():
    s = FakeNewznab({})
    s.fail, s.fail_code = True, 429
    s.retry_after = email.utils.formatdate(time.time() + 120, usegmt=True)
    c = Clock()
    ix = _client(s, c)
    try:
        assert _try_search(ix) == "RateLimited"
        s.fail = False
        c.t += 600  # well past the date the indexer gave
        got = _try_search(ix)
    finally:
        s.close()
    print("10 min after a 2-min HTTP-date Retry-After:", got, ix._dropped_until)
    assert got == "ok", "an HTTP-date Retry-After is ignored (dropped for 24 h instead)"


def test_E3_retry_after_longer_than_a_day_is_not_cut_short():
    s = FakeNewznab({})
    s.fail, s.fail_code, s.retry_after = True, 429, 2 * 86400
    c = Clock()
    ix = _client(s, c)
    try:
        assert _try_search(ix) == "RateLimited"
        c.t += 86400 + 1
        _try_search(ix)
    finally:
        s.close()
    print("requests within the 48 h Retry-After:", len(s.queries))
    assert len(s.queries) == 1, "asked again after 24 h although the indexer said 48 h"


def test_E4_dropped_indexer_comes_back_after_the_window():
    s = FakeNewznab({})
    s.fail = True
    c = Clock()
    ix = _client(s, c)
    try:
        got = [_try_search(ix) for _ in range(5)]
        assert got[:3] == ["NewznabError"] * 3 and got[3:] == ["IndexerDropped"] * 2
        assert len(s.queries) == 3
        s.fail = False
        c.t += 86400 + 1
        assert _try_search(ix) == "ok"
    finally:
        s.close()


def test_E5_grab_that_returns_an_error_page_counts_toward_drop():
    """Indexers answer an over-limit grab with HTTP 200 and <error code="501" .../>."""
    err = b'<?xml version="1.0"?><error code="501" description="Download limit reached"/>'
    s = FakeNewznab({f"g{i}": {"title": f"Movie.2020.1080p-G{i}", "nzb": err} for i in range(6)})
    c = Clock()
    ix = _client(s, c)
    try:
        rels = ix.search_indexer(ix.indexers[0], title="Movie")
        out = []
        for r in rels:
            try:
                ix.fetch_nzb(r)
                out.append("ok")
            except nz.NewznabError as e:
                out.append(type(e).__name__ + ":" + str(e))
    finally:
        s.close()
    print(out, "grabs that reached the indexer:", len(s.gets), "errors:", ix._errors)
    assert len(s.gets) <= 3, "an over-limit grab is noted as a success; the indexer is never dropped"


def test_E6_budget_and_spacing_are_thread_safe():
    s = FakeNewznab({})
    c = Clock()
    ix = _client(s, c, search_budget=5)
    res = []
    lock = threading.Lock()

    def go():
        r = _try_search(ix)
        with lock:
            res.append(r)

    ts = [threading.Thread(target=go) for _ in range(25)]
    try:
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        s.close()
    assert res.count("ok") == 5 and res.count("SearchBudgetSpent") == 20 and len(s.queries) == 5


def test_E7_budgets_survive_a_restart(tmp_path):
    """The runner is a container: a crash loop or a redeploy builds a new client."""
    s = FakeNewznab({})
    sp = str(tmp_path / "budget.json")
    try:
        for _ in range(3):  # three restarts within the day
            ix = _client(s, time.monotonic, search_budget=2, state_path=sp)
            for _ in range(5):
                _try_search(ix)
    finally:
        s.close()
    assert len(s.queries) <= 2


def test_E7b_budgets_reset_after_the_window_and_a_429_drop_survives(tmp_path):
    s = FakeNewznab({})
    sp = str(tmp_path / "budget.json")
    wall = Clock(5_000_000.0)
    mono = Clock(10.0)
    try:
        ix = _client(s, mono, search_budget=1, state_path=sp, wall=wall)
        assert _try_search(ix) == "ok" and _try_search(ix) == "SearchBudgetSpent"
        mono.t += 3600
        wall.t += 3600  # restart an hour later: the clocks differ, the budget holds
        mono2 = Clock(99.0)
        ix2 = _client(s, mono2, search_budget=1, state_path=sp, wall=wall)
        assert _try_search(ix2) == "SearchBudgetSpent"
        mono3 = Clock(5.0)
        wall.t += 86400  # a day on: free again
        ix3 = _client(s, mono3, search_budget=1, state_path=sp, wall=wall)
        assert _try_search(ix3) == "ok"
        s.fail, s.fail_code, s.retry_after = True, 429, 7200
        wall.t += 100
        ix4 = _client(s, Clock(1.0), search_budget=9, state_path=sp, wall=wall)
        assert _try_search(ix4) == "RateLimited"
        n = len(s.queries)
        s.fail = False
        wall.t += 60
        ix5 = _client(s, Clock(1.0), search_budget=9, state_path=sp, wall=wall)
        assert _try_search(ix5) == "IndexerDropped" and len(s.queries) == n
        wall.t += 7200
        ix6 = _client(s, Clock(1.0), search_budget=9, state_path=sp, wall=wall)
        assert _try_search(ix6) == "ok"
    finally:
        s.close()


def test_E7c_state_file_is_atomic_json_and_a_bad_one_is_ignored(tmp_path):
    s = FakeNewznab({})
    sp = tmp_path / "budget.json"
    try:
        ix = _client(s, Clock(), search_budget=3, state_path=str(sp))
        assert _try_search(ix) == "ok"
        import json
        d = json.loads(sp.read_text())
        assert len(d["spent"]["search|one"]) == 1
        assert [p.name for p in tmp_path.iterdir()] == ["budget.json"]  # no temp left behind
        sp.write_text("{not json")
        ix2 = _client(s, Clock(), search_budget=3, state_path=str(sp))
        assert _try_search(ix2) == "ok"
        ix3 = _client(s, Clock(), search_budget=3)  # no path: in memory only
        assert _try_search(ix3) == "ok"
    finally:
        s.close()


def test_E2b_bad_retry_after_keeps_the_24h_drop():
    for bad in ("soon", "", "Thu, 99 Foo 2020"):
        s = FakeNewznab({})
        s.fail, s.fail_code, s.retry_after = True, 429, bad
        c = Clock()
        ix = _client(s, c)
        try:
            assert _try_search(ix) == "RateLimited"
            c.t += 86399
            assert _try_search(ix) == "IndexerDropped"
            c.t += 2
            s.fail = False
            assert _try_search(ix) == "ok"
        finally:
            s.close()


def test_E5b_limit_codes_in_search_answers_drop_at_once_other_codes_count():
    for code, drops in (("500", True), ("501", True), ("429", True), ("100", False)):
        s = FakeNewznab({})
        s.fail = True
        s.fail_body = f'<?xml version="1.0"?><error code="{code}" description="x"/>'.encode()
        c = Clock()
        ix = _client(s, c)
        try:
            _try_search(ix)
            _try_search(ix)
            assert (len(s.queries) == 1) is drops, (code, len(s.queries))
        finally:
            s.close()


def test_E5c_a_good_grab_after_error_pages_resets_the_count():
    err = b'<?xml version="1.0"?><error code="100" description="bad"/>'
    good = make_nzb(["a@t"])
    s = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": err},
                     "g2": {"title": "Movie.2020.1080p-B", "nzb": err},
                     "g3": {"title": "Movie.2020.1080p-C", "nzb": good},
                     "g4": {"title": "Movie.2020.1080p-D", "nzb": err},
                     "g5": {"title": "Movie.2020.1080p-E", "nzb": err}})
    ix = _client(s, Clock())
    try:
        rels = ix.search_indexer(ix.indexers[0], title="Movie")
        by = {r.title[-1]: r for r in rels}
        got = []
        for k in "ABCDE":
            try:
                ix.fetch_nzb(by[k])
                got.append("ok")
            except nz.NewznabError as e:
                got.append(type(e).__name__)
        assert got == ["NewznabError", "NewznabError", "ok", "NewznabError", "NewznabError"]
        assert ix._dropped_until == {}
    finally:
        s.close()


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


def test_E8_spent_grab_budget_is_not_no_exact_nzb(tmp_path):
    w = World(tmp_path)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], grab_budget=0,
                              min_interval=0, grab_interval=0)
        r = _qualify(w, ix)
    finally:
        w.close()
    print(r.ok, r.reasons, r.steps.get("nzb"))
    assert r.reasons == ["nzb_unsure"], "a spent grab budget is 'try later', not 'not on any indexer'"


def test_E8b_partial_search_is_not_no_exact_nzb(tmp_path):
    """The exact release is only on indexer 'two', whose search budget is spent."""
    w = World(tmp_path)
    other = FakeNewznab({})
    try:
        ix = nz.NewznabClient([nz.Indexer("one", other.url, other.key),
                               nz.Indexer("two", w.nz.url, "nzkey")],
                              min_interval=0, grab_interval=0)
        ix._spent[("search", "two")] = __import__("collections").deque([ix._clock()] * 30)
        r = _qualify(w, ix)
    finally:
        other.close()
        w.close()
    print(r.ok, r.reasons)
    assert r.reasons == ["nzb_unsure"], "a partial search is 'try later', not 'not on any indexer'"


def test_E8c_complete_search_without_the_release_is_no_exact_nzb(tmp_path):
    w = World(tmp_path)
    other = FakeNewznab({})  # answers, has nothing
    try:
        w.nz.releases.clear()
        ix = nz.NewznabClient([nz.Indexer("one", other.url, other.key),
                               nz.Indexer("two", w.nz.url, "nzkey")],
                              min_interval=0, grab_interval=0)
        r = _qualify(w, ix)
    finally:
        other.close()
        w.close()
    assert r.reasons == ["no_exact_nzb"]


def test_E8d_three_failed_grabs_are_unsure(tmp_path):
    w = World(tmp_path)
    try:
        for rel in w.nz.releases.values():
            rel["nzb"] = b'<?xml version="1.0"?><error code="100" description="x"/>'
        ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], min_interval=0,
                              grab_interval=0)
        r = _qualify(w, ix)
    finally:
        w.close()
    assert r.reasons == ["nzb_unsure"]


# =============================================================== register

@pytest.fixture
def arrs():
    made = []

    def make(**kw):
        a = FakeArr(**kw)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


def _movie(arrs, path):
    return arrs(movies=[{"title": "Movie", "year": 2020, "imdbId": "tt1",
                         "movieFile": {"path": path, "size": 100}}])


def _disks(tmp_path):
    share = f"{tmp_path}/user/movies"
    d1, d2 = f"{tmp_path}/disk1/movies", f"{tmp_path}/disk2/movies"
    for d in (d1, d2, share):
        os.makedirs(d, exist_ok=True)
    with open(f"{d1}/m.mkv", "wb") as f:
        f.write(b"x" * 100)
    with open(f"{share}/m.mkv", "wb") as f:  # what the share shows (FUSE union)
        f.write(b"x" * 100)
    return share, d1, d2


def test_R1_arr_reporting_a_disk_path_never_becomes_lib_path(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path)
    monkeypatch.delenv("TIERING_ARR_PATH_MAP", raising=False)
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1},{share}={d2}")
    r = _movie(arrs, f"{d1}/m.mkv")  # an arr whose root folder is a disk path
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    got = [x["lib_path"] for x in st.list_files(db)]
    print(out, got)
    assert not any(p.startswith(d1) or p.startswith(d2) for p in got), \
        f"a disk path was stored as lib_path: {got}"
    assert out["disk_path"] == 1 and out["new"] == 0 and out["listed"] == 1


def test_R1b_a_mapped_arr_path_is_not_a_disk_path(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path)
    monkeypatch.setenv("TIERING_ARR_PATH_MAP", f"/arr={share}")
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1},{share}={d2}")
    r = _movie(arrs, "/arr/m.mkv")
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    assert out["new"] == 1 and out["disk_path"] == 0
    assert [x["lib_path"] for x in st.list_files(db)] == [f"{share}/m.mkv"]


def test_R2_unset_maps_store_the_arr_path_and_stat_it(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path)
    monkeypatch.delenv("TIERING_ARR_PATH_MAP", raising=False)
    monkeypatch.delenv("TIERING_PATH_MAP", raising=False)
    r = _movie(arrs, f"{share}/m.mkv")
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    assert out["new"] == 1 and [x["lib_path"] for x in st.list_files(db)] == [f"{share}/m.mkv"]


def test_R3_resolve_injection_and_counts(tmp_path, arrs, monkeypatch):
    monkeypatch.delenv("TIERING_ARR_PATH_MAP", raising=False)
    real = tmp_path / "disk3" / "c.mkv"
    real.parent.mkdir()
    real.write_bytes(b"c" * 300)
    table = {"/s/a.mkv": [], "/s/b.mkv": ["/d1/b.mkv", "/d2/b.mkv"], "/s/c.mkv": [str(real)]}
    asked, statted = [], []

    def resolve(p):
        asked.append(p)
        return table[p]

    def stat(p):
        statted.append(p)
        return os.stat(p)

    r = arrs(movies=[{"title": t, "year": 2020, "movieFile": {"path": f"/arr/{t}.mkv", "size": 1}}
                     for t in ("a", "b", "c", "a")])
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[],
                      arr_map="/arr=/s", resolve=resolve, stat=stat)
    print(out, asked, statted)
    assert out["listed"] == 3 and out["missing_local"] == 1 and out["ambiguous"] == 1
    assert out["new"] == 1 and statted == [str(real)]
    row = st.list_files(db)[0]
    assert row["lib_path"] == "/s/c.mkv" and row["size"] == 300


# =============================================================== keys

KEY = "K3Y" + "s" * 29


class _Redirect(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"http://10.9.9.9/steal?apikey={KEY}")
        self.end_headers()


def _all_text(e):
    parts = ["".join(traceback.format_exception(type(e), e, e.__traceback__)), repr(e), str(e)]
    x = e
    chain = []
    while x is not None:  # what a reporter that ignores __suppress_context__ would see
        chain.append(repr(x) + " " + str(getattr(x, "filename", "")) + str(getattr(x, "url", ""))
                     + " ".join(str(a) for a in getattr(x, "args", ())))
        x = x.__cause__ or x.__context__
    return "\n".join(parts), "\n".join(chain)


def test_K1_no_key_in_any_exception_text():
    shown, hidden = [], []
    s = FakeNewznab({"g": {"title": "Movie.2020.1080p-A", "nzb": make_nzb(["a@t"])}}, key=KEY)
    h = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    try:
        cases = []
        s.fail, s.fail_code = True, 500
        cases.append(lambda: _client(s, Clock()).search_indexer(nz.Indexer("one", s.url, KEY), title="M"))
        s2 = FakeNewznab({}, key=KEY)
        s2.fail, s2.fail_code, s2.retry_after = True, 429, 5
        cases.append(lambda: _client(s2, Clock()).search_indexer(nz.Indexer("one", s2.url, KEY), title="M"))
        s3 = FakeNewznab({"g": {"title": "Movie.2020.1080p-A", "nzb": make_nzb(["a@t"])}}, key=KEY)
        s3.truncate = True
        cases.append(lambda: _client(s3, Clock()).search_indexer(nz.Indexer("one", s3.url, KEY), title="M"))
        red = f"http://127.0.0.1:{h.server_address[1]}/api"
        cases.append(lambda: _client(s, Clock()).search_indexer(nz.Indexer("one", red, KEY), title="M"))
        cases.append(lambda: _client(s, Clock()).fetch_nzb(
            nz.Release("M", "g", 1, "one", 1080, f"{red}/get?apikey={KEY}")))
        cases.append(lambda: _client(s, Clock()).fetch_nzb(
            nz.Release("M", "g", 1, "one", 1080, f"{s.url}/get/missing?apikey={KEY}")))
        for fn in cases:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                a, b = _all_text(e)
                shown.append((type(e).__name__, KEY in a))
                hidden.append((type(e).__name__, KEY in b))
        s2.close()
        s3.close()
    finally:
        s.close()
        h.shutdown()
        h.server_close()
    print("in str/repr/traceback:", shown)
    print("in the suppressed __context__ chain:", hidden)
    assert len(shown) == 6 and not any(x for _, x in shown)
    assert not any(x for _, x in hidden), f"the key is reachable via __context__/__cause__: {hidden}"


def test_K2_no_key_in_logs_report_events_or_release(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    rel_raw = (b'<?xml version="1.0"?><rss><channel><item><title>Movie.2020.1080p-A</title>'
               b'<enclosure url="https://ix.example/getnzb/1.nzb&amp;apikey=' + KEY.encode()
               + b'" length="1"/></item></channel></rss>')
    ix = nz.Indexer("one", "https://ix.example/api", KEY)
    rel = nz.NewznabClient([ix])._parse(ix, rel_raw)[0]
    assert KEY not in repr(rel) and KEY not in rel.guid and rel.guid == ""
    assert KEY not in repr(ix)
    e, db = _real(tmp_path, ["Alpha", "Bravo"])
    e.indexers = nz.NewznabClient([nz.Indexer("one", e.nz.url, "nzkey")], min_interval=0)
    e.nz.fail = True
    try:
        _weekly(e, db)
    finally:
        e.close()
    blob = caplog.text + open(e.report).read() + repr(st.events(db)) + repr(st.list_files(db))
    assert "nzkey" not in blob


def test_K3_load_indexers_error_chain_has_no_key():
    raw = '[{"name": "one", "url": "https://ix.example/api", "apikey": "' + KEY + '"'  # cut off
    with pytest.raises(nz.NewznabError) as ei:
        nz.load_indexers(raw)
    shown, chain = _all_text(ei.value)
    x, docs = ei.value, []
    while x is not None:
        docs.append(str(getattr(x, "doc", "")))
        x = x.__cause__ or x.__context__
    assert KEY not in shown and KEY not in chain and not any(KEY in d for d in docs)


def test_K4_truncated_and_bad_gzip_nzb_chains_have_no_key():
    s = FakeNewznab({"g": {"title": "Movie.2020.1080p-A", "nzb": b"\x1f\x8bnot gzip"}}, key=KEY)
    try:
        c = _client(s, Clock())
        rel = nz.Release("M", "g", 1, "one", 1080, f"{s.url}/get/g?apikey={KEY}")
        with pytest.raises(nz.NewznabError) as ei:
            c.fetch_nzb(rel)
        a, b = _all_text(ei.value)
    finally:
        s.close()
    assert KEY not in a and KEY not in b


def test_S1_non_nntp_error_on_a_restore_stat_is_unchecked_not_missing():
    def boom(path):
        raise OSError(5, "EIO")

    def nntp(path):
        raise ns.NntpUnavailable("network")

    def notnzb(path):
        from xml.etree.ElementTree import ParseError
        raise ParseError("not xml")

    assert q.stat_status("x", [("p", boom)]) == "unchecked"
    assert q.stat_status("x", [("p", nntp)]) == "unchecked"
    assert q.stat_status("x", [("p", notnzb)]) == "missing"
