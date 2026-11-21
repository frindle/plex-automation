"""Seventh review of PR #82: one body that is no NZB is only a strike, in the restore step
and in the per-row streak. Fakes only."""
from test_qualify import World
from test_review_fixes4 import Clock, _kinds, _real, _weekly
from test_review_fixes5 import DAY, _c, _qualify
from tiering.offload import qualify as q
from tiering.offload import state as st

PAGE = b"<html><body>Down for maintenance</body></html>"


# ---------------------------------------------------------------- restore step
def test_G1_one_maintenance_page_is_unchecked_not_no_restore_path(tmp_path):
    w = World(tmp_path)
    w.nz.releases["alt"]["nzb"] = PAGE
    try:
        ix = _c(w.nz, Clock(10.0), Clock(5e6), str(tmp_path / "b.json"))
        r = _qualify(w, ix)
    finally:
        w.close()
    assert r.reasons == ["restore_check_unsure"]


def test_G1c_a_page_on_two_nights_is_no_restore_path(tmp_path):
    w = World(tmp_path)
    w.nz.releases["alt"]["nzb"] = PAGE
    mono, wall, sp = Clock(10.0), Clock(5e6), str(tmp_path / "b.json")
    try:
        first = _qualify(w, _c(w.nz, mono, wall, sp)).reasons
        mono.t += DAY
        wall.t += DAY
        second = _qualify(w, _c(w.nz, mono, wall, sp)).reasons
    finally:
        w.close()
    assert first == ["restore_check_unsure"]
    assert second == ["no_restore_path"]


def test_G1b_one_maintenance_page_in_weekly_never_promotes(tmp_path):
    e, db = _real(tmp_path, ["Alpha"])
    e.nz.releases["alt-Alpha"]["nzb"] = PAGE
    rid = st.list_files(db)[0]["id"]
    seen = []
    try:
        _weekly(e, db, on_promote=seen.append)
    finally:
        e.close()
    assert not seen and "weekly_flag" not in _kinds(db, rid)


# ---------------------------------------------------------------- per-row streak
def _rows(w, n):
    names = [f"Film{i}.2020.1080p.WEB-GRP" for i in range(n)]
    w.nz.releases.clear()
    for i, name in enumerate(names):
        w.nz.releases[f"g{i}"] = {"title": name, "nzb": PAGE}
    return [{"id": f"{i:016x}", "lib_path": str(w.lib), "release_name": name,
             "title": f"Film{i}", "year": 2020} for i, name in enumerate(names)]


def _nights(tmp_path, n_rows, n_nights):
    w = World(tmp_path, alt_title=None)
    rows = _rows(w, n_rows)
    mono, wall, sp = Clock(10.0), Clock(5e6), str(tmp_path / "b.json")
    out = []
    try:
        for _ in range(n_nights):
            ix = _c(w.nz, mono, wall, sp, grab_budget=100, search_budget=100)
            res = {}
            for row in q.oldest_first(rows, ix):
                mono.t += 60
                wall.t += 60
                w.row = row
                r = _qualify(w, ix)
                res[row["title"]] = (r.reasons[0], r.steps["nzb"].get("why"))
            out.append(res)
            mono.t += DAY
            wall.t += DAY
    finally:
        w.close()
    return out


def test_G2_indexer_wide_page_never_makes_a_row_a_miss(tmp_path):
    nights = _nights(tmp_path, 6, 4)
    misses = [t for night in nights for t, (reason, _) in night.items() if reason == "no_exact_nzb"]
    assert not misses


def test_G2b_a_release_with_no_nzb_on_every_night_is_still_called_a_miss(tmp_path):
    # two releases: too few for the indexer to be the problem, so the strikes confirm
    nights = _nights(tmp_path, 2, 4)
    assert all(reason == "nzb_unsure" for reason, _ in nights[0].values())
    assert all(v == ("no_exact_nzb", "grab_failed") for v in nights[-1].values())


# ---------------------------------------------------------------- clock set back
def test_G7_wall_clock_set_back_keeps_a_confirmed_mark(tmp_path):
    from fakes_intake import FakeNewznab
    s = FakeNewznab({"r": {"title": "Movie.2020.1080p.WEB-GRP", "nzb": PAGE}})
    sp = str(tmp_path / "b.json")
    mono, wall = Clock(10.0), Clock(5e6)
    try:
        ix = _c(s, mono, wall, sp)
        rel = ix.search_indexer(ix.indexers[0], title="Movie")[0]
        ix._mark_bad(rel)
        wall.t += 13 * 3600
        ix._mark_bad(rel)
        assert ix.is_bad(rel)
        wall.t -= 2 * DAY
        assert _c(s, mono, wall, sp).is_bad(rel)
    finally:
        s.close()
