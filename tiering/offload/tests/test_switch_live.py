"""switch_live with a fake Plex and a fake restore: no network, no files, no ffmpeg."""
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))

import pytest  # noqa: E402

from tiering.offload import ops, runner, switch_live  # noqa: E402
from tiering.offload import state as st  # noqa: E402

TOKEN = "tok-SECRET-123"
TRANSIENT = "transient-SECRET-456"
LIB = "/lib/Movie (2020)/Movie.mkv"
OLD_SIZE, NEW_SIZE = 1000, 2000
PLAYER = "player-1"


class Res:
    def __init__(self, ok, reason=""):
        self.ok, self.reason, self.state, self.step = ok, reason, "failed", 104


class FakePlex:
    """Answers the calls switch_live makes. `scan_after` = polls of the metadata before the new
    part shows (None: never)."""

    def __init__(self, *, playback=True, listed=True, poll=200, state="playing", offset=60_000,
                 scan_after=0, play_status=200, queue=True):
        self.calls = []
        self.listed, self.playback, self.poll, self.state = listed, playback, poll, state
        self.offset, self.scan_after, self.play_status, self.queue = offset, scan_after, play_status, queue
        self.meta_polls = 0
        self.session = True
        self.on_new = False  # the session reports the new part after playMedia
        self.plex = switch_live.Plex("http://plex.invalid:32400", TOKEN, http=self.http)

    def sent(self, path):
        return [c for c in self.calls if c[1] == path]

    def http(self, method, path, params, headers):
        self.calls.append((method, path, dict(params), dict(headers)))
        if path == "/status/sessions":
            if not self.session:
                return 200, json.dumps({"MediaContainer": {"Metadata": []}}).encode()
            size = NEW_SIZE if self.on_new else OLD_SIZE
            md = {"ratingKey": "42", "type": "movie", "viewOffset": self.offset, "duration": 7_200_000,
                  "Player": {"machineIdentifier": PLAYER, "state": self.state, "product": "App"},
                  "Media": [{"Part": [{"file": LIB, "size": size, "duration": 7_200_000}]}]}
            other = {"ratingKey": "9", "Player": {"machineIdentifier": "x"},
                     "Media": [{"Part": [{"file": "/lib/Other/Other.mkv", "size": 1}]}]}
            return 200, json.dumps({"MediaContainer": {"Metadata": [other, md]}}).encode()
        if path == "/clients":
            c = [{"machineIdentifier": PLAYER,
                  "protocolCapabilities": "timeline,playback,navigation" if self.playback else "timeline"}]
            return 200, json.dumps({"MediaContainer": {"Server": c if self.listed else []}}).encode()
        if path == "/player/timeline/poll":
            return self.poll, b""
        if path.startswith("/library/metadata/"):
            self.meta_polls += 1
            shown = self.scan_after is not None and self.meta_polls > self.scan_after
            parts = [{"size": OLD_SIZE, "duration": 7_200_000}] + (
                [{"size": NEW_SIZE, "duration": 7_260_000}] if shown else [])
            return 200, json.dumps({"MediaContainer": {"Metadata": [
                {"ratingKey": "42", "Media": [{"Part": parts}]}]}}).encode()
        if path == "/":
            return 200, json.dumps({"MediaContainer": {"machineIdentifier": "srv-id"}}).encode()
        if path == "/playQueues":
            body = {"MediaContainer": {"playQueueID": 77}} if self.queue else {}
            return (200 if self.queue else 500), json.dumps(body).encode()
        if path == "/security/token":
            return 200, json.dumps({"MediaContainer": {"token": TRANSIENT}}).encode()
        if path == "/player/playback/playMedia":
            if self.play_status == 200:
                self.on_new = True
            return self.play_status, b""
        return 404, b""


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    db = st.open_db(str(tmp_path / "t.db"))
    row = {"id": "abc", "lib_path": LIB, "stub_path": "/stub/abc.mkv", "state": "offloaded",
           "restore_note": None, "size": OLD_SIZE}
    clock = Clock()
    ctx = type("Ctx", (), {})()
    ctx.db, ctx.row, ctx.clock = db, row, clock
    ctx.restored = []
    ctx.maps = []

    def restore(db, row, apply, keep):
        ctx.restored.append((apply, keep))
        st.log_event(db, row["id"], "restore_done", {"name": "Movie.mp4", "size": NEW_SIZE})
        return Res(True)
    ctx.restore = restore

    def mapper(t_s, old, new, old_dur, new_dur):
        ctx.maps.append((t_s, old, new, old_dur, new_dur))
        return {"ok": True, "new_s": t_s + 5.0, "why": ""}
    ctx.mapper = mapper

    def run(fp, **kw):
        args = dict(apply=True, restore=ctx.restore, tierfs_mount="/mnt/tfs", mapper=ctx.mapper,
                    sleep=clock.sleep, clock=clock.now, scan_timeout=30, confirm_timeout=10)
        args.update(kw)
        return switch_live.switch_live(ctx.db, ctx.row, fp.plex, **args)
    ctx.run = run
    return ctx


def test_happy_path_restores_waits_maps_and_sends_playmedia(env):
    fp = FakePlex(scan_after=2)
    res = env.run(fp)
    assert res["switched"] == [PLAYER] and res["left"] == {}
    assert env.restored == [(True, True)]
    assert fp.meta_polls == 3  # waited for the scan
    (t_s, old, new, old_dur, new_dur), = env.maps
    assert (t_s, old, new) == (60.0, "/mnt/tfs/abc.mkv", "/lib/Movie (2020)/Movie.mp4")
    assert (old_dur, new_dur) == (7200.0, 7260.0)
    m, path, params, headers = fp.sent("/player/playback/playMedia")[0]
    assert headers["X-Plex-Target-Client-Identifier"] == PLAYER
    assert params["offset"] == 65_000 and params["key"] == "/library/metadata/42"
    assert params["machineIdentifier"] == "srv-id" and params["type"] == "video"
    assert (params["protocol"], params["address"], params["port"]) == ("http", "plex.invalid", 32400)
    assert params["containerKey"] == "/playQueues/77?window=100&own=1"
    assert params["token"] == TRANSIENT and params["commandID"] >= 1


def test_offset_is_read_again_just_before_the_switch(env):
    fp = FakePlex()
    orig = env.restore

    def restore(db, row, apply, keep):
        fp.offset = 90_000  # the viewer kept watching during the restore
        return orig(db, row, apply, keep)
    res = env.run(fp, restore=restore)
    assert res["switched"] == [PLAYER]
    assert env.maps[0][0] == 90.0
    assert fp.sent("/player/playback/playMedia")[0][2]["offset"] == 95_000


def test_paused_session_switches_at_once(env):
    fp = FakePlex(state="paused")
    assert env.run(fp)["switched"] == [PLAYER]


def test_no_session_restores_nothing(env):
    fp = FakePlex()
    fp.session = False
    res = env.run(fp)
    assert res["switched"] == [] and "no session" in res["note"]
    assert env.restored == [] and fp.sent("/player/playback/playMedia") == []


def test_player_without_playback_is_left_and_nothing_is_restored(env):
    fp = FakePlex(playback=False)
    res = env.run(fp)
    assert PLAYER in res["left"] and "playback" in res["left"][PLAYER]
    assert env.restored == [] and fp.sent("/player/playback/playMedia") == []


def test_unlisted_player_that_answers_the_probe_is_controllable(env):
    fp = FakePlex(listed=False, poll=200)
    assert switch_live.player_control(fp.plex, PLAYER)[0] is True
    fp2 = FakePlex(listed=False, poll=500)
    ok, why = switch_live.player_control(fp2.plex, PLAYER)
    assert ok is False and "500" in why


def test_scan_timeout_leaves_the_player_on_the_old_file(env):
    fp = FakePlex(scan_after=None)
    res = env.run(fp)
    assert res["switched"] == [] and res["left"][PLAYER] == "scan timeout"
    assert env.restored == [(True, True)]  # the restore itself is done and kept
    assert fp.sent("/player/playback/playMedia") == [] and env.maps == []


def test_mapping_failure_does_not_switch(env, caplog):
    fp = FakePlex()
    caplog.set_level(logging.INFO)
    res = env.run(fp, mapper=lambda *a: {"ok": False, "new_s": None, "why": "no_match"})
    assert res["switched"] == [] and "no_match" in res["left"][PLAYER]
    assert fp.sent("/player/playback/playMedia") == []
    assert "offset mapping failed" in caplog.text


def test_mapper_exception_does_not_switch(env):
    def boom(*a):
        raise RuntimeError("ffmpeg")
    fp = FakePlex()
    res = env.run(fp, mapper=boom)
    assert res["switched"] == [] and fp.sent("/player/playback/playMedia") == []


def test_failed_command_is_reported_and_player_left(env):
    fp = FakePlex(play_status=500)
    res = env.run(fp)
    assert res["switched"] == [] and "HTTP 500" in res["left"][PLAYER]


def test_missing_play_queue_still_sends_without_container_key(env):
    fp = FakePlex(queue=False)
    assert env.run(fp)["switched"] == [PLAYER]
    assert "containerKey" not in fp.sent("/player/playback/playMedia")[0][2]


def test_failed_restore_leaves_everything(env):
    fp = FakePlex()
    res = env.run(fp, restore=lambda *a: Res(False, "verify failed"))
    assert "verify failed" in res["note"] and res["left"][PLAYER] == "restore failed"
    assert fp.sent("/player/playback/playMedia") == []


def test_already_restored_row_is_not_restored_again(env):
    env.row.update(state="failed", restore_note=ops.RESTORED_NOTE)
    st.log_event(env.db, "abc", "restore_done", {"name": "Movie.mp4", "size": NEW_SIZE})
    fp = FakePlex()
    res = env.run(fp)
    assert env.restored == [] and res["switched"] == [PLAYER]


def test_dry_run_sends_nothing_and_restores_nothing(env, caplog):
    caplog.set_level(logging.INFO)
    fp = FakePlex()
    res = env.run(fp, apply=False)
    assert env.restored == [] and res["switched"] == []
    assert "dry run" in res["note"] and "would" in caplog.text
    for m, path, params, headers in fp.calls:
        assert m == "GET" and not path.startswith("/player/playback")
        assert path not in ("/playQueues", "/security/token")


def test_no_token_in_any_url_or_log(env, caplog):
    caplog.set_level(logging.DEBUG)
    fp = FakePlex(play_status=500)
    env.run(fp)
    env.run(FakePlex(scan_after=None))
    for m, path, params, headers in fp.calls:
        assert TOKEN not in path
        assert TOKEN not in json.dumps(params)
        assert headers["X-Plex-Token"] == TOKEN  # header only
    assert TOKEN not in caplog.text and TRANSIENT not in caplog.text
    assert "plex.invalid" not in caplog.text  # no URL is logged


def test_transport_error_message_has_no_url(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError("connect to http://plex.invalid:32400/?X-Plex-Token=" + TOKEN)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    p = switch_live.Plex("http://plex.invalid:32400", TOKEN)
    with pytest.raises(switch_live.PlexRCError) as e:
        p.get("/status/sessions")
    assert TOKEN not in str(e.value) and "plex.invalid" not in str(e.value)


def test_check_lists_sessions_and_control(env):
    out = []
    sess = switch_live.check(env.row, FakePlex().plex, out=out.append)
    assert len(sess) == 1
    text = "\n".join(out)
    assert PLAYER in text and "remote control OK" in text and "60s" in text
    out2 = []
    switch_live.check(env.row, FakePlex(playback=False).plex, out=out2.append)
    assert "NOT available" in "\n".join(out2)


def test_cli_check_and_dry_run(env, tmp_path, capsys):
    deps = type("D", (), {})()
    deps.state = st
    deps.restore = env.restore
    e = {"TIERING_DB": str(tmp_path / "t.db")}
    st.upsert(env.db, {"id": "abc", "lib_path": LIB, "state": "offloaded", "size": OLD_SIZE})
    fp = FakePlex()
    assert runner.switch_main(["--switch-live", "abc", "--check"], e, deps, plex=fp.plex) == 0
    assert PLAYER in capsys.readouterr().out
    assert fp.sent("/player/playback/playMedia") == []
    assert runner.switch_main(["--switch-live", "abc"], e, deps, plex=fp.plex) == 0  # dry run
    assert env.restored == [] and fp.sent("/player/playback/playMedia") == []
    assert runner.switch_main(["--switch-live", "nope"], e, deps, plex=fp.plex) == 1
    assert runner.switch_main(["--switch-live"], e, deps, plex=fp.plex) == 2


def _mark_env(tmp_path, state="offloaded", note=None, name="m"):
    db = st.open_db(str(tmp_path / f"{name}.db"))
    st.upsert(db, {"id": "abc", "lib_path": LIB, "state": state, "size": OLD_SIZE,
                   "restore_note": note})
    deps = type("D", (), {})()
    deps.state = st
    return db, deps, {"TIERING_DB": str(tmp_path / f"{name}.db")}


def test_mark_failed_dry_run_changes_nothing(tmp_path):
    db, deps, e = _mark_env(tmp_path)
    assert runner.main(["--mark-failed", "abc"], env=e, deps=deps) == 0
    assert st.get(db, "abc")["state"] == "offloaded"
    assert runner.main(["--mark-failed"], env=e, deps=deps) == 2
    assert runner.main(["--mark-failed", "nope"], env=e, deps=deps) == 1


def test_mark_failed_and_undo(tmp_path):
    db, deps, e = _mark_env(tmp_path)
    e["TIERING_APPLY"] = "1"
    assert runner.main(["--mark-failed", "abc"], env=e, deps=deps) == 0
    r = st.get(db, "abc")
    assert (r["state"], r["restore_note"]) == ("failed", runner.HAND_FAILED_NOTE)
    assert runner.main(["--mark-failed", "abc"], env=e, deps=deps) == 1  # not offloaded now
    assert runner.main(["--mark-failed", "abc", "--undo"], env=e, deps=deps) == 0
    r = st.get(db, "abc")
    assert (r["state"], r["restore_note"]) == ("offloaded", None)
    kinds = [x["kind"] for x in st.events(db, "abc")]
    assert kinds[-2:] == ["marked_failed", "unmarked_failed"]


def test_mark_failed_leaves_rows_in_other_states(tmp_path):
    for n, (state, note) in enumerate((("promoting", None), ("failed", "bad_chunk at offset 1"),
                                       ("failed", runner.RESTORED_NOTE))):
        db, deps, e = _mark_env(tmp_path, state, note, name=f"m{n}")
        e["TIERING_APPLY"] = "1"
        assert runner.main(["--mark-failed", "abc"], env=e, deps=deps) == 1
        if state == "failed":  # undo only reverses a hand mark, never a real failure
            assert runner.main(["--mark-failed", "abc", "--undo"], env=e, deps=deps) == 1
        assert (st.get(db, "abc")["state"], st.get(db, "abc")["restore_note"]) == (state, note)
