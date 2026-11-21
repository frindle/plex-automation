"""Proof tests from the PR #82 review. Each asserts the safe behaviour; a failure shows a bug."""
import os

from fakes_intake import APIKEY, FakeAltMount, FakeNewznab, make_nzb
from test_qualify import IDS, REL, World, idx
from test_weekly import DAY3, DAY15, Env, SpyReader, rotation_env
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q
from tiering.offload import weekly

ROW = {"release_name": REL, "title": "Movie", "year": 2020}


# qualify must compare the remote hash with the LOCAL file, not a stored sha256.
def test_qualify_fails_when_local_file_differs_from_stored_sha(tmp_path):
    w = World(tmp_path)
    try:
        lib = tmp_path / "lib.mkv"
        lib.write_bytes(os.urandom(50_000))  # what is on disk now (e.g. an arr upgrade)
        r = w.run({**w.row, "lib_path": str(lib)})  # row sha256 is stale and equals the remote
    finally:
        w.close()
    assert not r.ok, "qualify passed although the local file is not what AltMount streams"


# The same upload with one extra par2 in another indexer's NZB is not a restore path.
def test_same_upload_with_superset_nzb_is_not_a_restore_path():
    s, ix = idx((REL, make_nzb(IDS + ["extra-par2@t"])))
    try:
        assert q.score_restore(ROW, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


# The same upload (identical message-ids) under a tagged name is not a restore path.
def test_same_upload_under_tagged_name_is_not_a_restore_path():
    s, ix = idx((REL + "-xpost", make_nzb(IDS)))
    try:
        assert q.score_restore(ROW, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


# An untagged source (real 1080p, name says nothing, no is4k) must not drop the floor to 720.
def test_unknown_source_resolution_does_not_accept_720p():
    s, ix = idx(("Movie.2020.720p.WEB-DL-A", make_nzb(["z@t"])))
    try:
        row = {"release_name": "Movie.2020.BluRay.x264-GRP", "title": "Movie", "year": 2020, "is4k": 0}
        assert q.score_restore(row, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


# weekly must not write state="offloaded" over a row the runner changed during the run.
def test_weekly_does_not_reset_state_changed_mid_run(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    p, s = e.checkers()
    bravo = f"{1:016x}"

    def prim(nzb):
        e.state.rows[bravo]["state"] = "whole"  # the runner promoted Bravo meanwhile
        return p(nzb)

    try:
        weekly.run_weekly(None, stat_primary=prim, stat_secondary=s, indexers=e.indexers,
                          report_path=e.report, state=e.state, now=DAY15)
    finally:
        p.close()
        s.close()
        e.close()
    assert e.state.rows[bravo]["state"] == "whole"


# One indexer down (the one that lists the alternatives) must not flag every file for promotion.
def test_weekly_one_indexer_down_does_not_flag(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    up = FakeNewznab({})
    try:
        e.nz.fail = True
        e.indexers = nz.NewznabClient(e.indexers.indexers + [nz.Indexer("two", up.url, "nzkey")])
        assert e.run() == []
    finally:
        up.close()
        e.close()


# A row whose full read always fails must not freeze the monthly rotation without escalation.
def test_monthly_rotation_not_stuck_on_a_row_that_always_fails(tmp_path):
    e = rotation_env(tmp_path)
    try:
        good = {r["remote_path"]: r["sha256"] for r in e.state.rows.values()}
        good.pop("/N19.mkv")
        rd = SpyReader(good)
        p1 = e.run(reader=rd, now=DAY3)
        p2 = e.run(reader=rd, now=DAY3 + 31 * 86400)  # next month, day 4
    finally:
        e.close()
    escalated = any(r["title"] == "N19" for r in p1 + p2)
    assert escalated or rd.calls != ["/N19.mkv", "/N19.mkv"], rd.calls


# The AltMount API key must not be put in a request URL.
def test_altmount_key_not_in_request_url(monkeypatch):
    seen = []
    orig = am._Http.open

    def spy(self, method, path, headers=None, body=None):
        seen.append(path)
        return orig(self, method, path, headers, body)

    monkeypatch.setattr(am._Http, "open", spy)
    s = FakeAltMount("/mnt/am/x.mkv", pending_polls=0)
    try:
        am.ImportClient(s.url, APIKEY, poll_interval=0, sleep=lambda x: None).import_nzb(
            make_nzb(["a@x"]), "n")
    finally:
        s.close()
    assert not any(APIKEY in p for p in seen)


# TLS off must not be reachable from production env for a non-loopback host.
def test_tls_off_refused_for_remote_host():
    env = {"NNTP_PRIMARY_HOST": "news.example", "NNTP_PRIMARY_TLS": "0"}
    try:
        c = ns.ProviderCfg.from_env("primary", env)
    except Exception:
        return
    assert c.tls, "AUTHINFO PASS would go out in cleartext"
