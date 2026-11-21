"""Regression tests for the second review round: grab cost, hostile input, protocol checks."""
import gzip
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fakes_intake import FakeAltMount, FakeNewznab, make_nzb
from test_qualify import IDS, REL, World
from test_weekly import Env
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q

ROW = {"release_name": REL, "title": "Movie", "year": 2020}
REFUSED = b'<?xml version="1.0"?><error code="429" description="grab limit reached"/>'


# ---------------------------------------------------------------- grab cost

def test_grabs_for_one_row_stop_after_three_failures():
    rels = {f"g{i}": {"title": f"Movie.2020.1080p.WEB-DL-G{i:02d}", "nzb": REFUSED}
            for i in range(30)}
    s = FakeNewznab(rels)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0)
        sc = q.score_restore(ROW, ix, frozenset(IDS), stop_at=1)
        assert sc["unsure"] and sc["count"] == 0
        assert len(s.gets) == 3
    finally:
        s.close()


def test_grab_budget_makes_the_row_unsure_not_failed():
    rels = {f"g{i}": {"title": f"Movie.2020.1080p.WEB-DL-G{i:02d}",
                      "nzb": make_nzb([f"g{i}@t"])} for i in range(6)}
    s = FakeNewznab(rels)
    try:
        ix = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0,
                              grab_budget=2)
        sc = q.score_restore(ROW, ix, frozenset(IDS))
        assert ix.grabs == 2 and len(s.gets) == 2
        assert sc["count"] == 2 and sc["unsure"]
        with pytest.raises(nz.GrabBudgetSpent):
            ix.fetch_nzb(ix.search(title="Movie")[-1])
        assert ix.grabs == 2
    finally:
        s.close()


def test_grab_budget_comes_from_the_environment(monkeypatch):
    assert nz.NewznabClient([]).grab_budget == 10
    monkeypatch.setenv("NEWZNAB_GRAB_BUDGET", "3")
    assert nz.NewznabClient([]).grab_budget == 3
    monkeypatch.setenv("NEWZNAB_GRAB_BUDGET", "lots")
    assert nz.NewznabClient([]).grab_budget == 10
    assert "NEWZNAB_GRAB_BUDGET" in nz.__doc__


def test_a_search_alone_counts_other_names_without_any_grab():
    s = FakeNewznab({
        "own": {"title": REL, "nzb": make_nzb(IDS)},
        "alt": {"title": "Movie.2020.1080p.WEB-DL-ALT", "nzb": make_nzb(["alt@t"])}})
    try:
        ix = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0)
        sc = q.score_restore(ROW, ix, None, stop_at=1, verify=False)
        assert sc["count"] == 1 and not sc["unsure"] and s.gets == []
    finally:
        s.close()


def test_weekly_makes_no_grabs_for_many_rows(tmp_path):
    e = Env(tmp_path, [f"T{i}" for i in range(10)])
    try:
        assert e.run() == []
        assert e.indexers.grabs == 0 and e.nz.gets == []
    finally:
        e.close()


def test_weekly_grabs_when_the_known_restore_candidate_is_gone(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    e.nz.releases = {k: v for k, v in e.nz.releases.items() if k.startswith("own-")}  # only ours
    try:
        promote = e.run()
        assert [r["promote_reasons"] for r in promote] == [["no_restore_path"]]
        assert e.indexers.grabs == 1
    finally:
        e.close()


def test_weekly_with_a_spent_budget_is_unchecked_not_flagged(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    e.nz.releases = {k: v for k, v in e.nz.releases.items() if k.startswith("own-")}
    e.indexers = nz.NewznabClient([nz.Indexer("one", e.nz.url, "nzkey")], grab_budget=0)
    try:
        assert e.run() == []
        assert e.nz.gets == []
        row = e.state.rows[f"{0:016x}"]
        assert row["restore_score"] == 1  # untouched
        assert "restore_unsure: 1 files" in e.text()
    finally:
        e.close()


def test_weekly_grabs_for_the_monthly_full_read_row(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    rows = {r["remote_path"]: r["sha256"] for r in e.state.rows.values()}

    class Reader:
        def sha256_full(self, p):
            return rows[p]

    try:
        e.run(reader=Reader(), monthly=True)
        assert 0 < e.indexers.grabs <= 2
    finally:
        e.close()


# ---------------------------------------------------------------- hostile input

def test_gzip_nzb_is_capped_after_decompression():
    big = b'<?xml version="1.0"?><nzb>' + b" " * (nz.NZB_MAX + (1 << 20)) + b"</nzb>"
    s = FakeNewznab({"g": {"title": "Movie.2020.1080p-A", "nzb": gzip.compress(big, 1)}})
    try:
        ix = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=0)
        rel = ix.search(title="Movie")[0]
        with pytest.raises(nz.NewznabError):
            ix.fetch_nzb(rel)
    finally:
        s.close()


def test_gzip_nzb_under_the_cap_and_truncated_gzip():
    good = make_nzb(["a@t"])
    assert nz._gunzip_capped(gzip.compress(good), nz.NZB_MAX) == good
    with pytest.raises(nz.NewznabError):
        nz._gunzip_capped(gzip.compress(good)[:-8], nz.NZB_MAX)
    with pytest.raises(nz.NewznabError):
        nz._gunzip_capped(b"\x1f\x8b" + b"junk" * 10, nz.NZB_MAX)


def test_qualify_truncated_archive_is_a_fail_not_a_crash(tmp_path):
    w = World(tmp_path)
    p = tmp_path / "t.nzb.gz"
    p.write_bytes(gzip.compress(make_nzb(IDS))[:-12])
    try:
        r = w.run({**w.row, "nzb_path": str(p)})
    finally:
        w.close()
    assert not r.ok and r.reasons == ["nzb_error"]


def test_qualify_errors_cover_the_decoder_failures():
    import zlib
    for exc in (EOFError, zlib.error, ValueError):
        assert issubclass(exc, q._ERRORS)


class _BadDav(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, length, status=207):
        body = (b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
                b"<D:href>/d/a.mkv</D:href><D:propstat><D:prop><D:getcontentlength>"
                + length + b"</D:getcontentlength><D:resourcetype/></D:prop></D:propstat>"
                b"</D:response></D:multistatus>")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(b"12 GB")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "12 GB")
        self.end_headers()


def test_webdav_bad_length_raises_altmount_error():
    h = ThreadingHTTPServer(("127.0.0.1", 0), _BadDav)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    try:
        rd = am.WebDavReader(f"http://127.0.0.1:{h.server_address[1]}", "u", "p")
        with pytest.raises(am.AltMountError):
            rd.find_media("/d")
    finally:
        h.shutdown()
        h.server_close()


class _DupNntp(socketserver.StreamRequestHandler):
    """Answers the first STAT twice (a buggy or hostile server), the rest normally."""

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


def test_stat_reply_must_carry_the_requested_message_id():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _DupNntp)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = ns.ProviderCfg(host="127.0.0.1", port=srv.server_address[1], tls=False, conns=1,
                         batch=10, timeout=5)
    try:
        with ns.NntpPool(cfg) as pool:
            with pytest.raises(ns.NntpUnavailable) as ei:
                ns.check_ids(iter(["a@t", "gone@t"]), pool)
    finally:
        srv.shutdown()
        srv.server_close()
    assert ei.value.kind == "protocol"


# ---------------------------------------------------------------- AltMount

def test_history_poll_sends_nzo_ids_in_the_query_and_the_key_in_the_body():
    a = FakeAltMount("/mnt/am/movies/x", pending_polls=0)
    try:
        c = am.ImportClient(a.url, "k" * 32, poll_interval=0, sleep=lambda s: None)
        a.key = "k" * 32
        c._add(b"<nzb/>", "x", "job1")
        slot = c._history("job1")
        assert slot and slot["nzo_id"] == "job1"
        polls = [u for u in a.urls if "mode=history" in u]
        assert polls and all("nzo_ids=job1" in u for u in polls)
        assert not any("apikey" in u for u in a.urls)
        assert c._history("other") is None
    finally:
        a.close()


def test_altmount_plain_http_needs_loopback_or_the_override(monkeypatch):
    monkeypatch.delenv("TIERING_ALTMOUNT_ALLOW_HTTP", raising=False)
    am._Http("http://127.0.0.1:1")
    am._Http("https://altmount.example")
    with pytest.raises(am.AltMountError):
        am._Http("http://altmount.example")
    with pytest.raises(am.AltMountError):
        am.WebDavReader("http://dav.example", "u", "p")
    monkeypatch.setenv("TIERING_ALTMOUNT_ALLOW_HTTP", "1")
    am._Http("http://altmount.example")
    assert "TIERING_ALTMOUNT_ALLOW_HTTP" in am.__doc__
