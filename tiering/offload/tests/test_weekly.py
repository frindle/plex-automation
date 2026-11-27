import calendar
import hashlib
import os

import pytest

from fakes_intake import FakeNewznab, FakeNntp, FakeState, make_nzb
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import weekly

DAY15 = calendar.timegm((2026, 11, 15, 3, 0, 0))
DAY3 = calendar.timegm((2026, 11, 3, 3, 0, 0))


def ids_for(n):
    return [f"{n}-{i}@t" for i in range(12)]


class Env:
    def __init__(self, tmp_path, names, alts=None):
        self.tmp = tmp_path
        self.names = names
        # the alternatives' articles are on both providers: an alternative must be live
        live = [i for n in names for i in ids_for(n)] + [f"alt-{n}@t" for n in names]
        self.prim = FakeNntp(live)
        self.sec = FakeNntp(live)
        rels = {}
        for n in (alts if alts is not None else names):
            rels[f"alt-{n}"] = {"title": f"{n}.2020.1080p.WEB-DL-ALT", "nzb": make_nzb([f"alt-{n}@t"])}
            rels[f"own-{n}"] = {"title": f"{n}.2020.1080p.BluRay-GRP", "nzb": make_nzb(ids_for(n))}
        self.nz = FakeNewznab(rels)
        self.indexers = nz.NewznabClient([nz.Indexer("one", self.nz.url, "nzkey")])
        rows = []
        for k, n in enumerate(names):
            p = nz.archive_nzb(make_nzb(ids_for(n)), str(tmp_path / "arch"), f"{n}.2020.1080p.BluRay-GRP")
            rows.append({"id": f"{k:016x}", "state": "offloaded", "title": n, "year": 2020,
                         "release_name": f"{n}.2020.1080p.BluRay-GRP",
                         "nzb_path": p, "size": 1000, "sha256": "x", "remote_path": f"/{n}.mkv",
                         "restore_score": 1, "last_stat_ok_ts": DAY15 - 86400})
        self.state = FakeState(rows)
        self.report = str(tmp_path / "report.md")

    def checkers(self):
        return ns.Checker(self.prim.cfg()), ns.Checker(self.sec.cfg())

    def run(self, **kw):
        p, s = self.checkers()
        kw.setdefault("now", DAY15)
        try:
            return weekly.run_weekly(None, stat_primary=p, stat_secondary=s, indexers=self.indexers,
                                     report_path=self.report, state=self.state, **kw)
        finally:
            p.close()
            s.close()

    def text(self):
        return open(self.report).read()

    def close(self):
        for x in (self.prim, self.sec, self.nz):
            x.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo", "Charlie", "Delta"], alts=["Alpha", "Bravo", "Delta"])
    yield e
    e.close()


def test_flags_missing_segment_and_lost_restore_path(env):
    env.sec.ids.discard(ids_for("Bravo")[4])
    promote = env.run()
    by = {r["title"]: r["promote_reasons"] for r in promote}
    assert by == {"Bravo": ["missing_segments_secondary"], "Charlie": ["no_restore_path"]}
    st = env.state.rows
    assert st[f"{0:016x}"]["last_check_ts"] == DAY15
    assert st[f"{0:016x}"]["restore_score"] == 1
    assert st[f"{2:016x}"]["restore_score"] == 0
    assert all(r["state"] == "offloaded" for r in st.values())  # nothing promoted here
    assert {e[0] for e in env.state.events if e[1] == "weekly_flag"} == {f"{1:016x}", f"{2:016x}"}
    t = env.text()
    for line in ("Files offloaded: 4.", "Files checked: 4.", "Files OK: 2.", "Files flagged: 2.",
                 "Files that failed to check: 0.", "Files that could not be checked: 0.",
                 "Promotions requested: 2.", "Restore paths lost: 1.", "Bytes offloaded: 3.9 KiB."):
        assert line in t
    assert "- Bravo: missing_segments_secondary" in t and "- Charlie: no_restore_path" in t
    assert "127.0.0.1" not in t


def test_missing_on_primary_flags_too(env):
    env.prim.ids.discard(ids_for("Alpha")[0])
    assert {r["title"] for r in env.run()} == {"Alpha", "Charlie"}
    assert "Alpha: missing_segments_primary" in env.text()


def test_auth_error_does_not_flag_rows(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo", "Charlie"])
    try:
        e.prim.refuse_auth = True
        promote = e.run()
        assert promote == []
        t = e.text()
        assert "Files flagged: 0." in t and "Promotions requested: 0." in t
        assert "Files that could not be checked: 3." in t
        assert "primary_auth: 3 files" in t
        assert "The primary provider refused the login." in t
        assert e.prim.connections <= 3  # asked once, then left alone for the rest of the run
        assert all("last_ok_ts" not in r for r in e.state.rows.values())
    finally:
        e.close()


def test_quota_error_is_reported_separately(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    try:
        e.sec.quota = True
        assert e.run() == []
        assert "secondary_quota: 2 files" in e.text()
    finally:
        e.close()


def test_provider_down_is_failed_not_flagged(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    try:
        e.sec.close()
        assert e.run() == []
        assert "Files that failed to check: 1." in e.text()
        assert "secondary_network: 1 files" in e.text()
    finally:
        e.prim.close()
        e.nz.close()


def test_missing_nzb_file_is_unchecked_not_flagged(env):
    os.remove(env.state.rows[f"{0:016x}"]["nzb_path"])
    promote = env.run()
    assert "Alpha" not in [r["title"] for r in promote]
    assert "no_nzb: 1 files" in env.text()


def test_unreachable_indexers_do_not_flag(env):
    env.nz.fail = True
    assert env.run() == []
    assert "indexers_unreachable: 4 files" in env.text()


def test_no_indexers_configured_does_not_flag(env):
    env.indexers = nz.NewznabClient([])
    assert env.run() == []


def test_top_20_flagged_by_title(tmp_path):
    names = [f"T{i:02d}" for i in range(25)]
    e = Env(tmp_path, names, alts=[])
    try:
        promote = e.run()
        assert len(promote) == 25
        t = e.text()
        lines = [x for x in t.splitlines() if x.startswith("- T")]
        assert len(lines) == 20 and lines[0].startswith("- T00") and lines[-1].startswith("- T19")
        assert "- and 5 more." in t
    finally:
        e.close()


class SpyReader:
    def __init__(self, good):
        self.calls, self.good = [], good

    def sha256_full(self, path):
        self.calls.append(path)
        if path in self.good:
            return self.good[path]
        raise am.AltMountError("boom")


def rotation_env(tmp_path, n=20):
    e = Env(tmp_path, [f"N{i:02d}" for i in range(n)])
    for k, row in enumerate(e.state.rows.values()):
        row["last_ok_ts"] = 1000 + (n - k) * 10  # the last row is the oldest
        row["sha256"] = hashlib.sha256(row["remote_path"].encode()).hexdigest()
    return e


def test_monthly_full_read_rotates_by_oldest_last_ok(tmp_path):
    e = rotation_env(tmp_path)
    try:
        good = {r["remote_path"]: r["sha256"] for r in e.state.rows.values()}
        rd = SpyReader(good)
        e.run(reader=rd, now=DAY3)  # 5% of 20 rows of equal size is one row
        assert rd.calls == ["/N19.mkv"]
        assert e.state.rows[f"{19:016x}"]["last_ok_ts"] == DAY3
        e.run(reader=rd, now=DAY3 + 60)
        assert rd.calls == ["/N19.mkv", "/N18.mkv"]  # moved on to the next oldest
        assert "Full read check: 1 files" in e.text()
    finally:
        e.close()


def test_full_read_not_run_after_first_week_unless_forced(tmp_path):
    e = rotation_env(tmp_path, 4)
    try:
        rd = SpyReader({})
        e.run(reader=rd, now=DAY15)
        assert rd.calls == []
        assert "No full read check this week." in e.text()
        e.run(reader=rd, now=DAY15, monthly=True)
        assert len(rd.calls) == 1
    finally:
        e.close()


def test_full_read_mismatch_flags_and_keeps_old_last_ok(tmp_path):
    e = rotation_env(tmp_path, 4)
    try:
        oldest = e.state.rows[f"{3:016x}"]
        before = oldest["last_ok_ts"]
        rd = SpyReader({oldest["remote_path"]: "0" * 64})
        alerts = []
        promote = e.run(reader=rd, now=DAY3, alert=alerts.append)
        assert promote == []  # promoting cannot help: the remote copy is bad
        assert oldest["state"] == "failed" and oldest["last_ok_ts"] == before
        assert "hash mismatch" in oldest["restore_note"]
        assert any("hash mismatch" in a and "N03" in a for a in alerts)
        t = e.text()
        assert "1 mismatches" in t and "Files marked failed this run: 1." in t
        assert "## Failed files" in t and "- N03: full read hash mismatch" in t
        assert oldest["nzb_path2"] and f"restore path: {oldest['nzb_path2']}" in t
        assert ("N03" in t.split("## Alerts")[1].split("##")[0])
    finally:
        e.close()


def test_full_read_error_is_failed_not_flagged(tmp_path):
    e = rotation_env(tmp_path, 4)
    try:
        assert e.run(reader=SpyReader({}), now=DAY3) == []
        assert "Files that failed to check: 1." in e.text()
        assert "full_read_AltMountError: 1 files" in e.text()
    finally:
        e.close()


def test_does_not_touch_non_offloaded_rows(env):
    env.state.rows[f"{0:016x}"]["state"] = "whole"
    env.run()
    assert "Files offloaded: 3." in env.text()
    assert "last_check_ts" not in env.state.rows[f"{0:016x}"]


def test_single_provider_checks_the_primary_alone(env):
    """TIERING_SINGLE_PROVIDER: stat_secondary is None; a segment the primary lost still flags."""
    env.sec.ids.clear()  # would flag every row if the secondary were asked
    env.prim.ids.discard(ids_for("Bravo")[4])
    p = ns.Checker(env.prim.cfg())
    try:
        promote = weekly.run_weekly(None, stat_primary=p, stat_secondary=None, indexers=env.indexers,
                                    report_path=env.report, state=env.state, now=DAY15)
    finally:
        p.close()
    by = {r["title"]: r["promote_reasons"] for r in promote}
    assert by == {"Bravo": ["missing_segments_primary"], "Charlie": ["no_restore_path"]}
    assert env.sec.stats == 0
    assert env.state.rows[f"{0:016x}"]["last_stat_ok_ts"] == DAY15
