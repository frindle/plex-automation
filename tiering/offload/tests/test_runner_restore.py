"""Runner side of the restore path: the TIERING_AUTO_RESTORE trigger, the --restore entry,
and the orphan cleanup of a restored row. Fakes only (World and FakeState from test_runner)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))

import test_runner as T  # noqa: E402
from offload import runner  # noqa: E402
from offload.runner import Config  # noqa: E402

World, NIGHT, DAY, Res = T.World, T.NIGHT, 86400.0, T.Res


def alerts(w):
    if not os.path.exists(w.cfg.alerts):
        return []
    return [json.loads(line) for line in open(w.cfg.alerts)]


def bad_chunk_world(tmp_path, monkeypatch, **kw):
    ev = tmp_path / "events.jsonl"
    w = World(tmp_path, monkeypatch, events=str(ev), **kw)
    w.row("bad", 1, w.movie(1), "offloaded")
    w.deck = [{"ratingKey": "1"}]
    ev.write_text(json.dumps({"ts": NIGHT - 5, "id": "bad", "ev": "bad_chunk", "offset": 4096}) + "\n")
    w.restored = []

    def restore(db, row, apply, keep_name=True):
        w.restored.append((row["id"], apply, keep_name))
        return Res(True, "failed", 104, "restored from the restore path")
    w.runner.deps.restore = restore
    return w


def test_flag_is_off_by_default_and_read_from_env():
    base = {"TIERING_DB": "/x/t.db"}
    assert Config.from_env(base).auto_restore is False
    assert Config.from_env({**base, "TIERING_AUTO_RESTORE": "1"}).auto_restore is True
    assert Config.from_env({**base, "TIERING_AUTO_RESTORE": "0"}).auto_restore is False


def test_bad_chunk_without_the_flag_restores_nothing(tmp_path, monkeypatch):
    w = bad_chunk_world(tmp_path, monkeypatch)
    w.run()
    w.runner.join()
    assert w.state.rows["bad"]["state"] == "failed"
    assert w.restored == []


def test_bad_chunk_with_the_flag_restores_once(tmp_path, monkeypatch):
    w = bad_chunk_world(tmp_path, monkeypatch, auto_restore=True)
    w.run()
    w.runner.join()
    assert w.restored == [("bad", True, True)]
    assert "restore_done" in [a["kind"] for a in alerts(w)]
    w.run()
    w.runner.join()
    assert len(w.restored) == 1  # not retried every pass


def test_short_remote_with_the_flag_restores(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto_restore=True)
    w.row("a", 1, w.movie(1), "failed", stub_path="/s/a.mkv")
    w.restored = []
    w.runner.deps.restore = lambda db, row, apply, keep_name=True: (
        w.restored.append(row["id"]), Res(False, "failed", 0, "nope"))[1]
    w.state.evlog.append(("a", "promote_short_remote", {"offset": 5}))
    w.run()
    w.runner.join()
    assert w.restored == ["a"]
    assert "restore_failed" in [a["kind"] for a in alerts(w)]


def test_short_remote_without_the_flag_restores_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.row("a", 1, w.movie(1), "failed", stub_path="/s/a.mkv")
    w.restored = []
    w.runner.deps.restore = lambda *a, **k: w.restored.append(a)
    w.state.evlog.append(("a", "promote_short_remote", {}))
    w.run()
    w.runner.join()
    assert w.restored == []


def test_dry_run_with_the_flag_only_logs(tmp_path, monkeypatch):
    w = bad_chunk_world(tmp_path, monkeypatch, auto_restore=True, apply=False)
    w.run()
    w.runner.join()
    assert w.restored == []


def test_restore_that_raises_is_alerted_not_fatal(tmp_path, monkeypatch):
    w = bad_chunk_world(tmp_path, monkeypatch, auto_restore=True)

    def boom(*a, **k):
        raise RuntimeError("down")
    w.runner.deps.restore = boom
    w.run()
    w.runner.join()
    assert any(a["kind"] == "restore_failed" for a in alerts(w))


def test_orphan_cleanup_takes_a_restored_row_and_frees_its_path(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.runner.deps.open_holders = lambda p: []
    lib = w.movie(1)  # the restored file sits where L was
    open(lib, "w").write("restored bytes")
    stub = str(w.tmp / "o1.mkv")
    open(stub, "wb").write(b"x" * 10)
    man = w.tmp / "o1.json"
    man.write_text(json.dumps({"id": "o1", "stub_path": stub}))
    w.row("o1", 1, lib, "failed", stub_path=stub, restore_note=runner.RESTORED_NOTE)
    w.state.rows["o1"]["updated_ts"] = NIGHT - 10 * DAY
    w.run()
    assert not os.path.exists(stub) and not os.path.exists(man)
    assert open(lib).read() == "restored bytes"
    row = w.state.rows["o1"]
    assert row["restore_note"] == "orphan cleaned" and row["lib_path"] is None


def test_orphan_cleanup_keeps_a_restored_rows_stub_inside_grace(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.runner.deps.open_holders = lambda p: []
    lib = w.movie(1)
    open(lib, "w").write("restored bytes")
    stub = str(w.tmp / "o1.mkv")
    open(stub, "wb").write(b"x")
    w.row("o1", 1, lib, "failed", stub_path=stub, restore_note=runner.RESTORED_NOTE)
    w.state.rows["o1"]["updated_ts"] = NIGHT - 2 * DAY
    w.run()
    assert os.path.exists(stub)


def test_cli_restore_one_row(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.row("r1", 1, w.movie(1), "offloaded")
    got = []
    deps = w.runner.deps
    deps.restore = lambda db, row, apply, keep_name=True: (
        got.append((row["id"], apply, keep_name)), Res(True, "failed", 104, "ok"))[1]
    env = {"TIERING_DB": w.cfg.db}  # a dry run: apply is passed on as False
    assert runner.main(["--restore", "r1", "--release-name"], env=env, deps=deps) == 0
    assert got == [("r1", False, False)]
    assert runner.main(["--restore", "nope"], env=env, deps=deps) == 1
    assert runner.main(["--restore"], env=env, deps=deps) == 2
