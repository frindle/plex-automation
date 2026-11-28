"""notify: Radarr/Sonarr rescan through tiering's own client, then a Plex partial scan.
Fake HTTP only; the key must be in a header and never in a URL."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from offload import notify, plex_watch, register  # noqa: E402

KEY = "k-secret-123"
TOKEN = "t-secret-456"


class Fake:
    """An arr client double: a list of items, a log of posts."""

    def __init__(self, items):
        self.items, self.posts, self.gets = items, [], []

    def get(self, path, **params):
        self.gets.append(path)
        return self.items

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


MOVIE_ROW = {"id": "m1", "kind": "movie", "imdb_id": "tt1"}
EP_ROW = {"id": "e1", "kind": "episode", "tvdb_id": 77}


def test_movie_rescan_uses_the_movie_id_found_by_folder():
    r = Fake([{"id": 3, "path": "/data/movies/Other (2019)"},
              {"id": 5, "path": "/data/movies/Movie (2020)"}])
    out = notify.notify_from_env({}, radarrs=[r], sonarrs=[])(
        MOVIE_ROW, "/data/movies/Movie (2020)/File.mkv")
    assert r.gets == ["/api/v3/movie"]
    assert r.posts == [("/api/v3/command", {"name": "RescanMovie", "movieId": 5})]
    assert out["arr"] == "RescanMovie 5"


def test_episode_rescan_uses_the_series_id_found_by_folder():
    r = Fake([{"id": 8, "path": "/data/tv/Show"}])
    out = notify.notify_from_env({}, radarrs=[], sonarrs=[r])(
        EP_ROW, "/data/tv/Show/Season 1/File.mkv")
    assert r.posts == [("/api/v3/command", {"name": "RescanSeries", "seriesId": 8})]
    assert out["arr"] == "RescanSeries 8"


def test_path_map_translates_the_arr_folder():
    r = Fake([{"id": 4, "path": "/arr/movies/Movie (2020)"}])
    env = {"TIERING_ARR_PATH_MAP": "/arr/movies=/share/movies"}
    notify.notify_from_env(env, radarrs=[r], sonarrs=[])(
        MOVIE_ROW, "/share/movies/Movie (2020)/File.mkv")
    assert r.posts[0][1]["movieId"] == 4


def test_falls_back_to_a_unique_imdb_id():
    r = Fake([{"id": 9, "path": "/elsewhere/x", "imdbId": "tt1"}, {"id": 10, "path": "/y"}])
    notify.notify_from_env({}, radarrs=[r], sonarrs=[])(MOVIE_ROW, "/data/m/File.mkv")
    assert r.posts[0][1]["movieId"] == 9


def test_no_owner_means_no_rescan():
    r = Fake([{"id": 3, "path": "/other"}])
    out = notify.notify_from_env({}, radarrs=[r], sonarrs=[])(
        {"id": "x", "kind": "movie"}, "/data/m/File.mkv")
    assert r.posts == [] and out["arr"].startswith("skipped")


def test_plex_skipped_without_a_client_and_noted():
    out = notify.notify_from_env({}, radarrs=[], sonarrs=[])(MOVIE_ROW, "/data/m/File.mkv")
    assert out["plex"].startswith("skipped") and out["arr"].startswith("skipped")


def test_plex_scans_the_folder_when_configured():
    seen = []
    out = notify.notify_from_env({"PLEX_URL": "http://p.invalid", "PLEX_TOKEN": TOKEN},
                                 radarrs=[], sonarrs=[],
                                 plex_scan=lambda f: seen.append(f) or "Movies")(
        MOVIE_ROW, "/data/movies/Movie (2020)/File.mkv")
    assert seen == ["/data/movies/Movie (2020)"] and out["plex"] == "scanned folder"


def test_a_failing_part_never_raises_and_the_rest_still_runs():
    class Down(Fake):
        def get(self, path, **params):
            raise register.ArrError("arr unreachable: OSError")
    seen = []
    out = notify.notify_from_env({}, radarrs=[Down([])], sonarrs=[],
                                 plex_scan=lambda f: seen.append(f) or "Movies")(
        MOVIE_ROW, "/data/m/File.mkv")
    assert out["arr"].startswith("skipped") and out["plex"] == "scanned folder"

    def boom(f):
        raise RuntimeError("x")
    out = notify.notify_from_env({}, radarrs=[], sonarrs=[], plex_scan=boom)(
        MOVIE_ROW, "/data/m/File.mkv")
    assert out["plex"] == "failed: RuntimeError"


class Resp:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self):
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return Resp()


def test_arr_post_sends_the_key_in_a_header_never_in_the_url():
    c = register.ArrClient("http://localhost:7878", KEY)
    c._opener = op = Opener()
    c.post("/api/v3/command", {"name": "RescanMovie", "movieId": 5})
    c.get("/api/v3/movie")
    assert len(op.reqs) == 2
    for req in op.reqs:
        assert KEY not in req.full_url
        assert dict(req.header_items())["X-api-key"] == KEY
    assert op.reqs[0].get_method() == "POST"
    assert json.loads(op.reqs[0].data) == {"name": "RescanMovie", "movieId": 5}
    assert KEY not in repr(c)


def test_plex_scan_path_uses_the_longest_section_and_a_header_token(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://p.invalid:32400")
    monkeypatch.setenv("PLEX_TOKEN", TOKEN)
    monkeypatch.setattr(plex_watch, "_sections", lambda: [
        {"key": "1", "title": "All", "Location": [{"path": "/data"}]},
        {"key": "2", "title": "Movies", "Location": [{"path": "/data/movies"}]}])
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return Resp()
    monkeypatch.setattr(plex_watch.urllib.request, "urlopen", urlopen)
    assert plex_watch.scan_path("/data/movies/Movie (2020)") == "Movies"
    req = seen[0]
    assert "/library/sections/2/refresh?" in req.full_url
    assert "path=%2Fdata%2Fmovies%2FMovie+%282020%29" in req.full_url
    assert TOKEN not in req.full_url
    assert dict(req.header_items())["X-plex-token"] == TOKEN
    assert plex_watch.scan_path("/elsewhere/x") is None
    assert len(seen) == 1


def test_plex_scan_path_needs_settings(monkeypatch):
    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)
    with pytest.raises(plex_watch.PlexError):
        plex_watch.scan_path("/data/x")
