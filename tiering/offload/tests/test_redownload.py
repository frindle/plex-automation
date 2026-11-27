"""Automatic re-download of a lost file: the arr calls, the guards and the tracking."""
import pytest

from fakes_intake import FakeArr, FakeState
from tiering.offload import redownload as rd
from tiering.offload.register import ArrClient

DAY = 86400.0
NOW = 1_800_000_000.0
LIB = "/share/Movies/Film (2020)/Film.mkv"


def movie(mid=1, fid=10, path=LIB, monitored=True):
    return {"id": mid, "title": f"Film {mid}", "monitored": monitored, "hasFile": True,
            "movieFile": {"id": fid, "path": path}}


class W:
    """One row, one arr, a kv dict and an alert list."""

    def __init__(self, arrs=None, ids=("a",), **kw):
        self.state = FakeState([{"id": i, "state": "offloaded", "lib_path": LIB if i == "a"
                                 else f"/share/Movies/M{i}/M{i}.mkv"} for i in ids])
        self.kv, self.alerts, self.arr = {}, [], arrs
        self.l_ok_ids = set(ids)
        self.busy_ids = set()
        self.healthy = True
        self.clock_now = NOW
        self.rd = rd.Redownload(
            state=self.state, arrs=arrs, l_ok=lambda row: row["id"] in self.l_ok_ids,
            busy=lambda id: id in self.busy_ids, healthy=lambda: self.healthy,
            alert=lambda kind, id, **d: self.alerts.append((kind, id)),
            kv_get=lambda db, k, d=None: self.kv.get(k, d),
            kv_set=lambda db, k, v: self.kv.__setitem__(k, v),
            clock=lambda: self.clock_now, **kw)

    def kinds(self):
        return [e[1] for e in self.state.events]

    def process(self, lost, trouble=False, now=None):
        self.rd.process(None, lost, {"provider_trouble": trouble, "finished": True},
                        now=NOW if now is None else now)


@pytest.fixture
def radarr():
    a = FakeArr(movies=[movie()])
    yield a
    a.close()


def arrs_for(*fakes, sonarr=False):
    clients = [ArrClient(f.url, f.key) for f in fakes]
    return rd.Arrs([] if sonarr else clients, clients if sonarr else [])


LOST_A = [{"id": "a", "title": "Film", "articles": 3}]


def test_lost_row_deletes_arr_file_monitors_and_searches(radarr):
    radarr.movies[0]["monitored"] = False
    w = W(arrs_for(radarr))
    w.process(LOST_A)
    assert radarr.deleted == ["/api/v3/moviefile/10"]
    assert [p for p, _ in radarr.puts] == ["/api/v3/movie/1"]
    assert radarr.movies[0]["monitored"] is True
    assert radarr.commands == [{"name": "MoviesSearch", "movieIds": [1]}]
    row = w.state.rows["a"]
    assert row["state"] == "failed" and row["restore_note"].startswith("lost: re-download requested")
    assert {"redownload_requested", "redownload_monitored"} <= set(w.kinds())
    assert ("redownload_requested", "a") in w.alerts


def test_monitored_item_is_not_touched(radarr):
    w = W(arrs_for(radarr))
    w.process(LOST_A)
    assert radarr.puts == [] and "redownload_monitored" not in w.kinds()


def test_sonarr_with_several_instances_acts_on_the_right_one():
    ep = {"id": 7, "monitored": True, "hasFile": True,
          "episodeFile": {"id": 70, "path": LIB}}
    other = {"id": 8, "monitored": True, "hasFile": True,
             "episodeFile": {"id": 80, "path": "/share/TV/Other/S01E01.mkv"}}
    s1 = FakeArr(series=[{"id": 1, "title": "Other", "path": "/share/TV/Other"}],
                 episodes={1: [other]})
    s2 = FakeArr(series=[{"id": 1, "title": "Film Show", "path": "/share/Movies/Film (2020)"}],
                 episodes={1: [ep]})
    try:
        w = W(arrs_for(s1, s2, sonarr=True))
        w.process(LOST_A)
        assert s1.deleted == [] and s1.commands == []
        assert s2.deleted == ["/api/v3/episodefile/70"]
        assert s2.commands == [{"name": "EpisodeSearch", "episodeIds": [7]}]
    finally:
        s1.close()
        s2.close()


def test_arr_path_map_is_used():
    a = FakeArr(movies=[movie(path="/movies/Film (2020)/Film.mkv")])
    try:
        arrs = rd.Arrs([ArrClient(a.url, a.key)], [], arr_map=[("/movies", "/share/Movies")])
        w = W(arrs)
        w.process(LOST_A)
        assert a.deleted == ["/api/v3/moviefile/10"]
    finally:
        a.close()


def test_guard_provider_trouble_does_nothing(radarr):
    w = W(arrs_for(radarr))
    w.process(LOST_A, trouble=True)
    assert radarr.deleted == [] and radarr.commands == []
    assert w.state.rows["a"]["state"] == "offloaded"
    assert "a" in w.rd.pending(None), "kept for a clean night"


def test_guard_altmount_down_does_nothing(radarr):
    w = W(arrs_for(radarr))
    w.healthy = False
    w.process(LOST_A)
    assert radarr.deleted == []
    w.healthy = True
    w.process([], now=NOW + DAY)
    assert radarr.deleted == ["/api/v3/moviefile/10"], "acted on the next clean night"


def test_guard_max_per_night_stops_at_the_cap():
    a = FakeArr(movies=[movie(i, 10 + i, f"/share/Movies/M{i}/M{i}.mkv") for i in range(1, 4)])
    try:
        w = W(arrs_for(a), ids=("1", "2", "3"), max_per_night=2)
        w.process([{"id": i, "title": i, "articles": 1} for i in ("1", "2")])
        assert len(a.deleted) == 2
        w.process([{"id": "3", "title": "3", "articles": 1}], now=NOW + 3600)
        assert len(a.deleted) == 2, "the cap is per night"
        w.process([], now=NOW + DAY)
        assert len(a.deleted) == 3
    finally:
        a.close()


def test_mass_loss_acts_on_none_and_alerts():
    a = FakeArr(movies=[movie(i, 10 + i, f"/share/Movies/M{i}/M{i}.mkv") for i in range(1, 5)])
    try:
        ids = ("1", "2", "3", "4")
        w = W(arrs_for(a), ids=ids, max_per_night=3)
        w.process([{"id": i, "title": i, "articles": 1} for i in ids])
        assert a.deleted == [] and a.commands == []
        assert ("mass_loss", "-") in w.alerts
        assert w.kinds().count("mass_loss") == 4
        assert all(w.state.rows[i]["state"] == "offloaded" for i in ids)
        w.process([], now=NOW + 60)  # the same day: no second alert
        assert [x for x in w.alerts if x[0] == "mass_loss"] == [("mass_loss", "-")]
    finally:
        a.close()


def test_l_not_our_symlink_deletes_nothing(radarr):
    w = W(arrs_for(radarr))
    w.l_ok_ids = set()  # a real file, or another link, is at L
    w.process(LOST_A)
    assert radarr.deleted == [] and radarr.commands == []
    assert "redownload_skipped" in w.kinds()
    assert w.state.rows["a"]["state"] == "offloaded"
    assert "a" not in w.rd.pending(None)


def test_l_changes_during_the_lookup_deletes_nothing(radarr):
    w = W(arrs_for(radarr))
    real = w.rd.arrs.find

    def find(p):
        out = real(p)
        w.l_ok_ids = set()  # an arr upgrade replaced L while we looked
        return out

    w.rd.arrs.find = find
    w.process(LOST_A)
    assert radarr.deleted == []


def test_running_promote_defers_the_row(radarr):
    w = W(arrs_for(radarr))
    w.busy_ids = {"a"}
    w.process(LOST_A)
    assert radarr.deleted == [] and "a" in w.rd.pending(None)
    w.busy_ids = set()
    w.rd.retry_pending(None, NOW + 60)
    assert radarr.deleted == ["/api/v3/moviefile/10"]


def test_retry_only_on_a_night_that_passed_the_guards(radarr):
    w = W(arrs_for(radarr))
    w.process(LOST_A, trouble=True)
    w.rd.retry_pending(None, NOW + 60)
    assert radarr.deleted == []


def test_disabled_only_alerts(radarr):
    w = W(arrs_for(radarr), enabled=False)
    w.process(LOST_A)
    assert radarr.deleted == [] and ("lost_file", "a") in w.alerts


def test_chunk_mismatch_row_is_marked_and_acted_on_clean_night(radarr):
    w = W(arrs_for(radarr))
    w.state.rows["a"]["state"] = "failed"
    w.state.rows["a"]["restore_note"] = "chunk mismatch"
    assert w.rd.mark_lost(None, "a", "a", "chunk mismatch")
    assert w.state.rows["a"]["restore_note"].startswith("lost: re-download pending")
    w.process([])
    assert radarr.deleted == ["/api/v3/moviefile/10"]
    assert w.state.rows["a"]["restore_note"].startswith("lost: re-download requested")


def test_not_in_the_arr_alerts_and_waits(radarr):
    radarr.movies.clear()
    w = W(arrs_for(radarr))
    w.process(LOST_A)
    assert radarr.deleted == [] and ("redownload_failed", "a") in w.alerts
    assert "a" in w.rd.pending(None)


def test_stalled_searches_again_after_three_days_then_alerts_once(radarr):
    w = W(arrs_for(radarr))
    w.process(LOST_A)
    assert len(radarr.commands) == 1
    w.process([], now=NOW + 1 * DAY)
    assert len(radarr.commands) == 1, "not yet"
    w.process([], now=NOW + 3.1 * DAY)
    assert len(radarr.commands) == 2
    w.process([], now=NOW + 6.2 * DAY)
    assert len(radarr.commands) == 3
    w.process([], now=NOW + 9.3 * DAY)
    assert len(radarr.commands) == 3
    assert [x for x in w.alerts if x[0] == "redownload_stalled"] == [("redownload_stalled", "a")]
    w.process([], now=NOW + 12.4 * DAY)
    assert [x for x in w.alerts if x[0] == "redownload_stalled"] == [("redownload_stalled", "a")]


def test_redownload_done_when_the_arr_has_a_file(radarr):
    w = W(arrs_for(radarr))
    w.process(LOST_A)
    radarr.movies[0]["hasFile"] = True
    w.process([], now=NOW + DAY)
    assert "redownload_done" in w.kinds() and ("redownload_done", "a") in w.alerts
    assert w.state.rows["a"]["restore_note"] == rd.NOTE_DONE
    assert w.kv["redownloads"] == {}
    w.process([], now=NOW + 5 * DAY)
    assert len(radarr.commands) == 1, "done means no more searching"


def test_state_changed_under_us_drops_the_row(radarr):
    w = W(arrs_for(radarr))
    w.state.rows["a"]["state"] = "whole"  # promoted fine meanwhile
    w.process(LOST_A)
    assert radarr.deleted == []


def test_arr_client_verbs_send_the_key_and_return_json(radarr):
    c = ArrClient(radarr.url, radarr.key)
    assert c.post("/api/v3/command", {"name": "X"}) == {"id": 1}
    assert radarr.commands == [{"name": "X"}]
    assert c.delete("/api/v3/moviefile/10") is None
    assert all(req[-1] == radarr.key for req in radarr.requests if len(req) == 3)
    assert radarr.key not in repr(c)


def _guarded_pending(radarr, ids=("a",)):
    """A night that passed the guards with `a` pending (it was busy when the check found it)."""
    w = W(arrs_for(radarr), ids=ids)
    w.busy_ids = {"a"}
    w.process(LOST_A)
    assert "a" in w.rd.pending(None) and radarr.deleted == []
    w.busy_ids = set()
    return w


def test_a_row_marked_during_a_pass_stays_and_an_acted_one_does_not_come_back(radarr):
    w = _guarded_pending(radarr, ids=("a", "b"))
    real = w.rd.arrs.search

    def search_and_mark(item):
        real(item)
        w.rd.mark_lost(None, "b", "a", "marked while a was being acted on")

    w.rd.arrs.search = search_and_mark
    w.rd.retry_pending(None, NOW + 60)
    assert set(w.rd.pending(None)) == {"b"}


def test_two_threads_reaching_run_pending_act_on_a_row_once(radarr):
    import threading
    import time
    w = _guarded_pending(radarr)
    real = w.rd.arrs.delete_file
    n = []

    def slow_delete(item):
        n.append(1)
        time.sleep(0.2)
        real(item)

    w.rd.arrs.delete_file = slow_delete
    ts = [threading.Thread(target=w.rd.retry_pending, args=(None, NOW + 60)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(n) == 1 and radarr.deleted == ["/api/v3/moviefile/10"]


def test_reset_clears_the_mass_loss_list_and_the_mark():
    a = FakeArr(movies=[movie(i, 10 + i, f"/share/Movies/M{i}/M{i}.mkv") for i in range(1, 5)])
    try:
        ids = ("1", "2", "3", "4")
        w = W(arrs_for(a), ids=ids, max_per_night=3)
        w.process([{"id": i, "title": i, "articles": 1} for i in ids])
        assert len(w.rd.pending(None)) == 4 and w.kv["redownload_night"].get("mass")
        assert w.rd.reset(None) == 4
        assert w.rd.pending(None) == {} and w.kv["redownload_night"] == {}
    finally:
        a.close()
