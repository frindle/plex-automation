"""The nightly check: time budget and cursor, the 3-day alert, and lost files."""
import pytest

from fakes_intake import make_nzb
from test_weekly import DAY15, Env, ids_for
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import weekly

DAY = 86400


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo", "Charlie"], alts=["Alpha", "Bravo", "Charlie"])
    yield e
    e.close()


def make_clock():
    t = [0.0]

    def clock():
        t[0] += 10.0  # every look at the clock is 10 s later
        return t[0]
    return clock


def test_three_day_alert_when_the_stat_is_stale(env):
    env.prim.refuse_auth = True  # unknown: last_stat_ok_ts is not refreshed
    for r in env.state.rows.values():
        r["last_stat_ok_ts"] = DAY15 - 4 * DAY
    env.state.rows[f"{0:016x}"]["last_stat_ok_ts"] = DAY15 - 2 * DAY  # still fine
    got = []
    env.run(alert=got.append)
    assert len(got) == 2 and not any("Alpha" in g for g in got)


def test_two_day_old_stat_does_not_alert(env):
    env.prim.refuse_auth = True
    for r in env.state.rows.values():
        r["last_stat_ok_ts"] = DAY15 - 2 * DAY
    got = []
    env.run(alert=got.append)
    assert got == []


def test_time_budget_stops_and_the_oldest_rows_go_first(env):
    ids = [f"{k:016x}" for k in range(3)]
    for k, i in enumerate(ids):
        env.state.rows[i]["last_check_ts"] = DAY15 - (10 - k) * DAY  # ids[0] oldest
    summary = {}
    env.run(budget_s=15.0, clock=make_clock(), summary=summary)
    checked = [i for i in ids if env.state.rows[i]["last_check_ts"] == DAY15]
    assert checked == ids[:1] or len(checked) == 2, "the budget stops the loop early"
    assert summary["finished"] is False and summary["unreached"]
    first_unreached = summary["unreached"][0]
    # unreached rows keep their old last_stat_ok_ts
    assert env.state.rows[first_unreached]["last_stat_ok_ts"] == DAY15 - DAY
    assert "Time budget spent" in env.text()
    # the next night starts with the rows not reached
    env.run(budget_s=15.0, clock=make_clock(), summary={}, now=DAY15 + DAY)
    assert env.state.rows[first_unreached]["last_check_ts"] == DAY15 + DAY


def test_no_budget_checks_every_row(env):
    summary = {}
    env.run(summary=summary)
    assert summary["finished"] is True and summary["unreached"] == []
    assert all(r["last_check_ts"] == DAY15 for r in env.state.rows.values())


def test_promoting_rows_are_checked_too(env):
    env.state.rows[f"{1:016x}"]["state"] = "promoting"
    env.run()
    assert env.state.rows[f"{1:016x}"]["last_check_ts"] == DAY15


def _gone_everywhere(env, name, k=4):
    mid = ids_for(name)[k]
    env.prim.ids.discard(mid)
    env.sec.ids.discard(mid)
    return mid


def test_article_gone_on_every_provider_is_lost(env):
    _gone_everywhere(env, "Bravo")
    hooked = []
    summary = {}
    env.run(lost_hook=lambda lost, info: hooked.append((lost, info)), summary=summary)
    assert [lo["id"] for lo in hooked[0][0]] == [f"{1:016x}"]
    assert hooked[0][1]["provider_trouble"] is False
    assert summary["lost"][0]["id"] == f"{1:016x}"
    assert any(e[1] == "articles_gone" for e in env.state.events)


def test_gone_on_one_provider_only_is_not_lost(env):
    env.sec.ids.discard(ids_for("Bravo")[4])
    hooked = []
    env.run(lost_hook=lambda lost, info: hooked.append(lost))
    assert hooked == [[]]


def test_restat_that_finds_the_article_cancels_the_loss(env, monkeypatch):
    mid = _gone_everywhere(env, "Bravo")
    orig = ns.Checker.check_ids

    def check_ids(self, ids):  # the article is back by the second look
        env.prim.ids.add(mid)
        env.sec.ids.add(mid)
        return orig(self, ids)

    monkeypatch.setattr(ns.Checker, "check_ids", check_ids)
    hooked = []
    env.run(lost_hook=lambda lost, info: hooked.append(lost))
    assert hooked == [[]]


def test_a_live_twin_means_not_lost(env):
    _gone_everywhere(env, "Bravo")
    twin = nz.archive_nzb(make_nzb(["twin-1@t"]), str(env.tmp / "arch"), "Bravo.twin")
    env.prim.ids.add("twin-1@t")
    env.sec.ids.add("twin-1@t")
    env.state.rows[f"{1:016x}"]["twin_nzb_path"] = twin
    hooked = []
    env.run(lost_hook=lambda lost, info: hooked.append(lost))
    assert hooked == [[]]


def test_provider_trouble_is_reported_to_the_hook(env):
    _gone_everywhere(env, "Bravo")
    env.sec.refuse_auth = True
    hooked = []
    env.run(lost_hook=lambda lost, info: hooked.append((lost, info)))
    lost, info = hooked[0]
    assert info["provider_trouble"] is True and lost == [], "unknown never counts as lost"


def test_report_lists_lost_rows_by_id(env):
    rid = f"{1:016x}"
    env.state.rows[rid]["state"] = "failed"
    env.state.rows[rid]["restore_note"] = "lost: re-download requested"
    env.run()
    assert "Lost files" in env.text() and rid in env.text()


def test_stale_days_is_three():
    assert weekly.STALE_DAYS == 3
