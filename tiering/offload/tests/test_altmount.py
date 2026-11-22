import hashlib
import itertools
import os

import pytest

from fakes_intake import APIKEY, FakeAltMount, FakeDav, make_nzb
from tiering.offload import altmount as am

DATA = os.urandom(300_000)
MOVIE = "/movies/Movie.2020/Movie.2020.mkv"


@pytest.fixture
def dav():
    d = FakeDav({MOVIE: DATA, "/movies/Movie.2020/info.nfo": b"x"})
    yield d
    d.close()


def reader(dav, **kw):
    return am.WebDavReader(dav.url, kw.get("user", "u"), kw.get("password", "p"))


def test_read_range_uses_range_header(dav):
    r = reader(dav)
    assert r.read_range(MOVIE, 10, 100) == DATA[10:110]
    assert dav.range_requests == 1
    assert r.read_range(MOVIE, len(DATA) - 5, 100) == DATA[-5:]  # clipped at the end
    assert r.read_range(MOVIE, len(DATA) + 10, 5) == b""


def test_sha256_full_and_size(dav):
    r = reader(dav)
    assert r.sha256_full(MOVIE) == hashlib.sha256(DATA).hexdigest()
    assert r.size(MOVIE) == len(DATA)


def test_bad_password_is_auth_error_without_leaking(dav):
    r = reader(dav, password="wrong")
    with pytest.raises(am.AltMountAuthError) as e:
        r.read_range(MOVIE, 0, 10)
    assert "wrong" not in str(e.value) and "127.0.0.1" not in str(e.value)


def test_missing_file_is_error(dav):
    with pytest.raises(am.AltMountError):
        reader(dav).sha256_full("/nope.mkv")


def test_find_media_picks_largest_file_in_folder(dav):
    r = reader(dav)
    assert r.find_media("/movies/Movie.2020") == MOVIE
    assert r.find_media(MOVIE) == MOVIE


def test_env_config(monkeypatch, dav):
    monkeypatch.setenv("TIERFS_REMOTE_BASE", dav.url)
    monkeypatch.setenv("TIERFS_REMOTE_USER", "u")
    monkeypatch.setenv("TIERFS_REMOTE_PASS", "p")
    assert am.WebDavReader().read_range(MOVIE, 0, 4) == DATA[:4]


def importer(server, dav=None, **kw):
    return am.ImportClient(server.url, kw.pop("key", APIKEY),
                           reader=reader(dav) if dav else None,
                           mount_path="/mnt/am", poll_interval=0, sleep=lambda s: None, **kw)


def test_import_polls_until_done_and_resolves_media(dav):
    s = FakeAltMount("/mnt/am/movies/Movie.2020", pending_polls=2)
    try:
        path = importer(s, dav).import_nzb(make_nzb(["a@x"]), "Movie 2020/odd name")
    finally:
        s.close()
    assert path == MOVIE
    name, body = s.uploads[0]
    assert name.endswith(".nzb") and "/" not in name
    assert b"a@x" in body
    (job,) = s.jobs.values()
    assert job["polls"] == 3


def test_import_without_reader_returns_stripped_path():
    s = FakeAltMount("/mnt/am/movies/Movie.2020/Movie.2020.mkv", pending_polls=0)
    try:
        assert importer(s).import_nzb(make_nzb(["a@x"]), "n") == MOVIE
    finally:
        s.close()


def test_import_failed():
    s = FakeAltMount("", pending_polls=0, fail=True)
    try:
        with pytest.raises(am.ImportFailed):
            importer(s).import_nzb(make_nzb(["a@x"]), "n")
    finally:
        s.close()


def test_import_timeout():
    s = FakeAltMount("/mnt/am/x.mkv", pending_polls=10**9)
    tick = itertools.count()
    try:
        c = am.ImportClient(s.url, APIKEY, poll_interval=0, timeout=5, sleep=lambda x: None,
                            clock=lambda: next(tick))
        with pytest.raises(am.ImportTimeout):
            c.import_nzb(make_nzb(["a@x"]), "n")
    finally:
        s.close()


def test_wrong_key_does_not_leak_key():
    s = FakeAltMount("x")
    try:
        with pytest.raises(am.ImportFailed) as e:
            importer(s, key="z" * 32).import_nzb(make_nzb(["a@x"]), "n")
    finally:
        s.close()
    assert "z" * 32 not in str(e.value)


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("ALTMOUNT_URL", raising=False)
    monkeypatch.delenv("ALTMOUNT_API_KEY", raising=False)
    with pytest.raises(am.AltMountError):
        am.ImportClient()
