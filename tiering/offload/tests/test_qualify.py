import os

import pytest

from fakes_intake import (APIKEY, COLUMNS, FakeAltMount, FakeDav, FakeNewznab, FakeNntp,
                          make_nzb, sha)
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q

REL = "Movie.2020.1080p.BluRay-GRP"
IDS = [f"s{i}@t" for i in range(40)]
ALT = "Movie.2020.1080p.WEB-DL-ALT"


class World:
    def __init__(self, tmp_path, alt_title=ALT, content=None):
        self.content = content or os.urandom(50_000)
        self.nzb = make_nzb(IDS)
        self.dav = FakeDav({f"/movies/{REL}/{REL}.mkv": self.content})
        self.am = FakeAltMount(f"/mnt/am/movies/{REL}", pending_polls=1)
        rels = {"exact": {"title": REL, "nzb": self.nzb}}
        if alt_title:
            rels["alt"] = {"title": alt_title, "nzb": make_nzb([f"alt{i}@t" for i in range(5)])}
        self.nz = FakeNewznab(rels)
        self.prim, self.sec = FakeNntp(IDS), FakeNntp(IDS)
        self.lib = tmp_path / "lib.mkv"
        self.lib.write_bytes(self.content)  # L: what is on disk now
        self.row = {"id": "a" * 16, "lib_path": str(self.lib), "release_name": REL,
                    "title": "Movie", "year": 2020}
        self.archive = str(tmp_path / "arch")

    def run(self, row=None):
        reader = am.WebDavReader(self.dav.url, "u", "p")
        importer = am.ImportClient(self.am.url, APIKEY, reader=reader, mount_path="/mnt/am",
                                   poll_interval=0, sleep=lambda s: None)
        indexers = nz.NewznabClient([nz.Indexer("one", self.nz.url, "nzkey")])
        p, s = ns.Checker(self.prim.cfg()), ns.Checker(self.sec.cfg())
        try:
            return q.qualify(row or self.row, reader=reader, importer=importer, indexers=indexers,
                             stat_primary=p, stat_secondary=s, archive_dir=self.archive)
        finally:
            p.close()
            s.close()

    def close(self):
        for x in (self.dav, self.am, self.nz, self.prim, self.sec):
            x.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def test_perfect_fixture_passes(world):
    r = world.run()
    assert r.ok and r.reasons == []
    f = r.fields
    assert os.path.exists(f["nzb_path"]) and f["nzb_path"].startswith(world.archive)
    assert f["remote_path"] == f"/movies/{REL}/{REL}.mkv"
    assert f["restore_score"] == 1
    assert "best 1080p" in f["restore_note"] and "127.0.0.1" not in f["restore_note"]
    assert f["sha256"] == sha(world.content)
    assert set(f) <= set(COLUMNS)  # only files columns
    assert all(s["ok"] for s in r.steps.values())
    assert set(r.steps) == {"nzb", "import", "hash", "stat_secondary", "stat_primary", "restore"}
    assert world.sec.stats == 40 and world.prim.stats == 40


def test_wrong_hash_simulating_zero_fill_fails_at_hash(world):
    bad = bytearray(world.content)
    bad[20_000:30_000] = b"\0" * 10_000  # what AltMount serves for lost segments, still 206
    world.dav.files[f"/movies/{REL}/{REL}.mkv"] = bytes(bad)
    r = world.run()
    assert not r.ok and r.reasons == ["hash_mismatch"]
    assert world.sec.stats == 0 and world.prim.stats == 0  # stopped at the first failure
    assert "restore_score" not in r.fields


def test_one_missing_segment_on_secondary(world):
    world.sec.ids.discard("s17@t")
    r = world.run()
    assert not r.ok and r.reasons == ["missing_segments_secondary"]
    assert r.steps["stat_secondary"]["missing"] == 1
    assert world.prim.stats == 0


def test_one_missing_segment_on_primary(world):
    world.prim.ids.discard("s3@t")
    r = world.run()
    assert not r.ok and r.reasons == ["missing_segments_primary"]


def test_stat_login_refused_is_not_a_pass(world):
    world.sec.refuse_auth = True
    r = world.run()
    assert not r.ok and r.reasons == ["stat_unavailable_secondary"]
    assert r.steps["stat_secondary"]["kind"] == "auth"


def test_no_alternative_release(tmp_path):
    w = World(tmp_path, alt_title=None)
    try:
        r = w.run()
    finally:
        w.close()
    assert not r.ok and r.reasons == ["no_restore_path"]
    assert r.fields["restore_score"] == 0
    assert all(r.steps[k]["ok"] for k in ("hash", "stat_secondary", "stat_primary"))


def test_only_alternative_below_floor(tmp_path):
    w = World(tmp_path, alt_title="Movie.2020.720p.WEB-DL-LOW")
    try:
        r = w.run()
    finally:
        w.close()
    assert not r.ok and r.reasons == ["no_restore_path"]
    assert r.steps["restore"]["below_floor"] == 1
    assert "below-floor" in r.fields["restore_note"]


def test_no_exact_nzb_stops_before_import(world):
    world.nz.releases.pop("exact")
    r = world.run()
    assert not r.ok and r.reasons == ["no_exact_nzb"]
    assert world.am.uploads == []


def test_import_failure(world):
    world.am.fail = True
    r = world.run()
    assert not r.ok and r.reasons == ["import_failed"]
    assert world.dav.range_requests == 0


def test_existing_archived_nzb_is_reused(world, tmp_path):
    path = nz.archive_nzb(world.nzb, str(tmp_path / "old"), REL)
    world.nz.releases.pop("exact")  # indexer no longer lists it
    r = world.run({**world.row, "nzb_path": path})
    assert r.ok and r.fields["nzb_path"] == path
    assert r.steps["nzb"]["reused"] is True


# restore floor rules, tested directly

def idx(*releases):
    s = FakeNewznab({f"g{i}": {"title": t, "nzb": n} for i, (t, n) in enumerate(releases)})
    return s, nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")])


@pytest.mark.parametrize("src,alt,expect", [
    (2160, "Movie.2020.1080p.WEB-DL-A", 1),
    (2160, "Movie.2020.2160p.WEB-DL-A", 1),
    (2160, "Movie.2020.720p.WEB-DL-A", 0),
    (1080, "Movie.2020.1080p.WEB-DL-A", 1),
    (1080, "Movie.2020.720p.WEB-DL-A", 0),
    (720, "Movie.2020.720p.WEB-DL-A", 1),
    (720, "Movie.2020.480p.DVDRip-A", 0),
    (480, "Movie.2020.720p.WEB-DL-A", 1),
    (1080, "Movie.2020.WEB-DL-NORES", 0),
    (1080, "Movie.2019.1080p.WEB-DL-A", 0),
    (1080, "Other.Movie.2020.1080p.WEB-DL-A", 0),
])
def test_resolution_floor(src, alt, expect):
    s, ix = idx((alt, make_nzb(["z@t"])))
    try:
        row = {"release_name": f"Movie.2020.{src}p.X-GRP", "title": "Movie", "year": 2020}
        assert q.score_restore(row, ix, frozenset(IDS))["count"] == expect
    finally:
        s.close()


def test_same_release_other_indexer_same_nzb_does_not_count_but_different_nzb_does():
    own = make_nzb(IDS)
    s1, ix1 = idx((REL, own))
    s2, ix2 = idx((REL, own))
    s3, ix3 = idx((REL, make_nzb(["other@t"])))
    try:
        both = nz.NewznabClient(ix1.indexers + ix2.indexers)
        row = {"release_name": REL, "title": "Movie", "year": 2020}
        assert q.score_restore(row, both, frozenset(IDS))["count"] == 0
        both = nz.NewznabClient(ix1.indexers + ix3.indexers)
        assert q.score_restore(row, both, frozenset(IDS))["count"] == 1
    finally:
        for s in (s1, s2, s3):
            s.close()


def test_duplicate_alternatives_on_two_indexers_count_once():
    n = make_nzb(["a@t"])
    s1, ix1 = idx((ALT, n))
    s2, ix2 = idx((ALT, n))
    try:
        both = nz.NewznabClient(ix1.indexers + ix2.indexers)
        row = {"release_name": REL, "title": "Movie", "year": 2020}
        assert q.score_restore(row, both, frozenset(IDS))["count"] == 1
    finally:
        s1.close()
        s2.close()


def test_tv_matches_episode_only():
    s, ix = idx(("Show.S01E02.1080p.WEB-A", make_nzb(["a@t"])),
                ("Show.S01E03.1080p.WEB-B", make_nzb(["b@t"])),
                ("Show.S01.1080p.WEB.PACK", make_nzb(["c@t"])))
    try:
        row = {"release_name": "Show.S01E02.1080p.BluRay-X", "title": "Show", "season": 1,
               "episode": 2}
        sc = q.score_restore(row, ix, frozenset(IDS))
        assert sc["count"] == 1 and sc["best"] == 1080
    finally:
        s.close()


def test_identity_parses_release_name():
    i = q.identity({"release_name": "The.Big.Movie.2019.1080p.BluRay-X"})
    assert (i["title"], i["year"]) == ("The Big Movie", 2019)
    i = q.identity({"release_name": "Some.Show.S03E11.720p.HDTV-X"})
    assert (i["title"], i["season"], i["episode"]) == ("Some Show", 3, 11)
