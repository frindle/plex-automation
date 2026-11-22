"""Sixth review of PR #82: regressions the round-5 changes could cause. Fakes only."""
import collections
import multiprocessing
import os
import time

from fakes_intake import FakeNewznab, make_nzb
from test_qualify import REL, World
from test_review_fixes4 import Clock, _try_search
from test_review_fixes5 import DAY, _c, _qualify
from tiering.offload import newznab as nz
from tiering.offload import qualify as q

PAGE = b"<html><body>Down for maintenance</body></html>"
GONE = b'<?xml version="1.0"?><error code="300" description="No such item"/>'


def _nights(w, sp, n, before=None):
    mono, wall = Clock(10.0), Clock(5e6)
    out = []
    for night in range(n):
        if before:
            before(night)
        r = _qualify(w, _c(w.nz, mono, wall, sp))
        out.append((r.reasons, r.steps.get("nzb", {}).get("why"), list(w.nz.gets)))
        mono.t += DAY + 1
        wall.t += DAY + 1
    return out


def test_N1_known_bad_dupes_use_up_the_grab_tries_and_hide_a_good_listing(tmp_path):
    """Three listings with no NZB, then a good one. A known-bad release costs no grab but
    still counts toward MAX_FAILED_GRABS, so the good fourth listing is never asked for and
    on the third night the row is called a miss."""
    w = World(tmp_path, alt_title=None)
    good = w.nzb
    w.nz.releases.clear()
    for i in range(3):
        w.nz.releases[f"d{i}"] = {"title": REL, "nzb": PAGE}
    w.nz.releases["d3"] = {"title": REL, "nzb": good}
    try:
        out = _nights(w, str(tmp_path / "b.json"), 3)
    finally:
        w.close()
    print(*out, sep="\n")
    assert "d3" in w.nz.gets, f"good listing never grabbed; nights: {[o[:2] for o in out]}"


def test_N2_one_bad_answer_marks_a_release_bad_for_good(tmp_path):
    """Night 1 the grab gets a maintenance page (200, HTML); from night 2 the indexer serves
    the NZB again. The release is never asked for again and night 3 calls it a miss."""
    w = World(tmp_path, alt_title=None)
    good = w.nzb

    def before(night):
        w.nz.releases["exact"]["nzb"] = PAGE if night == 0 else good

    try:
        out = _nights(w, str(tmp_path / "b.json"), 3, before)
    finally:
        w.close()
    print(*out, sep="\n")
    assert any(o[0] != ["nzb_unsure"] and o[0] != ["no_exact_nzb"] for o in out), \
        f"one bad answer decided the row: {[o[:2] for o in out]}"


def test_N2b_a_site_wide_html_page_never_drops_and_poisons_every_release(tmp_path):
    rels = {f"r{i}": {"title": f"Movie.{i}.2020.1080p.WEB-GRP", "nzb": PAGE} for i in range(6)}
    s = FakeNewznab(rels)
    sp = str(tmp_path / "b.json")
    try:
        ix = _c(s, Clock(10.0), Clock(5e6), sp)
        got = []
        for rel in ix.search_indexer(ix.indexers[0], title="Movie"):
            try:
                ix.fetch_nzb(rel)
                got.append("ok")
            except nz.NewznabError as e:
                got.append(type(e).__name__)
        dropped = "one" in ix._dropped_until
        bad = len(ix._bad)
    finally:
        s.close()
    print("grabs:", got, "dropped:", dropped, "marked bad for good:", bad)
    assert dropped and bad == 0, "an indexer-wide error page marks releases bad instead of dropping the indexer"


def test_N3_an_alternative_with_no_nzb_keeps_the_restore_step_unsure_for_ever(tmp_path):
    """The exact NZB is fine; the one alternative listing has no NZB (code 300). It is known
    bad after night 1, yet score_restore counts it as 'could not check' every night."""
    w = World(tmp_path)
    w.nz.releases["alt"]["nzb"] = GONE
    try:
        out = _nights(w, str(tmp_path / "b.json"), 4)
    finally:
        w.close()
    print(*[(o[0], o[2].count("alt")) for o in out], sep="\n")
    assert any(o[0] != ["restore_check_unsure"] for o in out), "restore step is 'try later' for ever"


def test_N4_a_row_with_a_stored_nzb_is_never_recorded_as_attempted(tmp_path):
    """A row that already has nzb_path (reused) and fails later (here: hash mismatch) is
    never passed to note_attempt, so oldest_first puts it first every night."""
    w = World(tmp_path, alt_title=None)
    os.makedirs(w.archive, exist_ok=True)
    w.row = dict(w.row, nzb_path=nz.archive_nzb(w.nzb, w.archive, REL))
    w.lib.write_bytes(b"not the same bytes")
    other = {"release_name": "Other.2019.1080p.WEB-GRP", "title": "Other", "year": 2019}
    try:
        ix = _c(w.nz, Clock(10.0), Clock(5e6), str(tmp_path / "b.json"))
        ix.note_attempt(other["release_name"], "keep")  # another row tried earlier tonight
        r = _qualify(w, ix)
        order = [x.get("release_name") for x in q.oldest_first([other, w.row], ix)]
        info = ix.attempt_info(REL)
    finally:
        w.close()
    print(r.reasons, r.steps.get("nzb"), info, order)
    assert info[0] > 0, "a reused row's attempt is never recorded: it stays first in oldest_first"


def _big_state(sp, n=20000):
    s = FakeNewznab({})
    try:
        ix = _c(s, Clock(10.0), Clock(5e6), sp)
        with ix._sync():
            ix._attempts = {f"Some.Movie.Title.{i}.2020.1080p.BluRay.x264-GRP": (5e6 - i, 0)
                            for i in range(n)}
            ix._bad = collections.OrderedDict(
                (f"one|https://indexer.invalid/details/{i:040x}", [5e6 - 9e4, 5e6]) for i in range(n))
            ix._save_state()
    finally:
        s.close()
    return os.path.getsize(sp)


def test_N5_oldest_first_does_not_reload_the_state_file_per_row(tmp_path):
    """1000 rows on a ~3 MB state: the file is read once, not once per row."""
    sp = str(tmp_path / "b.json")
    size = _big_state(sp)
    assert size > 2_500_000
    s = FakeNewznab({})
    try:
        ix = _c(s, Clock(10.0), Clock(5e6), sp)
        rows = [{"release_name": f"Some.Movie.Title.{i}.2020.1080p.BluRay.x264-GRP"}
                for i in range(1000)]
        t0 = time.perf_counter()
        q.oldest_first(rows, ix)
        t_order = time.perf_counter() - t0
    finally:
        s.close()
    print(f"state file {size / 1e6:.1f} MB; oldest_first 1000 rows {t_order:.3f}s")
    assert t_order < 2, f"oldest_first reloads the file per row: {t_order:.1f}s for 1000 rows"


def test_N5b_oldest_first_orders_by_last_attempt_never_first(tmp_path):
    s = FakeNewznab({})
    wall = Clock(5e6)
    try:
        ix = _c(s, Clock(10.0), wall, str(tmp_path / "b.json"))
        for name in ("B.2020.1080p-X", "A.2020.1080p-X"):  # B tried first, then A
            wall.t += 10
            ix.note_attempt(name, "keep")
        rows = [{"release_name": n} for n in ("A.2020.1080p-X", "B.2020.1080p-X", "C.2020.1080p-X")]
        got = [r["release_name"] for r in q.oldest_first(rows, ix)]
    finally:
        s.close()
    assert got == ["C.2020.1080p-X", "B.2020.1080p-X", "A.2020.1080p-X"]


def test_N5c_a_change_by_another_client_is_seen_without_a_write(tmp_path):
    s = FakeNewznab({})
    sp = str(tmp_path / "b.json")
    try:
        a = _c(s, Clock(10.0), Clock(5e6), sp)
        b = _c(s, Clock(10.0), Clock(5e6), sp)
        assert a.attempt_info("X.2020")[0] == 0
        b.note_attempt("X.2020", "keep")
        assert a.attempt_info("X.2020")[0] > 0
    finally:
        s.close()


def test_N7_a_release_is_bad_only_after_two_nights_and_the_mark_expires(tmp_path):
    s = FakeNewznab({"r": {"title": "Movie.2020.1080p.WEB-GRP", "nzb": PAGE}})
    sp = str(tmp_path / "b.json")
    mono, wall = Clock(10.0), Clock(5e6)
    try:
        def grab():
            ix = _c(s, mono, wall, sp)
            rel = ix.search_indexer(ix.indexers[0], title="Movie")[0]
            try:
                ix.fetch_nzb(rel)
                return "ok"
            except nz.NewznabError as e:
                return type(e).__name__

        got = [grab()]
        wall.t += 3600  # the same night: still one strike
        got.append(grab())
        wall.t += DAY
        got.append(grab())  # a second night: now bad
        wall.t += DAY
        got.append(grab())  # known bad, not asked for
        wall.t += 8 * DAY   # expired
        got.append(grab())
        s.releases["r"]["nzb"] = make_nzb(["<a@x>"])
        wall.t += DAY
        got.append(grab())  # a valid NZB again
    finally:
        s.close()
    assert got[:5] == ["NotNzb", "NotNzb", "NotNzb", "BadRelease", "NotNzb"], got
    assert got[5] == "ok", got


def _worker(url, key, sp, out):
    ix = nz.NewznabClient([nz.Indexer("one", url, key)], sleep=lambda s: None, state_path=sp,
                          search_budget=3, min_interval=0, grab_interval=0)
    out.put([_try_search(ix) for _ in range(4)])


def test_N6_two_processes_share_one_budget(tmp_path):
    s = FakeNewznab({})
    sp = str(tmp_path / "b.json")
    ctx = multiprocessing.get_context("fork")
    out = ctx.Queue()
    try:
        ps = [ctx.Process(target=_worker, args=(s.url, s.key, sp, out)) for _ in range(3)]
        for p in ps:
            p.start()
        res = [out.get(timeout=60) for _ in ps]
        for p in ps:
            p.join(60)
    finally:
        s.close()
    print(res, "requests:", len(s.queries))
    assert len(s.queries) == 3
