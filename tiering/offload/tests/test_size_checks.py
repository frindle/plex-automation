"""Qualify's size checks: a same-titled listing of another size is no exact match, and an NZB
with no byte counts (or the wrong total) is never imported."""

import logging

import pytest

from fakes_intake import make_nzb
from test_qualify import IDS, REL, World
from tiering.offload import newznab as nz
from tiering.offload import qualify as q
from tiering.offload import runner


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _relist(w, **extra):
    """The fake listing: extra releases first (same title as ours), then the real one."""
    w.nz.releases = {**extra, **w.nz.releases}


# ---- fix 1: the listing size

def test_size_in_ratio_bounds():
    n = 1000
    assert q.size_in_ratio(int(n * q.SIZE_RATIO_MIN), n)
    assert q.size_in_ratio(int(n * q.SIZE_RATIO_MAX), n)
    assert not q.size_in_ratio(int(n * q.SIZE_RATIO_MIN) - 1, n)
    assert not q.size_in_ratio(int(n * q.SIZE_RATIO_MAX) + 1, n)
    assert q.size_in_ratio(0, n) and q.size_in_ratio(n * 9, None)  # unknown: not held against it


def test_find_exact_skips_a_same_title_listing_of_another_size():
    mk = lambda g, size: nz.Release(REL, g, size, "one", 1080, "l" + g)  # noqa: E731
    found = [mk("uhd", 3170), mk("ok", 1100), mk("small", 500)]
    assert [r.guid for r in q._find_exact(found, REL, 1000)] == ["ok"]
    assert [r.guid for r in q._find_exact(found, REL)] == ["uhd", "ok", "small"]


def test_wrong_edition_is_skipped_and_the_right_one_used(world):
    size = len(world.content)
    big = {"title": REL, "nzb": make_nzb(IDS, seg_bytes=size * 3 // len(IDS)), "size": size * 3}
    _relist(world, big=big)
    world.nz.releases["exact"]["size"] = size * 21 // 20
    r = world.run()
    assert r.ok
    assert "big" not in world.nz.gets and "exact" in world.nz.gets


def test_only_a_wrong_edition_listed_is_no_exact(world):
    size = len(world.content)
    world.nz.releases["exact"]["size"] = size * 3
    r = world.run()
    assert not r.ok and r.reasons == ["no_exact_nzb"]
    assert world.nz.gets == [] and world.am.uploads == []  # nothing grabbed, nothing imported


# ---- fix 2: the NZB itself

def _bad_world(world, **kw):
    world.nz.releases["exact"]["nzb"] = make_nzb(IDS, **kw)


def test_nzb_without_byte_counts_is_not_imported(world):
    _bad_world(world, seg_bytes=0)
    r = world.run()
    assert not r.ok and r.reasons == ["nzb_bad"]
    assert r.steps["nzb"]["why"] == "zero_size"
    assert world.am.uploads == [] and "nzb_path" not in r.fields


def test_nzb_of_the_wrong_total_is_not_imported(world):
    _bad_world(world, seg_bytes=len(world.content) * 3 // len(IDS))
    r = world.run()
    assert not r.ok and r.reasons == ["nzb_bad"]
    assert r.steps["nzb"]["why"] == "size_out_of_ratio"
    assert world.am.uploads == []


def test_an_archived_nzb_is_checked_too(world, tmp_path):
    from tiering.offload.newznab import archive_nzb
    path = archive_nzb(make_nzb(IDS, seg_bytes=0), world.archive, REL)
    r = world.run({**world.row, "nzb_path": path})
    assert not r.ok and r.reasons == ["nzb_bad"] and world.am.uploads == []


def test_a_bad_nzb_strikes_its_release(world):
    _bad_world(world, seg_bytes=0)
    seen = []
    orig = nz.NewznabClient.strike
    nz.NewznabClient.strike = lambda self, rel: (seen.append(rel.guid), orig(self, rel))[1]
    try:
        world.run()
    finally:
        nz.NewznabClient.strike = orig
    assert seen == ["exact"]


def test_strike_marks_bad_at_once():
    c = nz.NewznabClient([nz.Indexer("one", "http://x.invalid", "k")])
    rel = nz.Release("T", "g1", 1, "one", 1080, "http://x.invalid/g")
    assert not c.is_bad(rel)
    c.strike(rel)
    assert c.is_bad(rel)


def test_nzb_problem_ignores_par2_and_takes_the_largest_file():
    def nzb(*files):
        body = "".join(
            f'<file subject="{s}"><segments>'
            + "".join(f'<segment bytes="{b}" number="1">m{i}@t</segment>' for i, b in enumerate(bs))
            + "</segments></file>" for s, bs in files)
        return f'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{body}</nzb>'.encode()

    ok = nzb(("a.mkv yEnc", [500, 520]), ("a.vol0+1.PAR2 yEnc", [900_000]))
    assert q.nzb_problem(ok, 1000) is None
    assert q.nzb_problem(nzb(("a.mkv", [500, 520])), 3000) == "size_out_of_ratio"
    assert q.nzb_problem(nzb(("a.mkv", [0])), 1000) == "zero_size"
    assert q.nzb_problem(nzb(("a.par2", [1000])), 1000) == "size_out_of_ratio"
    # a release split into RAR volumes: no one file is near the local size, their sum is
    rar = nzb(*[(f"a.part{i:02d}.rar yEnc", [250]) for i in range(1, 5)],
              ("a.sfv yEnc", [10]), ("a.vol0+3.par2 yEnc", [300]))
    assert q.nzb_problem(rar, 1000) is None
    assert q.nzb_problem(rar, 3000) == "size_out_of_ratio"  # the sum is too small as well


# ---- fix 3: the @file allowlist is read again each pass

def _only_runner(tmp_path, monkeypatch, body):
    from test_runner import World as RW
    w = RW(tmp_path, monkeypatch)
    a, b = w.movie(1), w.movie(2)
    w.row("a", 1, a, "whole")
    w.row("b", 2, b, "whole")
    lst = tmp_path / "only.txt"
    lst.write_text(body(a, b))
    w.cfg.only_spec = "@" + str(lst)
    w.cfg.only_paths = runner.parse_only_paths(w.cfg.only_spec)
    return w, a, b, lst


def test_from_env_keeps_the_file_spec(tmp_path):
    lst = tmp_path / "o.txt"
    lst.write_text("/m/a.mkv\n")
    cfg = runner.Config.from_env({"TIERING_DB": "/x/t.db", "TIERING_ONLY_PATHS": "@" + str(lst)})
    assert cfg.only_spec == "@" + str(lst)
    assert runner.Config.from_env({"TIERING_DB": "/x/t.db",
                                   "TIERING_ONLY_PATHS": "/m/a.mkv"}).only_spec == ""


def test_the_file_is_read_again_each_pass(tmp_path, monkeypatch, caplog):
    w, a, b, lst = _only_runner(tmp_path, monkeypatch, lambda a, b: a + "\n")
    lst.write_text(b + "\n")  # edited after the start
    with caplog.at_level(logging.INFO, logger="tiering.runner"):
        w.run()
    assert w.ops("demote") == ["b"]
    assert "now 1 path(s)" in caplog.text and a not in caplog.text and b not in caplog.text


@pytest.mark.parametrize("after", [None, "# nothing\n,\n"])
def test_an_unusable_file_stops_demotes_that_pass(tmp_path, monkeypatch, after):
    w, a, b, lst = _only_runner(tmp_path, monkeypatch, lambda a, b: a + "\n")
    if after is None:
        lst.unlink()
    else:
        lst.write_text(after)
    w.run()
    assert w.ops("demote") == []
    lst.write_text(a + "\n")  # fixed: the next pass works again
    w.run()
    assert w.ops("demote") == ["a"]
