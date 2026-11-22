"""Regression tests for the third review round: restore NZB liveness and disjointness,
path namespaces in register, indexer etiquette, rolling budgets, hygiene.

Fakes only: no real indexer, provider or arr.
"""
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fakes_intake import APIKEY, FakeAltMount, FakeArr, FakeNewznab, make_nzb
from test_qualify import IDS, REL, World
from test_weekly import DAY15, Env
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q
from tiering.offload import register as rg
from tiering.offload import state as st
from tiering.offload import weekly

ROW = {"release_name": REL, "title": "Movie", "year": 2020}


@pytest.fixture(autouse=True)
def _fast_grabs(monkeypatch):
    monkeypatch.setenv("NEWZNAB_GRAB_INTERVAL", "0")
    monkeypatch.delenv("NEWZNAB_GRAB_BUDGET", raising=False)


def _weekly(e, db, **kw):
    p, s = e.checkers()
    try:
        return weekly.run_weekly(db, stat_primary=p, stat_secondary=s, indexers=e.indexers,
                                 report_path=e.report, state=st, now=kw.pop("now", DAY15), **kw)
    finally:
        p.close()
        s.close()


def _real(tmp_path, names, **kw):
    e = Env(tmp_path, names, **kw)
    db = st.open_db(tmp_path / "t.db")
    for r in e.state.rows.values():
        st.upsert(db, dict(r))
    return e, db


# =============================================================== earlier findings, re-proved

def test_E_last_stat_ok_ts_alert_on_real_state(tmp_path):
    e, db = _real(tmp_path, ["Alpha", "Bravo", "Charlie"])
    ids = {r["title"]: r["id"] for r in st.list_files(db)}
    st.update_if(db, ids["Alpha"], "offloaded", last_stat_ok_ts=DAY15 - 86400)
    st.update_if(db, ids["Bravo"], "offloaded", last_stat_ok_ts=DAY15 - 20 * 86400)
    st.update_if(db, ids["Charlie"], "offloaded", last_stat_ok_ts=None)
    try:
        e.prim.refuse_auth = True
        _weekly(e, db)
    finally:
        e.close()
    alerted = {ev["id"] for ev in st.events(db) if ev["kind"] == "alert"}
    assert ids["Alpha"] not in alerted and {ids["Bravo"], ids["Charlie"]} <= alerted


class _DupNntp(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"200 hi\r\n")
        first = True
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode().strip()
            if cmd.startswith("STAT"):
                mid = cmd[5:].strip()
                if first:
                    self.wfile.write(f"223 0 {mid}\r\n223 0 {mid}\r\n".encode())
                    first = False
                elif mid == "<gone@t>":
                    self.wfile.write(b"430 no such article\r\n")
                else:
                    self.wfile.write(f"223 0 {mid}\r\n".encode())
            elif cmd == "QUIT":
                return


def _nntp_srv(handler):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, ns.ProviderCfg(host="127.0.0.1", port=srv.server_address[1], tls=False,
                               conns=1, batch=10, timeout=5)


def test_E_stat_out_of_step_reply_is_never_counted_present():
    srv, cfg = _nntp_srv(_DupNntp)
    try:
        with ns.NntpPool(cfg) as pool:
            try:
                out = ns.check_ids(iter(["a@t", "gone@t"]), pool)
            except ns.NntpUnavailable as e:
                assert e.kind == "protocol"
                return
    finally:
        srv.shutdown()
        srv.server_close()
    assert out["missing"] == 1, out


def test_E_altmount_history_nzo_ids_in_query_key_in_body(monkeypatch):
    seen = []
    orig = am._Http.open

    def spy(self, method, path, headers=None, body=None):
        seen.append((path, body if isinstance(body, bytes) else b""))
        return orig(self, method, path, headers, body)

    monkeypatch.setattr(am._Http, "open", spy)
    s = FakeAltMount("/mnt/am/x.mkv", pending_polls=1)
    try:
        p = am.ImportClient(s.url, APIKEY, poll_interval=0, sleep=lambda x: None).import_nzb(
            make_nzb(["a@x"]), "n")
    finally:
        s.close()
    assert p == "/mnt/am/x.mkv"
    hist = [(u, b) for u, b in seen if b"history" in b]
    assert hist and all("nzo_ids=" in u and APIKEY not in u for u, _ in hist)
    assert all(b"apikey=" + APIKEY.encode() in b for _, b in hist)


def test_E_altmount_allow_http_env(monkeypatch):
    monkeypatch.delenv("TIERING_ALTMOUNT_ALLOW_HTTP", raising=False)
    with pytest.raises(am.AltMountError):
        am._Http("http://10.1.2.3:8080")
    am._Http("http://127.0.0.1:8080")
    monkeypatch.setenv("TIERING_ALTMOUNT_ALLOW_HTTP", "1")
    am._Http("http://10.1.2.3:8080")


def test_E_weekly_live_nzb_path2_costs_no_search_and_no_grab(tmp_path):
    e, db = _real(tmp_path, ["Alpha", "Bravo"])
    for r in st.list_files(db):  # store the alternative and make its articles live
        alt = make_nzb([f"alt-{r['title']}@t"])
        n2 = nz.archive_nzb(alt, str(tmp_path / "arch"), f"{r['title']}.2020.1080p.WEB-DL-ALT")
        st.update_if(db, r["id"], "offloaded", nzb_path2=n2)
        e.prim.ids.add(f"alt-{r['title']}@t")
        e.sec.ids.add(f"alt-{r['title']}@t")
    try:
        assert _weekly(e, db, monthly=False) == []
        assert e.nz.queries == [] and e.nz.gets == [], (e.nz.queries, e.nz.gets)
    finally:
        e.close()


# =============================================================== Path namespaces (register)

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


def _disks(tmp_path, on_disk):
    """share /<tmp>/user/movies, disks /<tmp>/disk1/movies and /<tmp>/disk2/movies."""
    share = f"{tmp_path}/user/movies"
    d1, d2 = f"{tmp_path}/disk1/movies", f"{tmp_path}/disk2/movies"
    for d in (d1, d2):
        os.makedirs(d, exist_ok=True)
    with open(f"{tmp_path}/{on_disk}/movies/m.mkv", "wb") as f:
        f.write(b"x" * 100)
    return share, d1, d2


def _movie(arrs, path):
    return arrs(movies=[{"title": "Movie", "year": 2020, "imdbId": "tt1",
                         "movieFile": {"path": path, "size": 100}}])


def test_N1a_register_stores_the_share_path_not_the_disk_path(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path, "disk1")
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1},{share}={d2}")  # contract: share=disk
    r = _movie(arrs, f"{share}/m.mkv")  # the arr sees the same path as Plex
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    paths = [x["lib_path"] for x in st.list_files(db)]
    print(out, paths)
    assert paths == [f"{share}/m.mkv"], f"lib_path is a disk path: {paths}"


def test_N1b_register_finds_a_file_on_the_second_disk_of_a_share(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path, "disk2")
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1},{share}={d2}")
    r = _movie(arrs, f"{share}/m.mkv")
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    print(out)
    assert out["new"] == 1 and out["missing_local"] == 0, out


def test_N1c_register_maps_arr_paths_with_TIERING_ARR_PATH_MAP(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path, "disk1")
    monkeypatch.setenv("TIERING_ARR_PATH_MAP", f"/data/movies={share}")
    monkeypatch.setenv("TIERING_PATH_MAP", f"{share}={d1},{share}={d2}")
    r = _movie(arrs, "/data/movies/m.mkv")  # the arr's own container path
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    print(out, [x["lib_path"] for x in st.list_files(db)])
    assert [x["lib_path"] for x in st.list_files(db)] == [f"{share}/m.mkv"], out


# =============================================================== restore path

def _takes_id(fn) -> bool:
    import inspect
    return len(inspect.signature(fn).parameters) > 1


def _idx(*releases):
    s = FakeNewznab({f"g{i}": {"title": t, "nzb": n} for i, (t, n) in enumerate(releases)})
    return s, nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")])


def test_N2a_same_upload_with_angle_brackets_is_not_a_restore_path():
    # valid XML: <segment>&lt;s0@t&gt;</segment>, as some posting tools write it
    s, ix = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb([f"&lt;{m}&gt;" for m in IDS])))
    try:
        sc = q.score_restore(ROW, ix, frozenset(IDS))
    finally:
        s.close()
    print(sc["note"], sc["unsure"])
    assert not sc["unsure"]
    assert sc["count"] == 0, f"the same articles in <...> counted as a different upload: {sc['note']}"


def test_N2b_message_ids_stay_case_sensitive():
    ids = [m.split("@")[0] + "@T" for m in IDS]  # a different id, not a spelling of ours
    s, ix = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(ids)))
    try:
        sc = q.score_restore(ROW, ix, frozenset(IDS))
    finally:
        s.close()
    assert sc["count"] == 1, sc["note"]


def test_N2c_norm_id_strips_brackets_and_space_only():
    assert nz.norm_id("  <a@b>\n") == "a@b" and nz.norm_id("A@b") == "A@b"


def test_N3_qualify_passes_with_a_dead_only_alternative(tmp_path):
    """The alternative has articles alt0..4@t; neither provider has them."""
    w = World(tmp_path, alt_live=False)
    try:
        r = w.run()
    finally:
        w.close()
    print(r.ok, r.fields.get("restore_note"), r.fields.get("nzb_path2"))
    assert not r.ok, "qualify counted (and archived as nzb_path2) an alternative whose articles are gone"


def test_N4_weekly_reaccepts_the_dead_nzb_path2_it_just_found_dead(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    e.prim.ids.discard("alt-Alpha@t")  # the alternative's article is on neither provider
    e.sec.ids.discard("alt-Alpha@t")
    row = st.list_files(db)[0]
    n2 = nz.archive_nzb(make_nzb(["alt-Alpha@t"]), str(tmp_path / "arch"),
                        "Alpha.2020.1080p.WEB-DL-ALT")
    st.update_if(db, row["id"], "offloaded", nzb_path2=n2)
    try:
        flagged = _weekly(e, db, monthly=False)
    finally:
        e.close()
    after = st.get(db, row["id"])
    print("flagged:", [x["id"] for x in flagged], "note:", after["restore_note"],
          "score:", after["restore_score"], "grabs:", e.nz.gets)
    assert flagged, "a restore NZB with missing articles was re-found and accepted as OK"
    assert "restore path OK" not in (after["restore_note"] or "")
    assert after["nzb_path2"] is None, "the dead restore NZB was kept"
    kinds = [ev["kind"] for ev in st.events(db, row["id"])] if _takes_id(st.events) else \
        [ev["kind"] for ev in st.events(db) if ev["id"] == row["id"]]
    assert "alert" in kinds


def test_N5_register_change_clears_nzb_path2(tmp_path, arrs):
    lib = tmp_path / "lib" / "m.mkv"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"n" * 200)
    db = st.open_db(tmp_path / "t.db")
    st.upsert(db, {"id": "a" * 16, "lib_path": str(lib), "state": "whole", "size": 100,
                   "mtime_ns": 1, "nzb_path": "/arch/A.nzb.gz", "nzb_path2": "/arch/B.nzb.gz",
                   "restore_score": 2, "restore_note": "2 alternative(s), best 1080p",
                   "title": "Movie", "release_name": "Movie.2020.720p-A"})
    r = arrs(movies=[{"title": "Movie", "year": 2020,
                      "movieFile": {"path": "/data/m.mkv", "size": 200,
                                    "sceneName": "Movie.2020.1080p-B"}}])
    rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[],
                arr_map=f"/data={tmp_path}/lib")
    row = st.get(db, "a" * 16)
    print({k: row[k] for k in ("nzb_path", "nzb_path2", "restore_score", "restore_note")})
    assert row["nzb_path"] is None
    assert row["nzb_path2"] is None, "the old release's restore NZB survived the release change"


def test_N6a_qualify_on_a_reused_client_passes_without_nzb_path2(tmp_path):
    """wiring.qualify_from_env builds ONE NewznabClient for the runner's life. A row
    qualified twice (dropped after qualify, or the next night) gets no nzb_path2 the
    second time: fetch_ids hits its cache and take_raw has nothing."""
    w = World(tmp_path)
    reader = am.WebDavReader(w.dav.url, "u", "p")
    importer = am.ImportClient(w.am.url, APIKEY, reader=reader, mount_path="/mnt/am",
                               poll_interval=0, sleep=lambda s: None)
    ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")])
    w.prim.ids |= {f"alt{i}@t" for i in range(5)}
    w.sec.ids |= {f"alt{i}@t" for i in range(5)}
    p, s = ns.Checker(w.prim.cfg()), ns.Checker(w.sec.cfg())
    try:
        runs = [q.qualify(w.row, reader=reader, importer=importer, indexers=ix,
                          stat_primary=p, stat_secondary=s, archive_dir=w.archive)
                for _ in range(2)]
    finally:
        p.close()
        s.close()
        w.close()
    print([(r.ok, r.fields.get("nzb_path2")) for r in runs])
    assert runs[0].ok and runs[0].fields.get("nzb_path2")
    assert not runs[1].ok or runs[1].fields.get("nzb_path2"), \
        "second qualify passed with no nzb_path2: a stale stored one is kept by update_if"


def test_N6b_weekly_trusts_an_nzb_path2_that_is_the_same_upload(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    row = st.list_files(db)[0]
    st.update_if(db, row["id"], "offloaded", nzb_path2=row["nzb_path"])  # e.g. the old release's alt
    try:
        _weekly(e, db, monthly=False)
    finally:
        e.close()
    after = st.get(db, row["id"])
    print(after["restore_note"], "searches:", len(e.nz.queries))
    assert "stored alternative" not in (after["restore_note"] or ""), \
        "weekly called the offloaded upload itself a restore path"


# =============================================================== indexer etiquette

def test_N7_weekly_stops_asking_an_indexer_that_keeps_failing(tmp_path):
    names = [f"T{i:02d}" for i in range(30)]
    e, db = _real(tmp_path, names)  # no nzb_path2 stored: every row searches
    e.nz.fail = True  # e.g. HTTP 500 / request limit reached
    try:
        _weekly(e, db, monthly=False)
        print("searches sent to a failing indexer:", len(e.nz.queries), "for", len(names), "rows")
        assert len(e.nz.queries) <= 3, f"{len(e.nz.queries)} searches to an indexer that keeps failing"
    finally:
        e.close()


def test_N7b_search_budget_caps_searches_and_the_rest_is_unchecked(tmp_path):
    names = [f"T{i:02d}" for i in range(12)]
    e, db = _real(tmp_path, names)
    e.indexers = nz.NewznabClient([nz.Indexer("one", e.nz.url, "nzkey")], search_budget=5)
    try:
        flagged = _weekly(e, db, monthly=False)
        assert len(e.nz.queries) == 5, len(e.nz.queries)
        assert flagged == [], "a spent budget must never promote"
    finally:
        e.close()


def test_N7c_default_budgets_and_interval_from_env(monkeypatch):
    for k in ("NEWZNAB_SEARCH_BUDGET", "NEWZNAB_MIN_INTERVAL_S", "NEWZNAB_GRAB_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    c = nz.NewznabClient([])
    assert (c.search_budget, c.min_interval, c.grab_budget) == (30, 2.0, 10)
    monkeypatch.setenv("NEWZNAB_SEARCH_BUDGET", "7")
    monkeypatch.setenv("NEWZNAB_MIN_INTERVAL_S", "0.5")
    c = nz.NewznabClient([])
    assert (c.search_budget, c.min_interval) == (7, 0.5)


def test_N7d_requests_to_one_indexer_are_spaced():
    s, _ = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(["z@t"])))
    sleeps = []
    t = [100.0]
    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0, min_interval=2.0,
                             sleep=lambda x: (sleeps.append(x), t.__setitem__(0, t[0] + x)),
                             clock=lambda: t[0])
        c.search(title="Movie")
        c.search(title="Movie")
    finally:
        s.close()
    assert sleeps == [2.0], sleeps


def test_N7e_a_429_drops_the_indexer_at_once_and_honours_retry_after():
    s, _ = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(["z@t"])))
    t = [0.0]
    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0, min_interval=0,
                             clock=lambda: t[0])
        s.fail, s.fail_code, s.retry_after = True, 429, 120
        with pytest.raises(nz.NewznabError):
            c.search(title="Movie")
        n = len(s.queries)
        s.fail = False
        t[0] = 60.0  # still inside Retry-After
        with pytest.raises(nz.NewznabError):
            c.search(title="Movie")
        assert len(s.queries) == n, "asked again inside Retry-After"
        t[0] = 121.0
        assert c.search(title="Movie")
    finally:
        s.close()


def test_N7f_three_errors_in_a_row_drop_the_indexer_for_the_window():
    s, _ = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(["z@t"])))
    t = [0.0]
    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0, min_interval=0,
                             clock=lambda: t[0])
        s.fail = True
        for _ in range(5):
            with pytest.raises(nz.NewznabError):
                c.search(title="Movie")
        assert len(s.queries) == 3
        s.fail = False
        t[0] = 24 * 3600 + 1
        assert c.search(title="Movie")
    finally:
        s.close()


def test_N7g_qualify_searches_once(tmp_path):
    w = World(tmp_path)
    w.prim.ids |= {f"alt{i}@t" for i in range(5)}
    w.sec.ids |= {f"alt{i}@t" for i in range(5)}
    try:
        r = w.run()
        assert r.ok, r.reasons
        assert len(w.nz.queries) == 1, w.nz.queries
    finally:
        w.close()


def test_N7h_qualify_stops_at_the_first_verified_alternative(tmp_path):
    w = World(tmp_path)
    w.prim.ids |= {f"alt{i}@t" for i in range(5)}
    w.sec.ids |= {f"alt{i}@t" for i in range(5)}
    try:
        r = w.run()
        assert r.ok and len(w.nz.gets) <= 2, w.nz.gets  # the exact NZB and one alternative
    finally:
        w.close()


def test_N8_long_lived_client_budgets_roll_over_24_hours(tmp_path):
    """One client, as wiring builds it, for many days: the caller never resets it."""
    now = [1000.0]
    oks = []
    ix = None
    for n in range(8):
        (tmp_path / f"w{n}").mkdir()
        w = World(tmp_path / f"w{n}")
        if ix is None:
            ix = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey")], grab_interval=0,
                                  min_interval=0, grab_budget=2, search_budget=1,
                                  clock=lambda: now[0])
        else:  # same indexer label, new fake server for this title
            ix.indexers = [nz.Indexer("one", w.nz.url, "nzkey")]
        w.prim.ids |= {f"alt{i}@t" for i in range(5)}
        w.sec.ids |= {f"alt{i}@t" for i in range(5)}
        reader = am.WebDavReader(w.dav.url, "u", "p")
        importer = am.ImportClient(w.am.url, APIKEY, reader=reader, mount_path="/mnt/am",
                                   poll_interval=0, sleep=lambda s: None)
        p, s = ns.Checker(w.prim.cfg()), ns.Checker(w.sec.cfg())
        try:
            r = q.qualify(w.row, reader=reader, importer=importer, indexers=ix,
                          stat_primary=p, stat_secondary=s, archive_dir=w.archive)
        finally:
            p.close()
            s.close()
            w.close()
        oks.append(r.ok)
        now[0] += 25 * 3600  # the next night
    assert all(oks), oks


def test_N8b_budget_inside_the_window_still_binds():
    s, _ = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(["z@t"])))
    t = [0.0]
    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0, min_interval=0,
                             search_budget=2, clock=lambda: t[0])
        c.search(title="Movie")
        c.search(title="Movie")
        t[0] = 23 * 3600
        with pytest.raises(nz.NewznabError):  # the only indexer has spent its budget
            c.search(title="Movie")
        t[0] = 24 * 3600 + 1
        assert c.search(title="Movie")
    finally:
        s.close()


# =============================================================== hygiene / robustness

def test_N9_release_repr_never_shows_the_link_key():
    ix = nz.Indexer("one", "http://127.0.0.1:1/api", "SECRETKEY")
    raw = (b'<?xml version="1.0"?><rss><channel><item><title>Movie.2020.1080p-A</title>'
           b'<enclosure url="https://ix.example/getnzb/1.nzb&amp;r=SECRETKEY" length="1"/>'
           b"</item></channel></rss>")
    rel = nz.NewznabClient([ix])._parse(ix, raw)[0]
    assert "SECRETKEY" not in repr(rel), repr(rel)
    assert "SECRETKEY" not in rel.guid


def test_N9b_errors_never_carry_the_key():
    s, _ = _idx(("Movie.2020.1080p.WEB-DL-OTHER", make_nzb(["z@t"])))
    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0, min_interval=0)
        s.fail = True
        with pytest.raises(nz.NewznabError) as ei:
            c.search_indexer(c.indexers[0], title="Movie")
        assert "nzkey" not in str(ei.value) and "nzkey" not in repr(c.indexers[0])
    finally:
        s.close()


class _ShortDav(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
        self.send_response(207)
        self.send_header("Content-Length", str(len(body) + 500))  # then hang up early
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def test_N10_webdav_listing_cut_short_is_an_altmount_error():
    h = ThreadingHTTPServer(("127.0.0.1", 0), _ShortDav)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    try:
        rd = am.WebDavReader(f"http://127.0.0.1:{h.server_address[1]}", "u", "p")
        with pytest.raises(q._ERRORS):
            rd.find_media("/d")
    finally:
        h.shutdown()
        h.server_close()


class _TextNntp(socketserver.StreamRequestHandler):
    """A server that adds RFC 977 style text after the message-id."""

    def handle(self):
        self.wfile.write(b"200 hi\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode().strip()
            if cmd.startswith("STAT"):
                mid = cmd[5:].strip()
                self.wfile.write(f"223 0 {mid} article retrieved - request text separately\r\n".encode())
            elif cmd == "QUIT":
                return


def test_N11_stat_reply_with_trailing_text_is_accepted():
    srv, cfg = _nntp_srv(_TextNntp)
    try:
        with ns.NntpPool(cfg) as pool:
            out = ns.check_ids(iter(["a@t", "b@t"]), pool)
    finally:
        srv.shutdown()
        srv.server_close()
    assert out == {"total": 2, "missing": 0, "missing_ids": []}


def test_nntp_retries_back_off(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ns, "_sleep", sleeps.append)
    monkeypatch.setattr(ns, "BACKOFF_S", 0.5)
    cfg = ns.ProviderCfg(host="127.0.0.1", port=1, tls=False, conns=1, batch=10, timeout=1)
    with ns.NntpPool(cfg) as pool:
        with pytest.raises(ns.NntpUnavailable):
            pool.stat_batch(["a@t"])
    assert sleeps == [0.5, 1.0], sleeps


def test_N10b_size_head_cut_short_is_an_altmount_error(monkeypatch):
    rd = am.WebDavReader("http://127.0.0.1:1", "u", "p")

    class R:
        status = 200

        def getheader(self, *a):
            raise am.http.client.IncompleteRead(b"")

    class C:
        def close(self):
            pass

    monkeypatch.setattr(rd._http, "open", lambda *a, **k: (C(), R()))
    with pytest.raises(am.AltMountError):
        rd.size("/x.mkv")


def test_register_resolve_is_injectable_and_trouble_is_counted(tmp_path, arrs):
    r = arrs(movies=[{"title": f"M{i}", "year": 2020, "imdbId": f"tt{i}",
                      "movieFile": {"path": f"/share/m{i}.mkv", "size": 1}} for i in range(3)])
    f = tmp_path / "one.mkv"
    f.write_bytes(b"x" * 7)
    answers = {"/share/m0.mkv": [str(f)], "/share/m1.mkv": [], "/share/m2.mkv": ["/a", "/b"]}
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[],
                      arr_map="", resolve=lambda p: answers[p])
    assert (out["listed"], out["new"], out["missing_local"], out["ambiguous"]) == (3, 1, 1, 1), out
    row = st.list_files(db)[0]
    assert row["lib_path"] == "/share/m0.mkv" and row["size"] == 7


def test_register_does_not_read_TIERING_PATH_MAP_for_arr_paths(tmp_path, arrs, monkeypatch):
    share, d1, d2 = _disks(tmp_path, "disk1")
    monkeypatch.delenv("TIERING_ARR_PATH_MAP", raising=False)
    monkeypatch.setenv("TIERING_PATH_MAP", f"/data/movies={share}")  # a different meaning
    r = _movie(arrs, "/data/movies/m.mkv")
    db = st.open_db(tmp_path / "t.db")
    out = rg.register(db, True, state=st, radarrs=[rg.ArrClient(r.url, r.key)], sonarrs=[])
    assert out["new"] == 0 and out["missing_local"] == 1, out


def test_N6c_qualify_clears_a_stale_nzb_path2_when_there_is_no_candidate(tmp_path):
    w = World(tmp_path, alt_live=False)  # the alternative's articles are gone
    w.row["nzb_path2"] = "/arch/stale.nzb.gz"
    try:
        r = w.run()
    finally:
        w.close()
    assert not r.ok and r.fields.get("nzb_path2", "unset") is None
