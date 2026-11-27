import gzip
import hashlib

import pytest

from fakes_intake import FakeNewznab, make_nzb
from tiering.offload import newznab as nz


def client(server, key="nzkey"):
    return nz.NewznabClient([nz.Indexer("one", server.url, key)])


def test_search_parses_items_and_params():
    s = FakeNewznab({"g1": {"title": "Movie.2020.2160p.UHD.BluRay-A", "nzb": make_nzb(["a@x"])},
                     "g2": {"title": "Movie.2020.720p.WEB-B", "nzb": make_nzb(["b@x"])}})
    try:
        rels = client(s).search(title="Movie", imdb_id="tt1234567")
    finally:
        s.close()
    assert {r.title: r.resolution for r in rels} == {
        "Movie.2020.2160p.UHD.BluRay-A": 2160, "Movie.2020.720p.WEB-B": 720}
    q = s.queries[0]
    assert q["t"] == "movie" and q["q"] == "Movie" and q["imdbid"] == "1234567"


def test_tv_search_params():
    s = FakeNewznab({})
    try:
        client(s).search(title="Show", tvdb_id=99, season=2, episode=5)
    finally:
        s.close()
    q = s.queries[0]
    assert (q["t"], q["tvdbid"], q["season"], q["ep"]) == ("tvsearch", "99", "2", "5")


def test_tv_search_sends_tvdbid_not_q_or_imdbid():
    # 0192: the indexer returned no episodes when q and imdbid rode along with tvdbid.
    s = FakeNewznab({})
    try:
        client(s).search(title="Show", imdb_id="tt0001", tvdb_id=99, season=2, episode=5)
    finally:
        s.close()
    q = s.queries[0]
    assert q["t"] == "tvsearch" and q["tvdbid"] == "99" and (q["season"], q["ep"]) == ("2", "5")
    assert "q" not in q and "imdbid" not in q


def test_tv_search_without_tvdbid_uses_title():
    s = FakeNewznab({})
    try:
        client(s).search(title="Show", imdb_id="tt0001", season=2, episode=5)
    finally:
        s.close()
    q = s.queries[0]
    assert (q["t"], q["q"], q["season"], q["ep"]) == ("tvsearch", "Show", "2", "5")
    assert "imdbid" not in q and "tvdbid" not in q


def test_fetch_nzb_and_bad_body():
    nzb = make_nzb(["a@x"])
    s = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": gzip.compress(nzb)},
                     "g2": {"title": "Movie.2020.1080p-B", "nzb": b"<html>nope</html>"}})
    try:
        c = client(s)
        by = {r.title: r for r in c.search(title="Movie")}
        assert c.fetch_nzb(by["Movie.2020.1080p-A"]) == nzb
        with pytest.raises(nz.NewznabError):
            c.fetch_nzb(by["Movie.2020.1080p-B"])
    finally:
        s.close()


def test_requests_send_own_user_agent_not_pythons():
    # an indexer behind Cloudflare 403s urllib's default "Python-urllib/3.x" on every call
    s = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": make_nzb(["a@x"])}})
    try:
        c = client(s)
        (rel,) = c.search(title="Movie")
        c.fetch_nzb(rel)
    finally:
        s.close()
    assert len(s.agents) == 2 and set(s.agents) == {nz.USER_AGENT}
    assert "python" not in nz.USER_AGENT.lower()


def test_bad_key_and_down_indexer_are_errors_without_leaking():
    s = FakeNewznab({"g1": {"title": "t", "nzb": b"x"}})
    try:
        with pytest.raises(nz.NewznabError) as e:
            client(s, key="secretkey").search(title="t")
        assert "secretkey" not in str(e.value) and "127.0.0.1" not in str(e.value)
        s.fail = True
        with pytest.raises(nz.NewznabError):
            client(s).search(title="t")
    finally:
        s.close()


def test_one_failing_indexer_is_skipped():
    good = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": b"x"}})
    bad = FakeNewznab({})
    bad.fail = True
    try:
        c = nz.NewznabClient([nz.Indexer("bad", bad.url, "nzkey"), nz.Indexer("good", good.url, "nzkey")])
        assert len(c.search(title="Movie")) == 1
    finally:
        good.close()
        bad.close()


def test_no_indexers_is_an_error_not_an_empty_list():
    with pytest.raises(nz.NewznabError):
        nz.NewznabClient([]).search(title="x")


def test_non_http_link_refused():
    c = nz.NewznabClient([nz.Indexer("one", "http://127.0.0.1:1", "k")])
    with pytest.raises(nz.NewznabError):
        c.fetch_nzb(nz.Release("t", "g", 1, "one", 1080, "file:///etc/passwd"))


def test_load_indexers_from_env(monkeypatch):
    monkeypatch.setenv("NEWZNAB_INDEXERS", '[{"name":"a","url":"http://h","apikey":"k"}]')
    (ix,) = nz.load_indexers()
    assert ix.name == "a" and "k" not in repr(ix) and "http://h" not in repr(ix)
    monkeypatch.setenv("NEWZNAB_INDEXERS", "not json")
    with pytest.raises(nz.NewznabError):
        nz.load_indexers()


@pytest.mark.parametrize("title,res", [
    ("Movie.2020.2160p.WEB-DL", 2160), ("Movie 2020 4K UHD", 2160), ("Movie.2020.1080p.BluRay", 1080),
    ("Show.S01E02.720p.HDTV", 720), ("Movie.2020.DVDRip", 0), ("Movie.1080i.x", 1080),
    ("Movie.2020.x264-GRP", 0)])
def test_parse_resolution(title, res):
    assert nz.parse_resolution(title) == res


def test_title_matches():
    m = nz.title_matches
    assert m("The.Thing.1982.1080p.BluRay", "The Thing", 1982)
    assert not m("The.Thing.2011.1080p.BluRay", "The Thing", 1982)
    assert not m("The.Thing.From.Another.World.1951.1080p", "The Thing", 1951)
    assert m("Show.2019.S01E01.1080p", "Show", None, 1, 1)
    assert not m("Show.Name.S01E01.1080p", "Show", None, 1, 1)
    assert not m("Other.Movie.2020.1080p", "Movie", 2020)
    assert m("Show.S02E05.1080p.WEB", "Show", None, 2, 5)
    assert not m("Show.S02E06.1080p.WEB", "Show", None, 2, 5)
    assert not m("Show.S02E055.1080p.WEB", "Show", None, 2, 5)
    assert m("Greys.Anatomy.S03E04.1080p", "Grey's Anatomy", None, 3, 4)
    assert m("Bobs.Burgers.S01E01.720p", "Bob’s Burgers", None, 1, 1)
    assert m("Law.and.Order.S01E02.1080p", "Law & Order", None, 1, 2)
    assert not m("Greys.S03E04.1080p", "Grey's Anatomy", None, 3, 4)


def test_archive_nzb_gzip_sha_in_name_and_idempotent(tmp_path):
    raw = make_nzb(["a@x", "b@x"])
    p = nz.archive_nzb(raw, str(tmp_path / "arch"), "Movie 2020/odd:name")
    assert p.endswith(f".{hashlib.sha256(raw).hexdigest()}.nzb.gz")
    assert "/odd" not in p.split("arch/")[1]
    assert gzip.decompress(open(p, "rb").read()) == raw
    assert nz.read_archived(p) == raw
    assert nz.archive_nzb(raw, str(tmp_path / "arch"), "Movie 2020/odd:name") == p
    assert nz.nzb_segment_ids(raw) == frozenset({"a@x", "b@x"})
