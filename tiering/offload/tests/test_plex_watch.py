import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from offload import plex_watch  # noqa: E402

TOKEN = "test-token-abc"
SEEN = []

SECTIONS = {"Directory": [
    {"key": "1", "type": "movie", "title": "Movies"},
    {"key": "2", "type": "show", "title": "TV"},
    {"key": "3", "type": "artist", "title": "Music"},
]}
MOVIES = [{"ratingKey": 10 + i, "type": "movie", "addedAt": 100 + i,
           "Media": [{"videoResolution": "4k" if i == 0 else "1080",
                      "Part": [{"file": f"/m/movie{i}.mkv"}]}]} for i in range(5)]
EPS = [{"ratingKey": 100 + i, "type": "episode", "grandparentRatingKey": 50,
        "parentIndex": 1, "index": i + 1, "addedAt": 200,
        "viewOffset": 5000 if i == 2 else 0,
        "Media": [{"videoResolution": "1080", "Part": [{"file": f"/t/e{i}.mkv"}]}]}
       for i in range(7)]


def page(rows, qs, hdr):
    start = int(hdr.get("X-Plex-Container-Start", 0))
    size = int(hdr.get("X-Plex-Container-Size", 1000))
    return {"MediaContainer": {"totalSize": len(rows), "size": len(rows[start:start + size]),
                               "Metadata": rows[start:start + size]}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        SEEN.append((self.path, dict(self.headers)))
        if self.headers.get("X-Plex-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        p = u.path
        if p == "/library/sections":
            body = {"MediaContainer": SECTIONS}
        elif p == "/library/sections/1/all":
            body = page(MOVIES, qs, self.headers)
        elif p == "/library/sections/2/all":
            rows = EPS
            if qs.get("inProgress"):
                rows = [e for e in EPS if e["viewOffset"] > 0]
                if qs.get("type") != ["4"]:  # without type=4 Plex returns shows, not episodes
                    rows = [{"ratingKey": 50, "type": "show", "viewOffset": 1}]
            body = page(rows, qs, self.headers)
        elif p == "/status/sessions/history/all":
            rows = [{"ratingKey": 10, "viewedAt": 500}, {"ratingKey": 101, "viewedAt": 900,
                    "grandparentRatingKey": 50}, {"ratingKey": 11, "viewedAt": 100},
                    {"ratingKey": 104, "viewedAt": 800, "type": "episode", "grandparentKey": "/library/metadata/50"}]
            body = page(rows, qs, self.headers)
        elif p == "/status/sessions":
            body = {"MediaContainer": {"Metadata": [
                {"ratingKey": 12, "Media": [{"Part": [{"file": "/m/movie2.mkv"}]}]}]}}
        elif p == "/library/onDeck":
            body = page([{"ratingKey": 103}], qs, self.headers)
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def plex(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("PLEX_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("PLEX_TOKEN", TOKEN)
    monkeypatch.setattr(plex_watch, "PAGE", 3)
    SEEN.clear()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_library_items_paged(plex):
    items = plex_watch.library_items()
    assert len(items) == 12
    m0 = items[0]
    assert m0["ratingKey"] == "10" and m0["section"] == "Movies"
    assert m0["videoResolution"] == "4k" and m0["files"] == ["/m/movie0.mkv"]
    e = [i for i in items if i["type"] == "episode"]
    assert e[2]["grandparentRatingKey"] == "50" and e[2]["index"] == 3
    # paging: 5 movies at size 3 means two requests for section 1
    assert sum(1 for p, _ in SEEN if p.startswith("/library/sections/1/all")) == 2


def test_token_header_only(plex):
    plex_watch.library_items()
    plex_watch.sessions()
    assert SEEN
    for path, hdr in SEEN:
        assert TOKEN not in path
        assert hdr.get("X-Plex-Token") == TOKEN


def test_history_sessions_deck_progress(plex):
    h = plex_watch.play_history(200)
    assert [r["ratingKey"] for r in h] == ["10", "101", "104"]
    assert h[2]["grandparentRatingKey"] == "50"  # real history rows only carry grandparentKey
    assert plex_watch.sessions() == [{"ratingKey": "12", "file": "/m/movie2.mkv"}]
    assert plex_watch.on_deck() == [{"ratingKey": "103"}]
    ip = plex_watch.in_progress()
    assert [r["ratingKey"] for r in ip] == ["102"]


def test_errors_do_not_leak_token(plex, monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN", "wrong-secret-value")
    with pytest.raises(plex_watch.PlexError) as ei:
        plex_watch.sessions()
    assert "wrong-secret-value" not in str(ei.value)


def test_missing_env(monkeypatch):
    monkeypatch.delenv("PLEX_URL", raising=False)
    with pytest.raises(plex_watch.PlexError):
        plex_watch.sessions()


def test_read_tierfs_events(tmp_path):
    p = tmp_path / "events.jsonl"
    ev = lambda ts, i, e: json.dumps({"ts": ts, "id": i, "ev": e}) + "\n"  # noqa: E731
    p.write_text(ev(1000, "aa", "open") + "not json\n" + ev(1100, "aa", "want")
                 + "\n[1,2]\n" + '{"ts": "x", "id": "aa", "ev": "want"}\n'
                 + ev(1200, "zz", "want") + ev(50, "bb", "want"))
    (tmp_path / "events.jsonl.1").write_text(ev(900, "cc", "want") + "\xff garbage")
    (tmp_path / "events.jsonl.3").write_text(ev(950, "dd", "want"))
    ids = {"aa": 7, "bb": "8", "cc": "9", "dd": 4}
    got = plex_watch.read_tierfs_events(str(p), 100, ids)
    assert got == {"7": 1100.0, "9": 900.0, "4": 950.0}
    assert set(plex_watch.read_tierfs_events(str(p), 0, ids)) == {"7", "8", "9", "4"}
    assert plex_watch.read_tierfs_events(str(tmp_path / "missing"), 0, ids) == {}


def test_get_only():
    src = open(plex_watch.__file__).read()
    assert not re.search(r"method\s*=", src)
    assert not re.search(r"\bdata\s*=", src)
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        assert verb not in src


def test_durations_part_then_media_then_item():
    row = {"ratingKey": "9", "duration": 3000, "Media": [
        {"duration": 2000, "Part": [{"file": "/a.mkv", "duration": 1000}, {"file": "/b.mkv"}]},
        {"Part": [{"file": "/c.mkv"}, {"file": "/d.mkv", "duration": "x"}, {"duration": 5}]},
    ]}
    assert plex_watch._durations(row) == {"/a.mkv": 1000, "/b.mkv": 2000, "/c.mkv": 3000}
    assert plex_watch._durations({"Media": [{"Part": [{"file": "/e.mkv"}]}]}) == {}
    assert plex_watch._norm(row)["durations"]["/a.mkv"] == 1000
