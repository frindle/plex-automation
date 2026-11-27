"""The runner side of the nightly check and the automatic re-download."""
import dataclasses
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from fakes_intake import FakeArr  # noqa: E402
from offload import redownload  # noqa: E402
from offload.runner import Config  # noqa: E402
from test_runner_contract import Rig, Res  # noqa: E402
from test_weekly import DAY15, ids_for  # noqa: E402
from tiering.offload import state as st  # noqa: E402
from tiering.offload import weekly  # noqa: E402
from tiering.offload.register import ArrClient  # noqa: E402

DAY = 86400


def _call_counter(r, calls, **extra):
    def fn(db, **kw):
        calls.append(kw)
        return weekly.run_weekly(db, stat_primary=r.e.checkers()[0], stat_secondary=r.e.checkers()[1],
                                 indexers=r.e.indexers, report_path=r.e.report, state=st,
                                 now=r.clock(), monthly=False, **kw)
    return fn


def test_check_runs_nightly_not_weekly(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        runs = []
        r.deps.weekly = lambda db: runs.append(r.clock()) or r.weekly(db)
        r.run()
        assert len(runs) == 1
        r.clock.t += 20 * 3600
        r.run()
        assert len(runs) == 1, "not due after 20 hours"
        r.clock.t += 4 * 3600
        r.run()
        assert len(runs) == 2, "due a day after the last start"
        assert st.kv_get(r.db, "weekly_last_ts") == r.clock.t, "the start time is stored"
    finally:
        r.close()


def test_a_slightly_late_start_does_not_skip_a_night(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        runs = []
        r.deps.weekly = lambda db: runs.append(1) or r.weekly(db)
        r.run()
        r.clock.t += 23.5 * 3600  # the window opens a little earlier than yesterday
        r.run()
        assert len(runs) == 2
    finally:
        r.close()


def test_cadence_follows_the_setting(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, check_every_h=48.0)
    try:
        runs = []
        r.deps.weekly = lambda db: runs.append(1) or r.weekly(db)
        r.run()
        r.clock.t += 30 * 3600
        r.run()
        assert len(runs) == 1
        r.clock.t += 19 * 3600
        r.run()
        assert len(runs) == 2
    finally:
        r.close()


def test_worker_passes_budget_summary_and_hook_and_stores_the_cursor(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, check_max_h=2.0)
    try:
        calls = []

        def weekly_fn(db, budget_s=None, summary=None, lost_hook=None):
            calls.append((budget_s, summary is not None, lost_hook is not None))
            summary.update(finished=False, unreached=["x", "y"])

        r.deps.weekly = weekly_fn
        r.run()
        assert calls == [(7200.0, True, True)]
        cur = st.kv_get(r.db, "weekly_cursor")
        assert cur["finished"] is False and cur["unreached"] == 2
    finally:
        r.close()


def test_config_env_defaults_and_settings():
    c = Config.from_env({"TIERING_DB": "x"})
    assert (c.check_every_h, c.check_max_h, c.auto_redownload, c.redownload_max) == (24.0, 4.0, True, 3)
    c = Config.from_env({"TIERING_DB": "x", "TIERING_CHECK_EVERY_H": "12", "TIERING_CHECK_MAX_H": "1.5",
                         "TIERING_AUTO_REDOWNLOAD": "0", "TIERING_REDOWNLOAD_MAX_PER_NIGHT": "5"})
    assert (c.check_every_h, c.check_max_h, c.auto_redownload, c.redownload_max) == (12.0, 1.5, False, 5)


# ------------------------------------------------------------------ lost files


def _lost_rig(tmp_path, monkeypatch, **cfg):
    """A Rig whose row Alpha has a real tierfs-style symlink and an arr that holds it."""
    r = Rig(tmp_path, monkeypatch, **cfg)
    rid = f"{0:016x}"
    lib = tmp_path / "lib" / "Alpha.mkv"
    lib.parent.mkdir()
    os.symlink(f"/tfs/{rid}.mkv", lib)
    assert st.update_if(r.db, rid, "offloaded", lib_path=str(lib))
    arr = FakeArr(movies=[{"id": 1, "title": "Alpha", "monitored": True, "hasFile": True,
                           "movieFile": {"id": 10, "path": str(lib)}}])
    from tiering.offload import redownload as rdl
    arrs = rdl.Arrs([ArrClient(arr.url, arr.key)], [])

    def weekly_fn(db, **kw):
        p, s = r.e.checkers()
        try:
            return weekly.run_weekly(db, stat_primary=p, stat_secondary=s, indexers=r.e.indexers,
                                     report_path=r.e.report, state=st, now=r.clock(), **kw)
        finally:
            p.close()
            s.close()

    r.deps = dataclasses.replace(r.deps, weekly=weekly_fn, arrs=lambda: arrs,
                                 altmount_ok=lambda: True)
    r.runner.deps = r.deps
    return r, rid, lib, arr


def test_l_check_is_strict(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch)
    try:
        row = st.get(r.db, rid)
        assert r.runner._l_is_symlink_ours(row) is True
        lib.unlink()
        os.symlink("/elsewhere/other.mkv", lib)
        assert r.runner._l_is_symlink_ours(row) is False, "another link"
        lib.unlink()
        lib.write_text("real media")
        assert r.runner._l_is_symlink_ours(row) is False, "a real file is never ours"
        lib.unlink()
        os.symlink(f"/not-the-mount/{rid}.mkv", lib)
        assert r.runner._l_is_symlink_ours(row) is False, "not into the tierfs mount"
    finally:
        arr.close()
        r.close()


def test_nightly_check_finds_a_gone_article_and_the_arr_is_asked(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch)
    try:
        mid = ids_for("Alpha")[3]
        r.e.prim.ids.discard(mid)
        r.e.sec.ids.discard(mid)
        r.run()
        assert arr.deleted == ["/api/v3/moviefile/10"]
        assert arr.commands == [{"name": "MoviesSearch", "movieIds": [1]}]
        row = st.get(r.db, rid)
        assert row["state"] == "failed" and row["restore_note"].startswith("lost: re-download requested")
        kinds = [e["kind"] for e in st.events(r.db, rid)]
        assert "redownload_requested" in kinds
    finally:
        arr.close()
        r.close()


def test_provider_unknown_night_deletes_nothing(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch)
    try:
        mid = ids_for("Alpha")[3]
        r.e.prim.ids.discard(mid)
        r.e.sec.ids.discard(mid)
        r.e.sec.refuse_auth = True
        r.run()
        assert arr.deleted == [] and st.get(r.db, rid)["state"] == "offloaded"
    finally:
        arr.close()
        r.close()


def test_dry_run_never_acts(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch, apply=False)
    try:
        mid = ids_for("Alpha")[3]
        r.e.prim.ids.discard(mid)
        r.e.sec.ids.discard(mid)
        r.run()
        assert arr.deleted == [] and arr.commands == []
    finally:
        arr.close()
        r.close()


def test_kill_switch_turns_the_redownload_off(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch, auto_redownload=False)
    try:
        mid = ids_for("Alpha")[3]
        r.e.prim.ids.discard(mid)
        r.e.sec.ids.discard(mid)
        r.run()
        assert arr.deleted == []
    finally:
        arr.close()
        r.close()


def test_promote_mismatch_on_every_copy_ends_as_lost(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch)
    try:
        def promote(db, row, apply):
            r.calls.append(row["id"])
            st.set_state(db, row["id"], "failed")
            return Res(False, "failed", reason="chunk mismatch")

        r.deps.promote = promote
        r.runner.flagged.add(rid)
        st.log_event(r.db, rid, "weekly_flag", {"reasons": ["x"]})
        r.run()
        assert r.calls == [rid]
        assert r.runner.flagged == set()
        # marked lost, and acted on in the same pass because that night's check was clean
        assert arr.deleted == ["/api/v3/moviefile/10"]
        assert "lost_exact_file" in [e["kind"] for e in st.events(r.db, rid)]
        assert r.runner._redl.pending(r.db) == {}
    finally:
        arr.close()
        r.close()


def test_pending_lost_row_gets_no_new_promote(tmp_path, monkeypatch):
    r, rid, lib, arr = _lost_rig(tmp_path, monkeypatch)
    try:
        r.runner.redownloader().mark_lost(r.db, rid, "b", "test")
        r.runner.flagged.add(rid)
        r.runner.lost_hold = {rid}
        r.run()
        assert r.calls == []
    finally:
        arr.close()
        r.close()


