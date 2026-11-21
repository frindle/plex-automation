import json
import logging
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from offload import plex_watch, runner  # noqa: E402
from offload.runner import Config, Deps, Runner  # noqa: E402

GB = 10**9
NIGHT = datetime(2026, 3, 10, 3, 0).timestamp()  # inside 02:00-06:00
NOON = datetime(2026, 3, 10, 12, 0).timestamp()


@dataclass
class Res:
    ok: bool
    state: str | None
    step: int = 0
    reason: str = ""
    dry_run: bool = False


@dataclass
class Q:
    ok: bool
    reasons: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeState:
    def __init__(self, clock):
        self.rows, self.events, self.mu, self.clock = {}, [], threading.Lock(), clock

    def open_db(self, path):
        return self

    def get(self, db, id):
        with self.mu:
            r = self.rows.get(id)
            return dict(r) if r else None

    def set_state(self, db, id, state, **f):
        with self.mu:
            self.rows[id].update(f, state=state, updated_ts=self.clock())

    def upsert(self, db, row):
        with self.mu:
            self.rows.setdefault(row["id"], {}).update(row)

    def list_files(self, db, state=None):
        with self.mu:
            return [dict(r) for r in self.rows.values() if state in (None, r["state"])]

    def log_event(self, db, id, kind, detail):
        self.events.append((id, kind, detail))


class World:
    def __init__(self, tmp_path, monkeypatch, clock_t=NIGHT, **cfgkw):
        self.tmp = tmp_path
        self.clock = Clock(clock_t)
        self.state = FakeState(self.clock)
        self.items, self.plays, self.sessions, self.deck, self.progress = [], [], [], [], []
        self.fail = set()
        self.calls = []
        self.delay = 0.0
        self.promote_result = {}
        self.gate_answer = "free"
        self.qualify_ok = True
        self.cur = self.maxc = 0
        self.mu = threading.Lock()
        (tmp_path / "lib").mkdir(exist_ok=True)
        kw = dict(db=str(tmp_path / "t.db"), tierfs_mount="/tfs", manifests=str(tmp_path),
                  stub_root=str(tmp_path), apply=True, alerts=str(tmp_path / "alerts.jsonl"),
                  window=runner.parse_window("02:00-06:00"))
        kw.update(cfgkw)
        self.cfg = Config(**kw)
        for name, fn in (("library_items", lambda: self.items),
                         ("play_history", lambda since: self.plays),
                         ("sessions", lambda: self.sessions),
                         ("on_deck", lambda: self.deck),
                         ("in_progress", lambda: self.progress)):
            monkeypatch.setattr(plex_watch, name, self._wrap(name, fn))
        deps = Deps(self.state, self._promote, self._demote, self._qualify,
                    lambda p: (self.calls.append(("gate", p)), self.gate_answer)[1])
        self.runner = Runner(self.cfg, deps, clock=self.clock, sleep=lambda s: None)

    def _wrap(self, name, fn):
        def f(*a):
            if name in self.fail:
                raise RuntimeError(f"{name} down")
            return fn(*a)
        return f

    # fake ops
    def _promote(self, db, row, apply):
        assert apply
        with self.mu:
            self.cur += 1
            self.maxc = max(self.maxc, self.cur)
            self.calls.append(("promote", row["id"]))
        time.sleep(self.delay)
        with self.mu:
            self.cur -= 1
        res = self.promote_result.get(row["id"])
        if res is None:
            self.state.set_state(db, row["id"], "whole")
            return Res(True, "whole")
        self.state.set_state(db, row["id"], res.state)
        return res

    def _demote(self, db, row, apply):
        assert apply
        self.calls.append(("demote", row["id"]))
        self.state.set_state(db, row["id"], "offloaded")
        return Res(True, "offloaded")

    def _qualify(self, row):
        self.calls.append(("qualify", row["id"]))
        return Q(self.qualify_ok, [] if self.qualify_ok else ["no_exact_nzb"],
                 {"nzb_path": "/n.nzb", "remote_path": "r"})

    # builders
    def movie(self, rk, section="Movies", added=0, path=None):
        path = path or str(self.tmp / "lib" / f"m{rk}.mkv")
        self.items.append({"ratingKey": str(rk), "type": "movie", "addedAt": added,
                           "section": section, "files": [path]})
        return path

    def episode(self, rk, show, season, index, path=None):
        path = path or str(self.tmp / "lib" / f"e{rk}.mkv")
        self.items.append({"ratingKey": str(rk), "type": "episode",
                           "grandparentRatingKey": str(show), "parentIndex": season,
                           "index": index, "addedAt": 0, "section": "TV", "files": [path]})
        return path

    def row(self, id, rk, path, state="whole", size=GB, **kw):
        if state == "whole":
            open(path, "a").close()
        elif state == "offloaded" and kw.pop("symlink", True):
            if os.path.lexists(path):
                os.unlink(path)
            os.symlink(f"/tfs/{id}{os.path.splitext(path)[1]}", path)
        self.state.rows[id] = dict(id=id, rating_key=str(rk) if rk is not None else None,
                                   lib_path=path, state=state, size=size, sha256="aa",
                                   updated_ts=0, **kw)

    def run(self, wait=True):
        return self.runner.run_pass(wait=wait)

    def ops(self, kind):
        return [c[1] for c in self.calls if c[0] == kind]


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ---------------------------------------------------------------- promotes


def test_live_session_then_season_in_episode_order_then_rest(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, promote_concurrency=1)
    # ratingKeys deliberately not in episode order
    for rk, idx in ((50, 1), (40, 2), (30, 3), (20, 4)):
        p = w.episode(rk, 7, 1, idx)
        w.row(f"e{rk}", rk, p, "offloaded")
    p = w.movie(99)
    w.row("m99", 99, p, "offloaded")
    w.deck = [{"ratingKey": "99"}]
    w.sessions = [{"ratingKey": "30"}]
    w.run()
    assert w.ops("promote") == ["e30", "e50", "e40", "e20", "m99"]


def test_promote_concurrency_cap(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, promote_concurrency=2)
    w.delay = 0.05
    for i in range(5):
        w.row(f"m{i}", i, w.movie(i), "offloaded")
        w.deck.append({"ratingKey": str(i)})
    w.run()
    assert w.maxc == 2
    assert sorted(w.ops("promote")) == [f"m{i}" for i in range(5)]


def test_replaying_a_pass_is_idempotent(w):
    w.row("a", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.row("b", 2, w.movie(2), "whole")
    w.run()
    first = list(w.calls)
    snap = {k: dict(v) for k, v in w.state.rows.items()}
    w.run()
    assert w.ops("promote") == ["a"]
    assert w.calls[:len(first)] == first
    assert w.state.rows["a"]["state"] == "whole" and w.state.rows["a"]["state"] == snap["a"]["state"]


# ---------------------------------------------------------------- demotes


def test_demote_window_and_midnight_crossing():
    day = runner.parse_window("02:00-06:00")
    night = runner.parse_window("22:00-02:00")
    at = lambda h, m=0: datetime(2026, 3, 10, h, m).timestamp()  # noqa: E731
    assert runner.night_start(at(3), day) is not None
    assert runner.night_start(at(6), day) is None
    assert runner.night_start(at(12), day) is None
    assert runner.night_start(at(23, 30), night) == at(22)
    assert runner.night_start(at(1), night) == datetime(2026, 3, 9, 22, 0).timestamp()
    assert runner.night_start(at(2), night) is None
    assert runner.night_start(at(12), night) is None
    with pytest.raises(ValueError):
        runner.parse_window("nonsense")


def test_demote_only_inside_window(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_t=NOON)
    w.row("a", 1, w.movie(1), "whole")
    w.run()
    assert w.ops("demote") == []
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == ["a"]
    assert w.ops("qualify") == ["a"]
    assert w.state.rows["a"]["nzb_path"] == "/n.nzb"


def test_demote_in_midnight_crossing_window(tmp_path, monkeypatch):
    t = datetime(2026, 3, 10, 0, 30).timestamp()
    w = World(tmp_path, monkeypatch, clock_t=t, window=runner.parse_window("22:00-02:00"))
    w.row("a", 1, w.movie(1), "whole")
    w.run()
    assert w.ops("demote") == ["a"]


def test_demote_gb_cap_per_night(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, demote_bytes_per_night=int(2.5 * GB))
    for i in range(3):
        w.row(f"m{i}", i, w.movie(i), "whole")
    w.run()
    assert len(w.ops("demote")) == 2
    w.run()
    assert len(w.ops("demote")) == 2
    w.clock.t += 86400
    w.run()
    assert len(w.ops("demote")) == 3


def test_no_demote_while_a_promote_is_pending(w):
    w.row("p", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.row("d", 2, w.movie(2), "whole")
    w.run()
    assert w.ops("promote") == ["p"] and w.ops("demote") == []
    w.run()
    assert w.ops("demote") == ["d"]


def test_no_demote_after_hash_failure_that_night(w):
    w.row("p", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.row("d", 2, w.movie(2), "whole")
    w.promote_result["p"] = Res(False, "failed", reason="sha256 mismatch")
    w.run()
    w.run()
    assert w.ops("demote") == []
    assert json.loads(open(w.cfg.alerts).readline())["kind"] == "promote_failed"
    w.clock.t += 86400
    w.run()
    assert w.ops("demote") == ["d"]


@pytest.mark.parametrize("failing", ["play_history", "sessions", "on_deck", "in_progress"])
def test_no_demote_when_any_plex_read_failed(w, failing):
    w.row("p", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.row("d", 2, w.movie(2), "whole")
    w.fail = {failing}
    w.run()
    w.run()
    assert w.ops("demote") == []
    if failing != "on_deck":
        assert w.ops("promote") == ["p"]  # promotes from the data we did get


def test_demote_skips_gate_nlink_qualify_never_and_wanted(w):
    w.row("gate", 1, w.movie(1), "whole")
    w.gate_answer = "wait"
    w.run()
    assert w.ops("demote") == [] and w.ops("qualify") == []
    w.gate_answer = "free"
    p = w.movie(2)
    w.row("link", 2, p, "whole")
    os.link(p, p + ".other")
    w.run()
    assert "link" not in w.ops("demote")
    w.qualify_ok = False
    w.run()
    assert w.ops("demote") == []
    w.qualify_ok = True
    w.row("adult", 3, w.movie(3, section="Adult"), "whole")
    w.row("hot", 4, w.movie(4, added=w.clock.t), "whole")
    w.run()
    assert "adult" not in w.ops("demote") and "hot" not in w.ops("demote")


# ---------------------------------------------------------------- dry run


def test_dry_run_calls_nothing_mutating(tmp_path, monkeypatch, caplog):
    w = World(tmp_path, monkeypatch, apply=False)
    w.row("p", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.row("d", 2, w.movie(2), "whole")
    w.row("x", 3, w.movie(3), "offloaded", symlink=False)  # L replaced
    before = {k: dict(v) for k, v in w.state.rows.items()}
    with caplog.at_level(logging.INFO, logger="tiering.runner"):
        w.run()
        w.clock.t += 60
        w.run()
    assert w.calls == []
    assert w.state.rows == before and w.state.events == []
    assert "plan: promote" in caplog.text
    assert not os.path.exists(w.cfg.alerts)


def test_dry_run_logs_demote_plan(tmp_path, monkeypatch, caplog):
    w = World(tmp_path, monkeypatch, apply=False)
    w.row("d", 2, w.movie(2), "whole")
    with caplog.at_level(logging.INFO, logger="tiering.runner"):
        w.run()
    assert "plan: demote" in caplog.text and w.calls == []


# ---------------------------------------------------------------- failures


def test_bad_chunk_fails_row_alerts_and_does_not_promote(tmp_path, monkeypatch, caplog):
    ev = tmp_path / "events.jsonl"
    w = World(tmp_path, monkeypatch, events=str(ev))
    w.row("bad", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    ev.write_text(json.dumps({"ts": NIGHT - 5, "id": "bad", "ev": "bad_chunk", "offset": 4096}) + "\n"
                  + "garbage\n")
    with caplog.at_level(logging.ERROR, logger="tiering.runner"):
        w.run()
    assert w.state.rows["bad"]["state"] == "failed"
    assert "4096" in w.state.rows["bad"]["restore_note"]
    assert w.ops("promote") == []
    alert = json.loads(open(w.cfg.alerts).readline())
    assert alert["kind"] == "bad_chunk" and alert["id"] == "bad"
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    w.run()
    assert len(open(w.cfg.alerts).readlines()) == 1


def test_reconcile_replaced_l_marks_failed_and_deletes_nothing(w):
    p = w.movie(1)
    w.row("gone", 1, p, "offloaded", symlink=False)
    open(p, "w").write("new bytes")  # an arr upgrade put a real file there
    q = w.movie(2)
    w.row("ok", 2, q, "offloaded")
    stub = w.tmp / "gone.mkv"
    stub.write_text("stub")
    w.run()
    assert w.state.rows["gone"]["state"] == "failed"
    assert w.state.rows["gone"]["restore_note"] == "L replaced"
    assert w.state.rows["ok"]["state"] == "offloaded"
    assert open(p).read() == "new bytes" and stub.exists()


# ---------------------------------------------------------------- process


def test_lock_prevents_second_instance(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    first = runner.acquire_lock(db)
    assert first is not None
    assert runner.acquire_lock(db) is None
    env = {"TIERING_DB": db}
    assert runner.main(["--once"], env=env, deps=object()) == 1
    first.close()
    second = runner.acquire_lock(db)
    assert second is not None
    second.close()


def test_path_map(tmp_path, monkeypatch):
    pairs = runner.parse_path_map("/data=/mnt/x,/data/deep=/mnt/y")
    assert runner.map_path("/data/deep/a.mkv", pairs) == "/mnt/y/a.mkv"
    assert runner.map_path("/data/a.mkv", pairs) == "/mnt/x/a.mkv"
    assert runner.map_path("/database/a.mkv", pairs) == "/database/a.mkv"
    w = World(tmp_path, monkeypatch, path_map=runner.parse_path_map(f"/data={tmp_path}/lib"))
    w.items.append({"ratingKey": "5", "type": "movie", "addedAt": 0, "section": "Movies",
                    "files": ["/data/a.mkv"]})
    w.row("a", None, str(tmp_path / "lib" / "a.mkv"), "offloaded")
    w.deck = [{"ratingKey": "5"}]
    w.run()
    assert w.ops("promote") == ["a"]
    assert w.state.rows["a"]["rating_key"] == "5"


def test_token_never_logged(w, monkeypatch, caplog):
    monkeypatch.setenv("PLEX_TOKEN", "sekrit-token-123")
    w.fail = set()
    monkeypatch.setattr(plex_watch, "sessions",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad sekrit-token-123")))
    with caplog.at_level(logging.DEBUG):
        w.run()
        monkeypatch.setattr(plex_watch, "library_items",
                            lambda: (_ for _ in ()).throw(RuntimeError("x sekrit-token-123")))
        w.runner.sleep = lambda s: w.runner.stop.set()
        w.runner.run_forever()
        runner.log.error("direct sekrit-token-123", exc_info=(RuntimeError, RuntimeError("sekrit-token-123"), None))
    assert "plex read sessions failed" in caplog.text
    assert "sekrit-token-123" not in caplog.text
    assert all("sekrit-token-123" not in (r.exc_text or "") for r in caplog.records)


def test_pass_exception_does_not_kill_loop(w):
    n = {"calls": 0}
    good = w.items

    def items():
        n["calls"] += 1
        if n["calls"] < 3:
            raise RuntimeError("down")
        return good

    w.items = good
    plex_watch.library_items = items  # restored by monkeypatch teardown below
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 4:
            w.runner.stop.set()

    w.runner.sleep = sleep
    w.runner.run_forever()
    assert w.runner.passes == 4 and w.runner.errors == 2
    assert os.path.exists(w.runner.heartbeat_path)


def test_healthz_localhost_only(w):
    w.run()
    srv = runner.start_health_server(w.runner, "127.0.0.1:0")
    try:
        port = srv.server_address[1]
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5))
        assert body["passes"] == 1 and body["last_pass_ts"] == NIGHT
        assert "counts" in body
    finally:
        srv.shutdown()
    with pytest.raises(ValueError):
        runner.start_health_server(w.runner, "0.0.0.0:0")


# ---------------------------------------------------------------- config


def test_config_defaults_and_overrides(tmp_path):
    c = Config.from_env({"TIERING_DB": str(tmp_path / "t.db")})
    assert c.apply is False and c.promote_concurrency == 2
    assert c.window == (120, 360) and c.demote_bytes_per_night == 200 * GB
    assert c.hot.never_sections == ("Adult",) and c.pass_seconds == 60
    assert c.hot.new_days == 30 and c.hot.idle_days == 60 and c.hot.near_end_eps == 3
    assert c.alerts == str(tmp_path / "alerts.jsonl") and c.http == "127.0.0.1:8471"
    c = Config.from_env({"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
                         "TIERING_NEVER_SECTIONS": "A, B", "TIERING_KEEP_4K": "1",
                         "TIERING_MAX_STREAM_KBPS": "9000", "TIERING_DEMOTE_GB_PER_NIGHT": "1.5"})
    assert c.apply and c.hot.never_sections == ("A", "B") and c.hot.keep_4k
    assert c.hot.max_stream_kbps == 9000 and c.demote_bytes_per_night == int(1.5 * GB)


def test_rule_timing_env(tmp_path):
    c = Config.from_env({"TIERING_DB": str(tmp_path / "t.db"), "TIERING_NEW_DAYS": "0.5",
                         "TIERING_IDLE_DAYS": "0.01", "TIERING_NEAR_END_EPS": "1"})
    assert c.hot.new_days == 0.5 and c.hot.idle_days == 0.01 and c.hot.near_end_eps == 1


def test_always_window(tmp_path, monkeypatch):
    c = Config.from_env({"TIERING_DB": str(tmp_path / "t.db"),
                         "TIERING_DEMOTE_WINDOW": "00:00-24:00"})
    assert c.window == (0, 1440)
    w = World(tmp_path, monkeypatch, clock_t=NOON, window=c.window)
    w.row("a", 1, w.movie(1), "whole")
    w.run()
    assert w.ops("demote") == ["a"]
    for h in (0, 5, 23):
        assert runner.night_start(datetime(2026, 3, 10, h, 59).timestamp(), c.window) is not None


def test_pass_seconds_sets_interval(tmp_path, monkeypatch):
    c = Config.from_env({"TIERING_DB": str(tmp_path / "t.db"), "TIERING_PASS_SECONDS": "2.5"})
    assert c.pass_seconds == 2.5
    w = World(tmp_path, monkeypatch, pass_seconds=2.5)
    sleeps = []
    w.runner.sleep = lambda s: (sleeps.append(s), w.runner.stop.set())
    w.runner.run_forever()
    assert sleeps == [2.5]
