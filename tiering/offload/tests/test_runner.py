import importlib.util
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

from offload import paths, plex_watch, runner  # noqa: E402
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
    COLUMNS = ("id", "lib_path", "stub_path", "remote_path", "size", "sha256", "nzb_path",
               "nzb_path2", "state", "restore_score", "restore_note", "last_check_ts",
               "rating_key", "updated_ts", "created_ts")

    def __init__(self, clock):
        self.rows, self.evlog, self.mu, self.clock = {}, [], threading.Lock(), clock

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

    def update_if(self, db, id, expect_state, **f):
        with self.mu:
            r = self.rows.get(id)
            if r is None or r["state"] != expect_state:
                return False
            bad = [k for k in f if k not in self.COLUMNS]
            assert not bad, bad
            r.update(f, updated_ts=self.clock())
            return True

    def list_files(self, db, state=None):
        with self.mu:
            return [dict(r) for r in self.rows.values() if state in (None, r["state"])]

    def log_event(self, db, id, kind, detail):
        self.evlog.append((id, kind, detail))

    def events(self, db, id=None):
        return [{"id": i, "kind": k, "detail": d} for i, k, d in self.evlog
                if id in (None, i)]


class World:
    def __init__(self, tmp_path, monkeypatch, clock_t=NIGHT, **cfgkw):
        self.tmp = tmp_path
        self.clock = Clock(clock_t)
        self.state = FakeState(self.clock)
        self.items, self.plays, self.sessions, self.deck, self.progress = [], [], [], [], []
        self.fail = set()
        self.calls = []
        self.delay = 0.0
        self.demoted_rows = {}
        self.promote_result = {}
        self.gate_answer = "free"
        self.qualify_ok = True
        self.cur = self.maxc = 0
        self.mu = threading.Lock()
        (tmp_path / "lib").mkdir(exist_ok=True)
        kw = dict(db=str(tmp_path / "t.db"), tierfs_mount="/tfs", manifests=str(tmp_path),
                  stub_roots=str(tmp_path), apply=True, alerts=str(tmp_path / "alerts.jsonl"),
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
        self.demoted_rows[row["id"]] = dict(row)
        self.state.set_state(db, row["id"], "offloaded")
        return Res(True, "offloaded")

    def _qualify(self, row):
        self.calls.append(("qualify", row["id"]))
        return Q(self.qualify_ok, [] if self.qualify_ok else ["no_exact_nzb"],
                 {"nzb_path": "/n.nzb", "remote_path": "r", "sha256": "ff" * 32,
                  "nzb_path2": "/n2.nzb", "restore_score": 2, "restore_note": "ok",
                  "steps": {"x": 1}})

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
    assert w.state.rows == before and w.state.evlog == []
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


def test_gate_path_map():
    pairs = runner.parse_gate_map("/mnt/disk1=/data,/mnt/disk2=/data,/mnt/disk2/deep=/deep")
    assert runner.gate_path("/mnt/disk1/a.mkv", pairs) == "/data/a.mkv"
    assert runner.gate_path("/mnt/disk2/a.mkv", pairs) == "/data/a.mkv"
    assert runner.gate_path("/mnt/disk2/deep/a.mkv", pairs) == "/deep/a.mkv"
    assert runner.gate_path("/mnt/disk10/a.mkv", pairs) == "/mnt/disk10/a.mkv"


def test_plex_file_paths_match_rows_by_share_path(tmp_path, monkeypatch):
    d1 = tmp_path / "d1"
    d1.mkdir()
    pm = paths.parse_path_map(f"/share={d1}")
    w = World(tmp_path, monkeypatch, path_map=pm)
    os.symlink("/tfs/a.mkv", d1 / "a.mkv")
    w.items.append({"ratingKey": "5", "type": "movie", "addedAt": 0, "section": "Movies",
                    "files": ["/share/a.mkv"]})
    w.state.rows["a"] = dict(id="a", rating_key=None, lib_path="/share/a.mkv",
                             state="offloaded", size=GB, sha256="aa", updated_ts=0)
    w.deck = [{"ratingKey": "5"}]
    w.run()
    assert w.ops("promote") == ["a"]
    assert w.state.rows["a"]["rating_key"] == "5"
    assert w.state.rows["a"]["lib_path"] == "/share/a.mkv"


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
                         "TIERING_GATE_PATH_MAP": "/a=/data/a", "TIERFS_STUB_ROOTS": "/d/s",
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


# ---- follow-up fixes


def test_qualified_row_reaches_ops_with_sha256(w):
    w.row("a", 1, w.movie(1), "whole")
    w.state.rows["a"]["sha256"] = None
    w.run()
    assert w.ops("demote") == ["a"]
    got = w.demoted_rows["a"]
    assert got["sha256"] == "ff" * 32 and got["nzb_path"] == "/n.nzb"
    assert got["nzb_path2"] == "/n2.nzb" and got["remote_path"] == "r"
    assert got["restore_score"] == 2 and got["restore_note"] == "ok"
    assert "steps" not in w.state.rows["a"]


def test_apply_needs_gate_path_map(tmp_path):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
           "TIERFS_STUB_ROOTS": "/mnt/disk1/s"}
    with pytest.raises(ValueError, match="TIERING_GATE_PATH_MAP"):
        Config.from_env(env)
    with pytest.raises(ValueError):
        Config.from_env({**env, "TIERING_GATE_PATH_MAP": " , "})
    assert runner.main(["--once"], env=env, deps=object()) == 2
    c = Config.from_env({**env, "TIERING_GATE_PATH_MAP": "/mnt/a=/data/a"})
    assert c.gate_path_map == [("/mnt/a", "/data/a")]
    assert Config.from_env({"TIERING_DB": env["TIERING_DB"]}).apply is False


def test_apply_needs_stub_roots(tmp_path):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
           "TIERING_GATE_PATH_MAP": "/mnt/a=/data/a"}
    with pytest.raises(ValueError, match="TIERFS_STUB_ROOTS"):
        Config.from_env(env)
    assert runner.main(["--once"], env=env, deps=object()) == 2
    c = Config.from_env({**env, "TIERFS_STUB_ROOTS": "/mnt/disk1/s,/mnt/disk2/s"})
    assert c.stub_roots == "/mnt/disk1/s,/mnt/disk2/s"
    assert Config.from_env({"TIERING_DB": env["TIERING_DB"]}).apply is False


def _two_disks(tmp_path, monkeypatch, names=("one",)):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    for d in (d1, d2):
        d.mkdir()
    pm = paths.parse_path_map(f"/share={d1},/share={d2}")
    w = World(tmp_path, monkeypatch, path_map=pm)
    for i, name in enumerate(names, 1):
        w.items.append({"ratingKey": str(i), "type": "movie", "addedAt": 0, "section": "Movies",
                        "files": [f"/share/{name}.mkv"]})
        w.state.rows[name] = dict(id=name, rating_key=str(i), lib_path=f"/share/{name}.mkv",
                                  state="whole", size=GB, sha256="aa", updated_ts=0)
    return w, d1, d2


def test_demote_keeps_share_path_and_skips_ambiguous(tmp_path, monkeypatch, caplog):
    w, d1, d2 = _two_disks(tmp_path, monkeypatch, ("one", "two", "gone"))
    (d1 / "one.mkv").write_text("x")
    (d1 / "two.mkv").write_text("x")
    (d2 / "two.mkv").write_text("x")
    with caplog.at_level(logging.WARNING, logger="tiering.runner"):
        w.run()
    assert w.ops("demote") == ["one"]
    assert w.demoted_rows["one"]["lib_path"] == "/share/one.mkv"  # the DB row, unchanged
    assert ("gate", str(d1 / "one.mkv")) in w.calls
    assert w.state.rows["two"]["state"] == "whole"
    skipped = [r for r in caplog.records if "skipping" in r.getMessage()]
    assert len(skipped) == 2


def test_whole_file_moves_between_disks_row_keeps_id(tmp_path, monkeypatch):
    w, d1, d2 = _two_disks(tmp_path, monkeypatch)
    (d1 / "one.mkv").write_text("x")
    seen = []
    orig = w.runner.deps.gate
    w.runner.deps.gate = lambda p: (seen.append(p), "wait")[1]
    w.run()
    assert seen == [str(d1 / "one.mkv")] and w.ops("demote") == []
    os.rename(d1 / "one.mkv", d2 / "one.mkv")  # a mover took it to the other disk
    w.runner.deps.gate = orig
    w.clock.t += 86400  # the next night; tonight's try is spent
    w.run()
    assert w.ops("demote") == ["one"]
    assert ("gate", str(d2 / "one.mkv")) in w.calls
    assert w.demoted_rows["one"]["id"] == "one"
    assert w.demoted_rows["one"]["lib_path"] == "/share/one.mkv"
    assert list(w.state.rows) == ["one"]


def test_duplicate_on_both_disks_is_skipped(tmp_path, monkeypatch):
    w, d1, d2 = _two_disks(tmp_path, monkeypatch)
    (d1 / "one.mkv").write_text("x")
    (d2 / "one.mkv").write_text("x")
    w.run()
    assert w.ops("demote") == [] and w.ops("qualify") == []
    assert w.state.rows["one"]["state"] == "whole"


def test_reconcile_does_not_fail_a_row_whose_disk_path_is_unresolved(tmp_path, monkeypatch):
    w, d1, d2 = _two_disks(tmp_path, monkeypatch, ("gone", "dup", "ok"))
    for n in ("dup", "ok"):
        w.state.rows[n]["state"] = "offloaded"
    w.state.rows["gone"]["state"] = "offloaded"  # on neither disk
    os.symlink("/tfs/dup.mkv", d1 / "dup.mkv")
    os.symlink("/tfs/dup.mkv", d2 / "dup.mkv")  # on both
    os.symlink("/tfs/ok.mkv", d2 / "ok.mkv")
    w.run()
    assert w.state.rows["gone"]["state"] == "offloaded"
    assert w.state.rows["dup"]["state"] == "offloaded"
    assert w.state.rows["ok"]["state"] == "offloaded"
    os.unlink(d2 / "ok.mkv")
    (d2 / "ok.mkv").write_text("replaced")  # resolved, and a real file now
    w.run()
    assert w.state.rows["ok"]["state"] == "failed"


def test_promote_gets_the_unchanged_row(tmp_path, monkeypatch):
    w, d1, d2 = _two_disks(tmp_path, monkeypatch)
    w.state.rows["one"]["state"] = "offloaded"
    os.symlink("/tfs/one.mkv", d2 / "one.mkv")
    w.deck = [{"ratingKey": "1"}]
    w.run()
    assert w.ops("promote") == ["one"]
    assert w.state.rows["one"]["lib_path"] == "/share/one.mkv"


def test_load_deps_passes_path_map_and_no_stub_root(tmp_path, monkeypatch):
    import types
    got = {}
    pkg = runner.__package__
    ops = types.ModuleType(f"{pkg}.ops")
    ops.demote = lambda db, row, **kw: got.update(demote=kw)
    ops.promote = lambda db, id, reader, **kw: got.update(promote=kw)
    alt = types.ModuleType(f"{pkg}.altmount")
    alt.WebDavReader = lambda: "reader"
    monkeypatch.setitem(sys.modules, f"{pkg}.ops", ops)
    monkeypatch.setitem(sys.modules, f"{pkg}.altmount", alt)
    monkeypatch.setitem(sys.modules, f"{pkg}.state", types.ModuleType(f"{pkg}.state"))
    from offload import wiring
    monkeypatch.setattr(wiring, "qualify_from_env", lambda env=None: "q")
    pm = paths.parse_path_map("/share=/d1")
    deps = runner.load_deps(Config(db=str(tmp_path / "t.db"), path_map=pm, apply=True,
                                   gate_path_map=[("/a", "/b")], stub_roots="/d1/s"))
    deps.demote("db", {"id": "x"}, True)
    deps.promote("db", {"id": "x"}, True)
    assert got["demote"]["path_map"] == pm and "stub_root" not in got["demote"]
    assert got["promote"]["path_map"] == pm


def test_register_unhealthy_results_block_demotes_and_alert(tmp_path, monkeypatch):
    cases = [({"errors": 1, "listed": 100}, True), ({"listed": 100, "missing_local": 6}, True),
             ({"listed": 100, "missing_local": 3, "ambiguous": 3}, True),
             ({"listed": 100, "missing_local": 5}, False), ({"new": 2}, False), ({}, False),
             ({"missing_local": 1}, True)]
    for i, (res, bad) in enumerate(cases):
        sub = tmp_path / str(i)
        sub.mkdir()
        w = World(sub, monkeypatch)
        w.runner.deps.register = lambda db, apply, res=res: res
        w.row("a", 1, w.movie(1), "whole")
        w.run()
        assert (w.ops("demote") == []) is bad, res
        alerts = [json.loads(line) for line in open(w.cfg.alerts)] if \
            os.path.exists(w.cfg.alerts) else []
        assert bool([a for a in alerts if a["kind"] == "register_failed"]) is bad, res


def test_ops_events_alert_once_per_id(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.state.evlog.append(("a", "promote_short_remote", {"offset": 5}))
    w.state.evlog.append(("b", "takeover_orphan", {"stub": "/s/b.mkv"}))
    w.state.evlog.append(("c", "qualify_failed", {}))
    w.run()
    w.state.evlog.append(("a", "promote_short_remote", {"offset": 5}))
    w.run()
    got = sorted((a["kind"], a["id"]) for a in map(json.loads, open(w.cfg.alerts)))
    assert got == [("promote_short_remote", "a"), ("takeover_orphan", "b")]
    w2 = Runner(w.cfg, w.runner.deps, clock=w.clock, sleep=lambda s: None)  # restart
    w2.run_pass(wait=True)
    assert len(open(w.cfg.alerts).readlines()) == 2


def test_register_runs_once_a_night_before_window(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_t=NOON)
    calls = []
    w.runner.deps.register = lambda db, apply: calls.append(apply) or {"new": 0}
    w.row("a", 1, w.movie(1), "whole")
    w.run()
    assert calls == []  # 12:00 is more than an hour before 02:00
    w.clock.t = datetime(2026, 3, 11, 1, 30).timestamp()
    w.run()
    w.run()
    assert calls == [True]
    w.clock.t = datetime(2026, 3, 11, 3, 0).timestamp()
    w.run()
    assert calls == [True] and w.ops("demote") == ["a"]
    w.clock.t = datetime(2026, 3, 12, 1, 30).timestamp()
    w.run()
    assert calls == [True, True]


def test_register_failure_skips_the_nights_demotes(tmp_path, monkeypatch, caplog):
    w = World(tmp_path, monkeypatch, clock_t=NIGHT)

    def boom(db, apply):
        raise RuntimeError("arr down")
    w.runner.deps.register = boom
    w.row("a", 1, w.movie(1), "whole")
    with caplog.at_level(logging.ERROR, logger="tiering.runner"):
        w.run()
    assert w.ops("demote") == [] and w.ops("qualify") == []
    assert any("register failed" in r.getMessage() for r in caplog.records)


def test_register_dry_run_passes_apply_false(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, apply=False)
    calls = []
    w.runner.deps.register = lambda db, apply: calls.append(apply) or {}
    w.row("a", 1, w.movie(1), "whole")
    w.run()
    assert calls == [False] and w.ops("demote") == []
    assert w.ops("qualify") == []


def test_slot_timeout_alerts_once_per_id_per_night_and_counts(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    w = World(tmp_path, monkeypatch, events=str(ev))
    w.row("s", 1, w.movie(1), "offloaded")

    def put(t, i="s"):
        with open(ev, "a") as fh:
            fh.write(json.dumps({"ts": t, "id": i, "ev": "slot_timeout"}) + "\n")
    put(NIGHT - 5)
    put(NIGHT - 4)
    put(NIGHT - 3, "t")
    c = w.run()
    assert c["slot_timeouts"] == 3
    kinds = [json.loads(line) for line in open(w.cfg.alerts)]
    assert sorted(k["id"] for k in kinds) == ["s", "t"]
    assert all(k["kind"] == "slot_timeout" for k in kinds)
    assert w.state.rows["s"]["state"] == "offloaded"
    put(NIGHT - 2)
    c = w.run()
    assert c["slot_timeouts"] == 1 and len(open(w.cfg.alerts).readlines()) == 2
    w.clock.t = NIGHT + 86400
    put(NIGHT + 86400 - 1)
    w.run()
    assert len(open(w.cfg.alerts).readlines()) == 3


def _fake_intake(monkeypatch, calls):
    import types
    pkg = runner.__package__

    def mk(name, **attrs):
        m = types.ModuleType(f"{pkg}.{name}")
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, f"{pkg}.{name}", m)

    class Cfg:
        @staticmethod
        def from_env(name, env=None):
            return None if name == "secondary" and "NO_SECONDARY" in (env or {}) else name

    mk("nntp_stat", ProviderCfg=Cfg, Checker=lambda cfg: ("checker", cfg))
    mk("newznab", load_indexers=lambda raw: [("idx", raw)],
       NewznabClient=lambda ix: ("indexers", ix))
    mk("altmount", WebDavReader=lambda: "reader",
       ImportClient=lambda reader=None: ("importer", reader))
    mk("qualify", qualify=lambda row, **kw: (calls.append((row, kw)), "result")[1])


def test_wiring_builds_clients_from_env(tmp_path, monkeypatch):
    from offload import wiring
    calls = []
    _fake_intake(monkeypatch, calls)
    env = {"TIERING_DB": str(tmp_path / "t.db"), "NEWZNAB_INDEXERS": "[]"}
    fn = wiring.qualify_from_env(env)
    assert fn({"id": "a"}) == "result"
    row, kw = calls[0]
    assert kw["stat_primary"] == ("checker", "primary")
    assert kw["stat_secondary"] == ("checker", "secondary")
    assert kw["reader"] == "reader" and kw["importer"] == ("importer", "reader")
    assert kw["indexers"] == ("indexers", [("idx", "[]")])
    assert kw["archive_dir"] == str(tmp_path / "nzb")
    env["TIERING_NZB_DIR"] = "/x/nzb"
    wiring.qualify_from_env(env)({"id": "b"})
    assert calls[1][1]["archive_dir"] == "/x/nzb"
    with pytest.raises(wiring.WiringError):
        wiring.qualify_from_env({**env, "NO_SECONDARY": "1"})


def test_default_factory_is_wiring(tmp_path, monkeypatch):
    from offload import wiring
    monkeypatch.delenv("TIERING_QUALIFY_FACTORY", raising=False)
    monkeypatch.setattr(wiring, "qualify_from_env", lambda env=None: "built")
    _fake_intake(monkeypatch, [])
    import types
    for name in ("ops", "state"):
        monkeypatch.setitem(sys.modules, f"{runner.__package__}.{name}",
                            types.ModuleType(f"{runner.__package__}.{name}"))
    cfg = Config(db=str(tmp_path / "t.db"), gate_path_map=[("/a", "/b")])
    deps = runner.load_deps(cfg)
    assert deps.qualify == "built" and deps.register is not None


_HAVE_INTAKE = all(importlib.util.find_spec(f"offload.{m}") for m in
                   ("qualify", "nntp_stat", "newznab", "altmount", "register"))


@pytest.mark.skipif(not _HAVE_INTAKE, reason="tiering-intake (#82) is not merged yet")
def test_wiring_against_real_intake_modules(tmp_path, monkeypatch):
    from offload import wiring
    env = {"TIERING_DB": str(tmp_path / "t.db"), "NNTP_PRIMARY_HOST": "p.example",
           "NNTP_SECONDARY_HOST": "s.example", "NEWZNAB_INDEXERS": "[]",
           "ALTMOUNT_URL": "http://altmount.example", "ALTMOUNT_API_KEY": "k"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert callable(wiring.qualify_from_env(env))
