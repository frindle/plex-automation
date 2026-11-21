"""The runner against the REAL intake modules (state, weekly, qualify, newznab, wiring):
the weekly flag reaches a promote with no restore_note involved, a `want` starts a promote
within 1 s, `nzb_unsure` writes no row, and the indexer budgets survive a restart."""
import json
import os
import sys
import threading
import time
from dataclasses import dataclass

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from offload import plex_watch, runner  # noqa: E402
from offload.runner import Config, Deps, Runner  # noqa: E402
from test_qualify import World as QWorld  # noqa: E402
from test_review_fixes4 import _qualify, _real, _store_alt  # noqa: E402
from test_weekly import DAY15  # noqa: E402
from tiering.offload import newznab as nz  # noqa: E402
from tiering.offload import state as st  # noqa: E402
from tiering.offload import weekly, wiring  # noqa: E402

WEEK = 7 * 86400


@dataclass
class Res:
    ok: bool
    state: str | None
    step: int = 0
    reason: str = ""
    dry_run: bool = False


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """A Runner on a real SQLite state DB with the real weekly check; promote is a fake."""

    def __init__(self, tmp_path, monkeypatch, names=("Alpha",), **cfgkw):
        self.e, self.db = _real(tmp_path, list(names))
        self.clock = Clock(float(DAY15))
        self.calls, self.fail_promote = [], set()
        for name, fn in (("library_items", lambda: []), ("play_history", lambda since: []),
                         ("sessions", lambda: []), ("on_deck", lambda: []),
                         ("in_progress", lambda: [])):
            monkeypatch.setattr(plex_watch, name, fn)
        kw = dict(db=str(tmp_path / "t.db"), tierfs_mount="/tfs", manifests=str(tmp_path),
                  stub_roots=str(tmp_path), apply=True, alerts=str(tmp_path / "alerts.jsonl"),
                  window=runner.parse_window("00:00-24:00"))
        kw.update(cfgkw)
        self.cfg = Config(**kw)
        self.deps = Deps(st, self.promote, lambda *a: None, lambda row: None, lambda p: "free",
                         weekly=self.weekly)
        self.runner = Runner(self.cfg, self.deps, clock=self.clock, sleep=lambda s: None)

    def weekly(self, db):
        p, s = self.e.checkers()
        try:
            return weekly.run_weekly(db, stat_primary=p, stat_secondary=s,
                                     indexers=self.e.indexers, report_path=self.e.report,
                                     state=st, now=self.clock(), monthly=False)
        finally:
            p.close()
            s.close()

    def promote(self, db, row, apply):
        self.calls.append(row["id"])
        if row["id"] in self.fail_promote:
            return Res(False, "offloaded", reason="remote down")
        st.set_state(db, row["id"], "whole")
        return Res(True, "whole")

    def run(self):
        return self.runner.run_pass(wait=True)

    def row(self, id):
        return st.get(self.db, id)

    def close(self):
        self.e.close()


def test_weekly_flag_promotes_without_restore_note_and_survives_a_good_week(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        rid, _ = _store_alt(tmp_path, r.db, "Alpha")
        r.e.prim.ids.discard("alt-Alpha@t")  # the restore NZB lost articles
        r.fail_promote.add(rid)
        r.run()  # start-up: the weekly check is due and runs; demotes would wait for it
        assert st.kv_get(r.db, "weekly_last_ts") == DAY15
        assert any(e["kind"] == "weekly_flag" for e in st.events(r.db, rid))
        # the flag is the event, not the note: wipe the note
        assert st.update_if(r.db, rid, "offloaded", restore_note=None)
        r.run()
        assert r.calls == [rid] and st.kv_get(r.db, "flagged") == [rid]
        assert st.kv_get(r.db, "weekly_flag_cursor") and r.runner.flagged == {rid}
        # a good week overwrites the note; the flag must still be there when the promote retries
        r.e.prim.ids.add("alt-Alpha@t")
        r.clock.t += WEEK + 3600
        r.run()
        assert st.kv_get(r.db, "weekly_last_ts") == r.clock.t
        assert not (r.row(rid)["restore_note"] or "").startswith("promote flagged")
        assert not [e for e in st.events(r.db, rid) if e["kind"] == "weekly_flag"][1:], \
            "the second week is good"
        assert r.calls == [rid, rid], "retried after the backoff, flag intact"
        r.fail_promote.clear()
        r.clock.t += 700
        r.run()
        assert r.calls == [rid] * 3 and r.row(rid)["state"] == "whole"
        assert st.kv_get(r.db, "flagged") == [] and r.runner.flagged == set()
    finally:
        r.close()


def test_flag_and_cursor_survive_a_runner_restart(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        rid, _ = _store_alt(tmp_path, r.db, "Alpha")
        r.e.prim.ids.discard("alt-Alpha@t")
        r.fail_promote.add(rid)
        r.run()
        r.run()
        r2 = Runner(r.cfg, r.deps, clock=r.clock, sleep=lambda s: None)
        r.clock.t += 400
        r2.run_pass(wait=True)
        assert r2.flagged == {rid} and r2.flag_seq == r.runner.flag_seq
        assert r2.weekly_last == DAY15 and not r2.weekly_running
        assert r.calls == [rid, rid]
    finally:
        r.close()


def test_demotes_wait_for_the_weekly_check(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        r.runner.complete = True
        assert "weekly check" in r.runner._demote_block(r.clock())
        r.run()
        assert "weekly check" not in r.runner._demote_block(r.clock())
        r.clock.t += WEEK
        assert "weekly check" in r.runner._demote_block(r.clock())
    finally:
        r.close()


def test_a_failing_weekly_check_alerts_blocks_demotes_and_retries(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        n = []

        def boom(db):
            n.append(1)
            raise RuntimeError("down")
        r.deps.weekly = boom
        r.run()
        assert "weekly check" in r.runner._demote_block(r.clock())
        r.run()
        assert len(n) == 1, "no retry inside the hour"
        r.clock.t += 3601
        r.run()
        assert len(n) == 2
        kinds = [json.loads(x)["kind"] for x in open(r.cfg.alerts)]
        assert kinds.count("weekly_failed") == 2
    finally:
        r.close()


def _write_events(path, *evs):
    with open(path, "a") as fh:
        for e in evs:
            fh.write(json.dumps(e) + "\n")


def test_want_starts_a_promote_within_a_second_outside_the_concurrency(tmp_path, monkeypatch):
    events = str(tmp_path / "events.jsonl")
    open(events, "w").close()
    r = Rig(tmp_path, monkeypatch, names=("Alpha", "Beta"), events=events, promote_concurrency=1)
    try:
        a, b = (x["id"] for x in sorted(st.list_files(r.db), key=lambda x: x["id"]))
        started, a_started = threading.Event(), threading.Event()
        release, seen = threading.Event(), {}

        def promote(db, row, apply, want=None):
            r.calls.append(row["id"])
            if row["id"] == a:  # holds the only regular slot
                a_started.set()
                release.wait(10)
            else:
                seen["first"] = want()
                started.set()
            st.set_state(db, row["id"], "whole")
            return Res(True, "whole")
        r.deps.promote = promote
        # a is a flagged row and takes the regular slot
        st.log_event(r.db, a, "weekly_flag", {"reasons": ["x"]})
        r.runner.weekly_last = r.clock()  # the weekly check is not due
        r.runner.run_pass()
        assert a_started.wait(5)
        t0 = time.monotonic()
        th = threading.Thread(target=r.runner.follow_forever, daemon=True)
        th.start()
        _write_events(events, {"ts": r.clock(), "id": b, "ev": "want", "offset": 1000})
        assert started.wait(1.0), "a want must start its promote within 1 s"
        assert time.monotonic() - t0 < 1.0
        assert seen["first"] == 1000 and r.calls[:2] == [a, b]
        release.set()
        r.runner.stop.set()
        th.join(2)
        r.runner.join()
        assert r.row(b)["state"] == "whole"
    finally:
        r.close()


def test_want_for_a_running_promote_moves_its_cursor_and_stale_or_whole_is_ignored(
        tmp_path, monkeypatch):
    events = str(tmp_path / "events.jsonl")
    open(events, "w").close()
    r = Rig(tmp_path, monkeypatch, events=events)
    try:
        rid = st.list_files(r.db)[0]["id"]
        r.runner.weekly_last = r.clock()
        in_promote, release, cursors = threading.Event(), threading.Event(), []

        def promote(db, row, apply, want=None):
            cursors.append(want())
            in_promote.set()
            release.wait(10)
            cursors.append(want())
            st.set_state(db, row["id"], "whole")
            return Res(True, "whole")
        r.deps.promote = promote
        _write_events(events, {"ts": r.clock() - 5000, "id": rid, "ev": "want", "offset": 7})
        r.runner._follow_once()
        assert not in_promote.wait(0.2), "a want from long ago is not a player waiting"
        _write_events(events, {"ts": r.clock(), "id": rid, "ev": "want", "offset": 10})
        r.runner._follow_once()
        assert in_promote.wait(2)
        _write_events(events, {"ts": r.clock(), "id": rid, "ev": "want", "offset": 99})
        r.runner._follow_once()
        release.set()
        r.runner.join()
        assert cursors == [10, 99] and r.row(rid)["state"] == "whole"
        _write_events(events, {"ts": r.clock(), "id": rid, "ev": "want", "offset": 5})
        r.runner._follow_once()
        r.runner.join()
        assert len(cursors) == 2, "a want for a whole row is ignored"
    finally:
        r.close()


def test_want_during_backoff_starts_then_is_spaced_and_failed_is_ignored(tmp_path, monkeypatch):
    events = str(tmp_path / "events.jsonl")
    open(events, "w").close()
    r = Rig(tmp_path, monkeypatch, events=events)
    try:
        rid = st.list_files(r.db)[0]["id"]
        r.runner.weekly_last = r.clock()
        r.fail_promote.add(rid)

        def want(off=1):
            _write_events(events, {"ts": r.clock(), "id": rid, "ev": "want", "offset": off})
            r.runner._follow_once()
            r.runner.join()
        want()
        assert r.calls == [rid]
        assert r.runner.retry_after[rid] > r.clock(), "the failure put the row in backoff"
        r.clock.t += 30
        want()
        assert r.calls == [rid], "a second want inside 60 s starts nothing"
        r.clock.t += 31
        want()
        assert r.calls == [rid, rid], "inside the backoff, 60 s on: a want starts at once"
        assert r.runner.want_last[rid] == r.clock.t
        assert st.kv_get(r.db, "want_last") == {rid: r.clock.t}
        # the spacing survives a restart
        r2 = Runner(r.cfg, r.deps, clock=r.clock, sleep=lambda s: None)
        r2._load_persisted(r.db)
        assert r2.want_last == {rid: r.clock.t}
        # a failed row (two hash mismatches) is not retried by a want
        assert st.update_if(r.db, rid, "offloaded", state="failed")
        r.clock.t += 120
        want()
        assert r.calls == [rid, rid]
    finally:
        r.close()


def test_a_promote_without_want_in_its_signature_still_runs(tmp_path, monkeypatch):
    events = str(tmp_path / "events.jsonl")
    open(events, "w").close()
    r = Rig(tmp_path, monkeypatch, events=events)
    try:
        rid = st.list_files(r.db)[0]["id"]
        r.runner.weekly_last = r.clock()
        _write_events(events, {"ts": r.clock(), "id": rid, "ev": "want", "offset": 1})
        r.runner._follow_once()
        r.runner.join()
        assert r.calls == [rid]
    finally:
        r.close()


# ------------------------------------------------------------------ qualify results


def test_nzb_unsure_writes_no_row_and_no_event(tmp_path, monkeypatch):
    (tmp_path / "q").mkdir()
    (tmp_path / "r").mkdir()
    q = QWorld(tmp_path / "q")
    rig = Rig(tmp_path / "r", monkeypatch)
    try:
        row = {"id": "a" * 16, "state": "whole", "lib_path": str(q.lib), "size": 5,
               "release_name": q.row["release_name"], "title": "Movie", "year": 2020,
               "sha256": "x"}
        st.upsert(rig.db, row)
        # a spent grab budget: the search was incomplete, so the answer is "not tonight"
        ix = nz.NewznabClient([nz.Indexer("one", q.nz.url, "nzkey")], grab_budget=0,
                              min_interval=0, grab_interval=0)
        rig.deps.qualify = lambda r: _qualify(q, ix)
        rig.deps.demote = lambda *a: pytest.fail("no demote")
        before = dict(st.get(rig.db, row["id"]))
        events_before = len(st.events(rig.db))
        rig.runner.disk[row["id"]] = str(q.lib)
        rig.runner._demote_one(rig.db, row["id"], rig.clock())
        assert st.get(rig.db, row["id"]) == before, "nothing is written on nzb_unsure"
        assert len(st.events(rig.db)) == events_before, "no qualify_failed event either"
    finally:
        rig.close()
        q.close()


def test_no_exact_nzb_is_still_recorded(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    try:
        row = {"id": "b" * 16, "state": "whole", "lib_path": str(tmp_path / "f.mkv"),
               "release_name": "X", "size": 5, "sha256": "x"}
        open(row["lib_path"], "w").close()
        st.upsert(rig.db, row)

        class Q:
            ok, reasons, fields = False, ["no_exact_nzb"], {}
        rig.deps.qualify = lambda r: Q
        rig.runner.disk[row["id"]] = row["lib_path"]
        rig.runner._demote_one(rig.db, row["id"], rig.clock())
        assert "qualify_failed" in [e["kind"] for e in st.events(rig.db, row["id"])]
    finally:
        rig.close()


# ------------------------------------------------------------------ budgets across restarts


def test_indexer_budgets_survive_a_runner_restart(tmp_path, monkeypatch):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "NNTP_PRIMARY_HOST": "p.example",
           "NNTP_SECONDARY_HOST": "s.example", "NEWZNAB_INDEXERS": "[]",
           "ALTMOUNT_URL": "https://altmount.example", "ALTMOUNT_API_KEY": "k",
           "TIERFS_REMOTE_BASE": "https://dav.example"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    wiring._clients.clear()
    c1 = wiring.clients()
    assert wiring.clients() is c1, "one client per process"
    for _ in range(3):
        c1.indexers._take("search", "one", 3, nz.SearchBudgetSpent)
    assert os.path.exists(tmp_path / "newznab_state.json")
    wiring._clients.clear()  # the container restarts
    c2 = wiring.clients()
    assert c2 is not c1
    with pytest.raises(nz.SearchBudgetSpent):
        c2.indexers._take("search", "one", 3, nz.SearchBudgetSpent)
    monkeypatch.setenv("TIERING_NEWZNAB_STATE", str(tmp_path / "other.json"))
    wiring._clients.clear()
    wiring.clients().indexers._take("search", "one", 3, nz.SearchBudgetSpent)
    assert os.path.exists(tmp_path / "other.json")
    wiring._clients.clear()
