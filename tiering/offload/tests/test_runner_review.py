"""Regression tests from the review of the runner: each one failed before its fix.
Fakes only (World and FakeState from test_runner), no network."""
import json
import os
import sqlite3
import sys
import threading
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))  # repo root (tiering_gate)

import test_runner as T  # noqa: E402
from offload import runner  # noqa: E402
from offload.runner import Config, Runner  # noqa: E402

World, Res, NIGHT, GB, NOON = T.World, T.Res, T.NIGHT, T.GB, T.NOON


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def alerts(w):
    if not os.path.exists(w.cfg.alerts):
        return []
    return [json.loads(line) for line in open(w.cfg.alerts)]


def kinds(w):
    return [a["kind"] for a in alerts(w)]


# P1: a weekly flag promotes the row whether or not the hot set wants it
def test_p1_weekly_flagged_row_is_promoted(w):
    w.row("wk", 1, w.movie(1), "offloaded")
    w.state.evlog.append(("wk", "weekly_flag", {"reasons": ["missing_segments_x"]}))
    w.run()
    w.run()
    assert w.ops("promote") == ["wk"], "a weekly-flagged row must be promoted even when idle"


def test_p1_flag_needs_an_event_and_ends_with_its_promote(w):
    w.row("a", 1, w.movie(1), "offloaded", restore_note="promote flagged: stale")
    w.run()
    assert w.ops("promote") == [], "a note alone is not a flag"
    w.row("b", 2, w.movie(2), "offloaded")  # no note: the event is the flag
    w.state.evlog.append(("b", "weekly_flag", {"reasons": []}))
    w.run()
    w.state.rows["b"]["state"] = "offloaded"  # offloaded again later
    w.run()
    assert w.ops("promote") == ["b"]


def test_p1_flagged_promote_failure_backs_off(w):
    w.row("wk", 1, w.movie(1), "offloaded")
    w.state.evlog.append(("wk", "weekly_flag", {}))
    w.promote_result["wk"] = Res(False, "offloaded", reason="no")
    w.run()
    w.run()
    assert w.ops("promote") == ["wk"], "retried inside the backoff"
    w.clock.t += 301
    w.run()
    assert w.ops("promote") == ["wk", "wk"]


# P2: our own play record survives an empty Plex history
def test_p2_empty_history_does_not_make_played_file_demotable(w):
    w.row("m", 1, w.movie(1), "whole")
    w.plays = [{"ratingKey": "1", "viewedAt": NIGHT - 86400}]
    w.run()
    assert w.ops("demote") == []
    w.plays = []  # history wiped; the read still looks complete
    w.clock.t += 60
    w.run()
    assert w.ops("demote") == [], "played 1 day ago, must stay whole 60 days"
    assert "history_shrunk" in kinds(w)


def test_p2_own_record_alone_keeps_a_file_hot(w):
    w.row("m", 1, w.movie(1), "whole")
    w.state.plays_seen["1"] = NIGHT - 86400
    w.run()
    assert w.ops("demote") == []


def test_p2_drop_threshold(tmp_path, monkeypatch):
    for n, blocked in ((8, False), (7, True)):
        sub = tmp_path / f"n{n}"
        sub.mkdir()
        w = World(sub, monkeypatch)
        w.row("m", 1, w.movie(1), "whole")
        w.state.kv["history_count"] = 10
        w.plays = [{"ratingKey": "5", "viewedAt": NIGHT - 40 * 86400}] * n
        w.run()
        assert (w.ops("demote") == []) is blocked, n


def test_a_play_ageing_out_of_the_window_is_not_a_wiped_history(w):
    """The idle window slides: with one play in it, that play ageing out empties the
    history without anything being lost, and demotes must go on."""
    idle = w.cfg.hot.idle_days * 86400
    w.row("m", 1, w.movie(1), "whole")
    w.plays = [{"ratingKey": "5", "viewedAt": NIGHT - idle + 600}]  # ten minutes left
    w.run()
    w.clock.t += 900  # the play is now outside the window, Plex no longer lists it
    w.plays = []
    w.run()
    assert "history_shrunk" not in kinds(w)
    assert w.runner.history_ok


def test_a_wiped_history_still_blocks_when_the_plays_are_inside_the_window(w):
    w.row("m", 1, w.movie(1), "whole")
    w.plays = [{"ratingKey": "5", "viewedAt": NIGHT - 86400}] * 3
    w.run()
    w.plays = []
    w.clock.t += 60
    w.run()
    assert "history_shrunk" in kinds(w)
    assert not w.runner.history_ok


def test_a_torrent_that_starts_during_qualify_stops_the_demote(w):
    """The gate said free before qualify; qualify takes minutes. It is asked again right
    before the offload, and a file that is no longer free stays whole."""
    w.row("m", 1, w.movie(1), "whole")
    real = w.runner.deps.qualify

    def qualify_then_torrent(row):
        out = real(row)
        w.gate_answer = "pause"  # a torrent began to seed it meanwhile
        return out

    w.runner.deps.qualify = qualify_then_torrent
    w.run()
    assert ("qualify", "m") in w.calls
    assert w.ops("demote") == []
    assert w.state.rows["m"]["state"] == "whole"
    assert len([c for c in w.calls if c[0] == "gate"]) == 2  # before and after qualify


EVE = NIGHT - 8 * 3600  # 19:00 the evening before: outside the demote window


LANDED = EVE - 30 * 86400  # the file landed a month before: Plex's own analysis is long done


def _touch_atime(path, t, landed=LANDED):
    os.utime(path, (t, landed))


def test_a_read_of_a_whole_file_restarts_its_idle_clock(tmp_path, monkeypatch):
    """A download or sync of a file that is still whole leaves no tierfs event. Its access
    time is the record: a read after the first look keeps the file whole."""
    w = World(tmp_path, monkeypatch, clock_t=EVE)  # outside the window: nothing is demoted
    path = w.movie(1)
    w.row("m", 1, path, "whole")
    _touch_atime(path, EVE - 5000)
    w.run()  # first look: the access time is noted
    assert w.ops("demote") == []
    _touch_atime(path, NIGHT - 100)  # the sync
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == [], "a file read minutes ago must stay whole"
    assert w.state.plays_seen["1"] == pytest.approx(NIGHT - 100)


@pytest.mark.parametrize("fresh", ["mtime", "added"])
def test_plex_analysing_a_new_import_is_not_a_read(tmp_path, monkeypatch, fresh):
    """Plex reads a new or replaced file for previews and markers soon after it lands. With
    new_days at 0 that must not hold the file whole for idle_days."""
    w = World(tmp_path, monkeypatch, clock_t=EVE, hot=runner.hotset.Cfg(new_days=0))
    path = w.movie(1, added=EVE - 3600 if fresh == "added" else 0)
    w.row("m", 1, path, "whole")
    if fresh == "mtime":
        _touch_atime(path, EVE - 3000, landed=EVE - 3600)  # imported an hour ago
    else:  # an old file date (an arr set it to the air date), added to Plex an hour ago
        _touch_atime(path, EVE - 3000)
    w.run()  # first look: the access time is noted
    _touch_atime(path, NIGHT - 100, landed=os.stat(path).st_mtime)  # the preview job
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == ["m"], "a read inside the import grace must not keep the file whole"
    assert "1" not in w.state.plays_seen


def test_a_sync_soon_after_the_first_pass_is_a_read(tmp_path, monkeypatch):
    """Every file gets its row on the first pass after a deploy. A sync of an old file the
    next day is a read, not an import being analysed."""
    w = World(tmp_path, monkeypatch, clock_t=EVE)
    path = w.movie(1, added=EVE - 400 * runner.DAY)
    w.row("m", 1, path, "whole", created_ts=EVE - 60)
    _touch_atime(path, EVE - 5000)
    w.run()
    _touch_atime(path, NIGHT - 100)  # the sync
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == [], "a sync must keep the file whole"


def test_a_file_nobody_read_is_still_demoted(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_t=EVE)
    path = w.movie(1)
    w.row("m", 1, path, "whole")
    _touch_atime(path, EVE - 5000)
    w.run()
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == ["m"]


def test_a_file_that_appears_between_checks_is_noted_at_once(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_t=EVE)
    w.run()  # no rows yet: the check has run
    path = w.movie(1)
    w.row("m", 1, path, "whole")
    _touch_atime(path, EVE - 5000)
    w.clock.t += 5
    w.run()  # not time for a check, but the file is new to us
    _touch_atime(path, NIGHT - 100)  # the sync
    w.clock.t = NIGHT
    w.run()
    assert w.ops("demote") == [], "the time of a file that was new must have been noted"


def test_a_noted_file_gets_its_access_time_set_back_so_relatime_shows_the_next_read(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_t=EVE)
    path = w.movie(1)
    w.row("m", 1, path, "whole")
    mt = os.stat(path).st_mtime
    os.utime(path, (mt + 5000, mt))
    w.run()
    assert os.stat(path).st_atime <= mt + 1, "the access time must be set back to the modification time"
    assert os.stat(path).st_mtime == mt


def test_our_own_qualify_read_is_not_a_sync(w):
    path = w.movie(1)
    w.row("m", 1, path, "whole")
    _touch_atime(path, NIGHT - 5000)
    w.qualify_ok = False  # qualify reads the file, then refuses
    real = w.runner.deps.qualify

    def reading_qualify(row):
        _touch_atime(path, NIGHT - 50)
        return real(row)

    w.runner.deps.qualify = reading_qualify
    w.run()
    assert ("qualify", "m") in w.calls
    w.clock.t += runner.ATIME_EVERY + 1
    db = w.runner.db()
    assert w.runner._local_reads(db, w.clock.t, w.state.list_files(db), {"m": "1"}) == {}


def test_p2_fresh_install_with_no_history_still_demotes(w):
    w.row("m", 1, w.movie(1), "whole")
    w.run()
    assert w.ops("demote") == ["m"]


# P3: a promote worker whose DB open raises must not strand the count
def test_p3_worker_open_failure_does_not_kill_promotes(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, promote_concurrency=1)
    w.row("a", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.runner.db()  # the pass connection is already open
    real = w.state.open_db

    def locked(path):
        raise sqlite3.OperationalError("database is locked")

    w.state.open_db = locked
    w.run()
    assert "promote_worker_died" in kinds(w)
    assert w.runner.health()["worker_deaths"] == 1
    w.state.open_db = real
    w.clock.t += 600
    w.run()
    assert w.ops("promote") == ["a"], f"promote_workers stuck at {w.runner.promote_workers}"
    assert w.runner.promote_workers == 0


def test_p3_healthz_reports_a_stuck_promote(w):
    w.runner.promote_started["x"] = NIGHT - 4 * 3600
    assert w.runner.health()["stuck_promotes"] == ["x"]


# P4: an unmapped disk path must never reach the gate as if it were free
def test_p4_unmapped_disk_path_is_not_free(monkeypatch):
    pairs = runner.parse_gate_map("/mnt/disk1=/data,/mnt/disk2=/data")
    assert runner.gate_path("/mnt/disk3/Media/m.mkv", pairs) is None
    sent = []

    def urlopen(*a, **k):
        sent.append(a)
        raise AssertionError("an unmapped path reached the gate")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    cfg = Config(db="x", gate_path_map=pairs)
    gate = runner._http_gate(cfg)
    assert gate("/mnt/disk3/Media/m.mkv") == "wait" and not sent


def test_p4_apply_refuses_when_a_disk_prefix_has_no_gate_entry(tmp_path):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
           "TIERING_GATE_PATH_MAP": "/mnt/disk1=/data", "TIERFS_STUB_ROOTS": "/mnt/disk1/.s",
           "TIERING_PATH_MAP": "/media=/mnt/disk1/media,/media=/mnt/disk2/media",
           **T._TFS(tmp_path)}
    with pytest.raises(ValueError, match="disk2"):
        Config.from_env(env)
    ok = Config.from_env({**env, "TIERING_GATE_PATH_MAP": "/mnt/disk1=/data,/mnt/disk2=/data"})
    assert len(ok.path_map) == 2


# P5: a wanted offloading row is promoted; a promoting row is always resumed
def test_p5_wanted_offloading_row_is_promoted(w):
    p = w.movie(1)
    os.symlink("/tfs/o.mkv", p)
    w.row("o", 1, p, "offloading")
    w.sessions = [{"ratingKey": "1"}]
    w.clock.t += 3 * 3600  # daytime, nothing resumes it
    w.run()
    w.run()
    assert w.ops("promote") == ["o"], "a live session on an offloading row is ignored"


def test_p5_promoting_row_resumes_even_when_not_wanted(w):
    p = w.movie(1)
    os.symlink("/tfs/p.mkv", p)
    w.row("p", 1, p, "promoting")
    w.run()
    assert w.ops("promote") == ["p"]


# P6: a raise from ops stops the night and alerts
def test_p6_demote_exception_stops_night_and_alerts(w):
    w.row("a", 1, w.movie(1), "whole")
    w.row("b", 2, w.movie(2), "whole")
    real = w._demote

    def demote(db, row, apply):
        if row["id"] == "a":
            w.calls.append(("demote", "a"))
            raise RuntimeError("head/tail hash mismatch after punching holes")
        return real(db, row, apply)

    w.runner.deps.demote = demote
    w.run()
    w.run()
    assert w.ops("demote") == ["a"], "no demote after the failure that night"
    assert "demote_failed" in kinds(w)


def test_p6_promote_exception_alerts_and_backs_off(w):
    w.row("a", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]

    def promote(db, row, apply):
        w.calls.append(("promote", row["id"]))
        raise RuntimeError("boom")

    w.runner.deps.promote = promote
    w.run()
    assert "promote_failed" in kinds(w)
    w.run()
    assert w.ops("promote") == ["a"], "retry waits for the backoff"


# P7: a bad_chunk during a running demote is held, then applied with update_if
def test_p7_bad_chunk_during_demote_is_not_lost(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    ev.write_text("")
    w = World(tmp_path, monkeypatch, events=str(ev))
    path = w.movie(1)
    w.row("x", 1, path, "whole")

    def demote(db, row, apply):
        w.calls.append(("demote", "x"))
        os.unlink(path)
        os.symlink("/tfs/x.mkv", path)
        w.state.set_state(db, "x", "offloading")
        with open(ev, "a") as fh:
            fh.write(json.dumps({"ts": NIGHT + 1, "id": "x", "ev": "bad_chunk",
                                 "offset": 8}) + "\n")
        w.runner._bad_chunks(db, NIGHT + 2)  # the pass thread, concurrently
        mid.append(w.state.rows["x"]["state"])
        w.state.set_state(db, "x", "offloaded")
        return Res(True, "offloaded")

    mid = []
    w.runner.deps.demote = demote
    w.run()
    assert mid == ["offloading"], "the runner must not write under a running demote"
    w.run()
    assert w.state.rows["x"]["state"] != "failed", "a promote decides, not the event"
    assert "x" in w.runner.flagged or "x" in w.ops("promote")
    assert "bad_chunk" in kinds(w)


def test_p7_bad_chunk_write_is_compare_and_set(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({"ts": NIGHT + 1, "id": "x", "ev": "bad_chunk", "offset": 8}) + "\n")
    w = World(tmp_path, monkeypatch, events=str(ev))
    w.row("x", 1, w.movie(1), "offloaded")
    seen = []
    real = w.state.update_if

    def update_if(db, id, expect, **f):
        seen.append(expect)
        w.state.rows[id]["state"] = "promoting"  # the row moved under us
        return real(db, id, expect, **f)

    w.state.update_if = update_if
    w.runner._bad_chunks(w.runner.db(), NIGHT + 2)
    assert seen == ["offloaded"] and w.state.rows["x"]["state"] == "promoting"
    assert "x" in w.runner.pending_bad, "kept to try again"


# P8: the register verdict fails closed
@pytest.mark.parametrize("res", [
    None, [], "ok", {}, {"errors": [1, 2]}, {"errors": 0, "listed": 5, "missing_local": 0},
    {"listed": 0, "missing_local": 0, "ambiguous": 0, "errors": 0},
    {"listed": 9, "missing_local": 0, "ambiguous": 0, "errors": 0.0},
    {"listed": 9, "missing_local": True, "ambiguous": 0, "errors": 0},
    {"new": 0, "updated": 3, "unchanged": 9000, "skipped_state": 0, "missing_local": 1,
     "unlisted": 0, "errors": 0, "applied": True},
])
def test_p8_register_verdict_fails_closed_on_bad_shape(res):
    assert Runner._register_verdict(res) != ""


def test_p8_register_real_shape_one_missing_does_not_block_forever():
    real = {"new": 0, "updated": 3, "unchanged": 9000, "skipped_state": 0, "listed": 9003,
            "missing_local": 1, "ambiguous": 0, "unlisted": 0, "errors": 0, "applied": True}
    assert Runner._register_verdict(real) == ""


# P9: apply needs the tierfs settings and an events directory
@pytest.mark.parametrize("drop", ["TIERFS_EVENTS", "TIERFS_MOUNT", "TIERFS_MANIFESTS"])
def test_p9_apply_requires_tierfs_settings(tmp_path, drop):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
           "TIERING_GATE_PATH_MAP": "/mnt/disk1=/data", "TIERFS_STUB_ROOTS": "/mnt/disk1/.s",
           **T._TFS(tmp_path)}
    del env[drop]
    with pytest.raises(ValueError, match=drop):
        Config.from_env(env)


def test_p9_events_directory_must_exist(tmp_path):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERING_APPLY": "1",
           "TIERING_GATE_PATH_MAP": "/mnt/disk1=/data", "TIERFS_STUB_ROOTS": "/mnt/disk1/.s",
           **T._TFS(tmp_path), "TIERFS_EVENTS": str(tmp_path / "nope" / "ev.jsonl")}
    with pytest.raises(ValueError, match="does not exist"):
        Config.from_env(env)


# P10: reconcile compares the link target's name, not its spelling
def test_p10_respelled_mount_does_not_fail_offloaded_rows(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, tierfs_mount="/tfs/.")
    w.row("a", 1, w.movie(1), "offloaded")
    w.clock.t += 6 * 3600
    w.run()
    assert w.state.rows["a"]["state"] == "offloaded"


def test_p10_mount_is_normalised_once(tmp_path):
    env = {"TIERING_DB": str(tmp_path / "t.db"), "TIERFS_MOUNT": "/mnt//tierfs/./"}
    assert Config.from_env(env).tierfs_mount == "/mnt/tierfs"


def test_reconcile_still_fails_a_row_whose_link_is_another_file(w):
    p = w.movie(1)
    os.symlink("/tfs/other.mkv", p)
    w.row("a", 1, p, "offloaded", symlink=False)
    w.run()
    assert w.state.rows["a"]["state"] == "failed"


# LOW: missing L is an alert, not a failure
def test_missing_l_alerts_once_and_keeps_the_row(w):
    p = w.movie(1)
    w.row("a", 1, p, "offloaded")
    os.unlink(p)
    w.run()
    w.run()
    assert w.state.rows["a"]["state"] == "offloaded"
    assert kinds(w).count("l_missing") == 1


# backoff
def test_promote_backoff_doubles_caps_and_alerts_on_the_third_failure(w):
    w.row("a", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.promote_result["a"] = Res(False, "offloaded", reason="no")
    delays = []
    for _ in range(9):
        w.runner.retry_after.clear()
        w.run()
        delays.append(w.runner.retry_after["a"] - w.clock.t)
    assert delays[:3] == [300, 600, 1200] and max(delays) == 6 * 3600
    assert kinds(w).count("promote_retrying") == 1


def test_success_resets_the_failure_count(w):
    w.row("a", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    w.promote_result["a"] = Res(False, "offloaded", reason="no")
    w.run()
    assert w.runner.fails["a"] == 1
    del w.promote_result["a"]
    w.clock.t += 400
    w.run()
    assert "a" not in w.runner.fails


# restart: events cursor, flagged ids, slot de-dup and tried survive
def test_events_are_read_incrementally(w):
    w.state.evlog.append(("a", "promote_short_remote", {}))
    reads = []
    real = w.state.events

    def events(db, id=None, after_seq=0):
        reads.append(after_seq)
        return real(db, id, after_seq)

    w.state.events = events
    w.run()
    w.run()
    assert reads[-1] == 1 and kinds(w) == ["promote_short_remote"]


def test_cursor_and_flags_survive_a_restart(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.row("wk", 1, w.movie(1), "offloaded")
    w.state.evlog.append(("wk", "weekly_flag", {}))
    w.promote_result["wk"] = Res(False, "offloaded", reason="no")
    w.run()
    assert w.state.kv["weekly_flag_cursor"] == 1 and w.state.kv["flagged"] == ["wk"]
    w2 = World(tmp_path, monkeypatch)
    w2.state = w.state
    w2.runner.state = w.state
    w2.runner.deps.state = w.state
    w2.runner.deps.promote = w2._promote
    w2.clock.t = w.clock.t + 400
    w2.run()
    assert w2.ops("promote") == ["wk"] and w2.runner.flag_seq == 1


def test_slot_timeout_dedup_survives_a_restart(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({"ts": NIGHT, "id": "s", "ev": "slot_timeout"}) + "\n")
    w = World(tmp_path, monkeypatch, events=str(ev))
    w.run()
    assert kinds(w) == ["slot_timeout"]
    w2 = World(tmp_path, monkeypatch, events=str(ev))
    w2.state = w.state
    w2.runner.state = w.state
    w2.run()
    assert kinds(w2) == ["slot_timeout"], "not alerted again after a restart"


def test_tried_ids_are_saved_with_the_night(w):
    w.row("m", 1, w.movie(1), "whole")
    w.run()
    saved = json.load(open(w.runner._night_file()))
    assert "m" in saved["tried"]


# workers honour stop
def test_workers_stop_when_asked(w):
    w.row("a", 1, w.movie(1), "whole")
    w.row("b", 2, w.movie(2), "whole")
    w.runner.stop.set()
    w.run()
    assert w.ops("demote") == []
    assert threading.active_count() >= 1
