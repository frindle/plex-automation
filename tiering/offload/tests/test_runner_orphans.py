"""Orphan stub cleanup: a row an arr upgrade or delete left as failed / "L replaced" loses
its stub and manifest after TIERING_ORPHAN_GRACE_DAYS, and only when every check holds.
Fakes only (World and FakeState from test_runner), synthetic paths."""
import json
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))

import test_runner as T  # noqa: E402
from offload.runner import Config  # noqa: E402

World, NIGHT, DAY = T.World, T.NIGHT, 86400.0


def alerts(w):
    if not os.path.exists(w.cfg.alerts):
        return []
    return [json.loads(line) for line in open(w.cfg.alerts)]


def orphan_alerts(w):
    return [a for a in alerts(w) if a["kind"] == "orphan_stubs_removed"]


def events(w, kind="orphan_stub_removed"):
    return [e for e in w.state.evlog if e[1] == kind]


def make_orphan(w, id, rk, age_days=10, size=100, stub=None, manifest_stub=None):
    """A failed / "L replaced" row: L is a regular file (the arr's new one), the stub
    sits under the stub root and the manifest names it."""
    lib = w.movie(rk)
    open(lib, "w").write("new bytes")
    stub = stub or str(w.tmp / f"{id}.mkv")
    os.makedirs(os.path.dirname(stub), exist_ok=True)
    with open(stub, "wb") as f:
        f.write(b"x" * size)
    man = w.tmp / f"{id}.json"
    man.write_text(json.dumps({"id": id, "stub_path": manifest_stub or stub}))
    w.row(id, rk, lib, "failed", stub_path=stub, restore_note="L replaced")
    w.state.rows[id]["updated_ts"] = NIGHT - age_days * DAY
    return lib, stub, str(man)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.holders = []
    world.runner.deps.open_holders = lambda p: (world.calls.append(("open", p)), world.holders)[1]
    return world


def kept(w, id, lib, stub, man):
    assert os.path.exists(stub) and os.path.exists(man)
    assert open(lib).read() == "new bytes"
    assert w.state.rows[id]["restore_note"] == "L replaced"
    assert events(w) == [] and orphan_alerts(w) == []


def test_grace_not_reached_keeps_everything(w):
    lib, stub, man = make_orphan(w, "o1", 1, age_days=6)
    w.run()
    kept(w, "o1", lib, stub, man)


def test_grace_reached_removes_stub_and_manifest_only(w):
    lib, stub, man = make_orphan(w, "o1", 1, age_days=8, size=123)
    w.run()
    assert not os.path.exists(stub) and not os.path.exists(man)
    assert open(lib).read() == "new bytes"  # L is never touched
    row = w.state.rows["o1"]
    assert row["state"] == "failed" and row["restore_note"] == "orphan cleaned"
    assert events(w) == [("o1", "orphan_stub_removed", {"id": "o1", "size": 123})]
    # handled once: a later pass does nothing more
    w.clock.t += 60
    w.run()
    assert len(events(w)) == 1 and len(orphan_alerts(w)) == 1


def test_stub_with_two_links_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1)
    os.link(stub, str(w.tmp / "other-name.mkv"))
    w.run()
    kept(w, "o1", lib, stub, man)


def test_l_a_tierfs_symlink_again_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1)
    os.unlink(lib)
    os.symlink("/tfs/o1.mkv", lib)  # promote or restore put the symlink back
    w.run()
    assert os.path.exists(stub) and os.path.exists(man) and os.path.islink(lib)
    assert w.state.rows["o1"]["restore_note"] == "L replaced"
    assert events(w) == [] and orphan_alerts(w) == []


def test_l_symlink_appearing_during_the_open_check_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1)

    def holders(p):
        os.unlink(lib)
        os.symlink("/tfs/o1.mkv", lib)  # the re-check right before deleting sees it
        return []

    w.runner.deps.open_holders = holders
    w.run()
    assert os.path.exists(stub) and os.path.exists(man) and events(w) == []


def test_stub_outside_the_roots_is_kept(w):
    sub = str(w.tmp / "elsewhere" / "o1.mkv")
    lib, stub, man = make_orphan(w, "o1", 1, stub=sub)
    w.run()
    kept(w, "o1", lib, stub, man)
    assert not [c for c in w.calls if c[0] == "open"]


def test_stub_not_named_by_id_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1, stub=str(w.tmp / "someone-else.mkv"))
    w.run()
    kept(w, "o1", lib, stub, man)


def test_manifest_stub_path_mismatch_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1, manifest_stub=str(w.tmp / "x" / "o1.mkv"))
    w.run()
    kept(w, "o1", lib, stub, man)


def test_open_stub_is_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1)
    w.holders = [-1]  # not clear
    w.run()
    kept(w, "o1", lib, stub, man)


def test_no_open_check_means_kept(w):
    lib, stub, man = make_orphan(w, "o1", 1)
    w.runner.deps.open_holders = None
    w.run()
    kept(w, "o1", lib, stub, man)


def test_grace_zero_turns_cleanup_off(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, orphan_grace_days=0)
    w.runner.deps.open_holders = lambda p: []
    lib, stub, man = make_orphan(w, "o1", 1, age_days=400)
    w.run()
    kept(w, "o1", lib, stub, man)


def test_dry_run_changes_nothing(tmp_path, monkeypatch, caplog):
    w = World(tmp_path, monkeypatch, apply=False)
    w.runner.deps.open_holders = lambda p: []
    lib, stub, man = make_orphan(w, "o1", 1)
    before = {k: dict(v) for k, v in w.state.rows.items()}
    with caplog.at_level(logging.INFO, logger="tiering.runner"):
        w.run()
    assert "would remove" in caplog.text
    assert w.state.rows == before and w.state.evlog == []
    assert os.path.exists(stub) and os.path.exists(man) and open(lib).read() == "new bytes"
    assert not os.path.exists(w.cfg.alerts)


def test_alert_once_per_pass_with_count_and_bytes(w):
    make_orphan(w, "o1", 1, age_days=9, size=100)
    make_orphan(w, "o2", 2, age_days=30, size=250)
    make_orphan(w, "young", 3, age_days=1, size=999)
    w.run()
    a = orphan_alerts(w)
    assert len(a) == 1 and a[0]["count"] == 2 and a[0]["bytes_freed"] == 350
    assert sorted(e[0] for e in events(w)) == ["o1", "o2"]
    assert os.path.exists(w.tmp / "young.mkv")
    w.clock.t += 60
    w.run()
    assert len(orphan_alerts(w)) == 1


def test_config_env_default_and_zero():
    assert Config.from_env({"TIERING_DB": "/x/t.db"}).orphan_grace_days == 7.0
    assert Config.from_env({"TIERING_DB": "/x/t.db",
                            "TIERING_ORPHAN_GRACE_DAYS": "0"}).orphan_grace_days == 0.0
    assert Config.from_env({"TIERING_DB": "/x/t.db",
                            "TIERING_ORPHAN_GRACE_DAYS": "14"}).orphan_grace_days == 14.0
