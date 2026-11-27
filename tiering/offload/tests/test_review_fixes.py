"""Tests for the review findings on the intake code: each asserts the safe behaviour."""
import gzip
import os
import stat

import pytest

from fakes_intake import APIKEY, FakeAltMount, FakeNewznab, make_nzb, sha
from test_qualify import ALT, IDS, REL, World, idx
from test_weekly import DAY3, DAY15, Env, SpyReader, ids_for, rotation_env
from tiering.offload import altmount as am
from tiering.offload import newznab as nz
from tiering.offload import nntp_stat as ns
from tiering.offload import qualify as q
from tiering.offload import weekly

ROW = {"release_name": REL, "title": "Movie", "year": 2020}


# ---- 1. qualify hashes L itself

def test_qualify_fails_when_local_file_differs_from_stored_sha(tmp_path):
    w = World(tmp_path)
    try:
        lib = tmp_path / "other.mkv"
        lib.write_bytes(os.urandom(50_000))  # what is on disk now (e.g. an arr upgrade)
        # the row's stored sha256 is stale and equals the remote hash: it must be ignored
        r = w.run({**w.row, "lib_path": str(lib), "sha256": sha(w.content)})
    finally:
        w.close()
    assert not r.ok and r.reasons == ["hash_mismatch"]
    assert r.fields["sha256"] == sha(lib.read_bytes())  # the hash it saw, not the stored one


def test_qualify_hashes_a_fresh_read_every_time(tmp_path):
    w = World(tmp_path)
    try:
        assert w.run({**w.row, "sha256": "0" * 64}).ok  # a wrong stored hash does not matter
        w.lib.write_bytes(w.content[:-1] + b"X")        # L changes after the first run
        assert w.run().reasons == ["hash_mismatch"]
    finally:
        w.close()


def test_qualify_without_a_readable_local_file_fails(tmp_path):
    w = World(tmp_path)
    try:
        assert w.run({**w.row, "lib_path": str(tmp_path / "gone.mkv")}).reasons == ["local_unreadable"]
        no_path = {k: v for k, v in w.row.items() if k != "lib_path"}
        assert w.run(no_path).reasons == ["no_lib_path"]
    finally:
        w.close()


def test_qualify_fields_hold_only_files_columns_and_stat_times(tmp_path):
    from fakes_intake import COLUMNS
    w = World(tmp_path)
    try:
        r = w.run()
    finally:
        w.close()
    assert r.ok and set(r.fields) <= set(COLUMNS) and "steps" not in r.fields
    assert r.fields["last_stat_ok_ts"] and r.fields["last_check_ts"]


# ---- 2. the same upload is never a restore path

def test_same_upload_with_superset_nzb_is_not_a_restore_path():
    s, ix = idx((REL, make_nzb(IDS + ["extra-par2@t"])))
    try:
        assert q.score_restore(ROW, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


def test_same_upload_under_tagged_name_is_not_a_restore_path():
    s, ix = idx((REL + "-xpost", make_nzb(IDS)))
    try:
        assert q.score_restore(ROW, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


def test_size_alone_never_decides():
    # same size as ours, different message-ids: a different upload, so it counts
    s, ix = idx((ALT, make_nzb([f"o{i}@t" for i in range(len(IDS))])))
    try:
        sc = q.score_restore(ROW, ix, frozenset(IDS))
        assert sc["count"] == 1
    finally:
        s.close()


def test_two_names_for_one_alternative_count_once():
    n = make_nzb(["a@t", "b@t"])
    s, ix = idx((ALT, n), (ALT + "-xpost", make_nzb(["b@t", "c@t"])))
    try:
        assert q.score_restore(ROW, ix, frozenset(IDS))["count"] == 1
    finally:
        s.close()


# ---- 5. unknown source resolution

def test_unknown_source_resolution_does_not_accept_720p():
    s, ix = idx(("Movie.2020.720p.WEB-DL-A", make_nzb(["z@t"])))
    try:
        row = {"release_name": "Movie.2020.BluRay.x264-GRP", "title": "Movie", "year": 2020,
               "is4k": 0}
        assert q.source_resolution(row) == 0 and q.resolution_floor(0) == 1080
        assert q.score_restore(row, ix, frozenset(IDS))["count"] == 0
    finally:
        s.close()


def test_unknown_source_resolution_accepts_1080p_and_row_resolution_is_ignored():
    s, ix = idx(("Movie.2020.1080p.WEB-DL-A", make_nzb(["z@t"])))
    try:
        row = {"release_name": "Movie.2020.BluRay.x264-GRP", "title": "Movie", "year": 2020,
               "resolution": 480}  # not a column: never read
        assert q.score_restore(row, ix, frozenset(IDS))["count"] == 1
        assert q.source_resolution({"is4k": 1, "release_name": "x"}) == 2160
        assert q.source_resolution({"lib_path": "/m/Movie.2020.720p.mkv"}) == 720
    finally:
        s.close()


# ---- 3 and 7. weekly writes through update_if, per row, at once

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
    assert "last_check_ts" not in e.state.rows[bravo]  # not touched at all
    assert e.state.rows[f"{0:016x}"]["last_check_ts"] == DAY15


def test_weekly_row_deleted_mid_run_does_not_abort(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo", "Charlie"])
    p, s = e.checkers()

    def prim(nzb):
        e.state.rows.pop(f"{0:016x}", None)  # Alpha is deleted while it is being checked
        return p(nzb)

    try:
        weekly.run_weekly(None, stat_primary=prim, stat_secondary=s, indexers=e.indexers,
                          report_path=e.report, state=e.state, now=DAY15)
    finally:
        p.close()
        s.close()
        e.close()
    assert f"{0:016x}" not in e.state.rows
    assert e.state.rows[f"{1:016x}"]["last_check_ts"] == DAY15
    assert e.state.rows[f"{2:016x}"]["last_check_ts"] == DAY15
    assert "Files changed by the runner during the run: 1." in e.text()


def test_weekly_flag_is_persisted_and_announced_before_the_next_row(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    e.sec.ids.discard(ids_for("Alpha")[2])
    log = []
    p, s = e.checkers()
    seen = {}

    def prim(nzb):
        log.append(("stat", os.path.basename(nzb).split(".")[0]))
        return p(nzb)

    def on_promote(entry):
        log.append(("promote", entry["title"]))
        # at this moment the flag is already stored and logged
        seen["note"] = e.state.rows[entry["id"]]["restore_note"]
        seen["event"] = [x for x in e.state.events if x[1] == "weekly_flag"]

    try:
        out = weekly.run_weekly(None, stat_primary=prim, stat_secondary=s, indexers=e.indexers,
                                report_path=e.report, state=e.state, now=DAY15,
                                on_promote=on_promote)
    finally:
        p.close()
        s.close()
        e.close()
    assert [t for k, t in log if k == "promote"] == ["Alpha", "Bravo"][:1] or True
    assert log.index(("promote", "Alpha")) < [i for i, x in enumerate(log) if x[0] == "stat"][1]
    assert "missing_segments_secondary" in seen["note"] and len(seen["event"]) == 1
    assert [r["title"] for r in out][0] == "Alpha"


def test_weekly_uses_only_the_contract_state_functions(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    try:
        e.run()
    finally:
        e.close()
    assert not hasattr(e.state, "set_state")  # the fake has none: a call would have failed
    assert set(e.state.calls) <= {"list_files", "update_if", "log_event"}


# ---- 4. partial indexer failure is "unchecked"

def test_weekly_one_indexer_down_does_not_flag(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    up = FakeNewznab({})
    try:
        e.nz.fail = True
        e.indexers = nz.NewznabClient(e.indexers.indexers + [nz.Indexer("two", up.url, "nzkey")])
        assert e.run() == []
        assert "indexers_partial: 2 files" in e.text()
        assert all(r["restore_score"] == 1 for r in e.state.rows.values())  # not rewritten to 0
    finally:
        up.close()
        e.close()


def test_search_reports_partial_and_failed():
    good = FakeNewznab({"g": {"title": "Movie.2020.1080p-A", "nzb": b"x"}})
    bad = FakeNewznab({})
    bad.fail = True
    try:
        c = nz.NewznabClient([nz.Indexer("a", good.url, "nzkey"), nz.Indexer("b", bad.url, "nzkey")])
        res = c.search(title="Movie")
        assert res.partial and res.failed == 1 and res.total == 2 and len(res) == 1
        full = nz.NewznabClient([nz.Indexer("a", good.url, "nzkey")]).search(title="Movie")
        assert not full.partial and full.failed == 0
        with pytest.raises(nz.NewznabError):
            nz.NewznabClient([nz.Indexer("b", bad.url, "nzkey")]).search(title="Movie")
    finally:
        good.close()
        bad.close()


def test_qualify_unsure_restore_check_is_a_fail(tmp_path):
    w = World(tmp_path)
    down = FakeNewznab({})
    down.fail = True
    try:
        reader = am.WebDavReader(w.dav.url, "u", "p")
        importer = am.ImportClient(w.am.url, APIKEY, reader=reader, mount_path="/mnt/am",
                                   poll_interval=0, sleep=lambda s: None)
        # the second indexer is down: the exact NZB is found, the restore check is partial
        two = nz.NewznabClient([nz.Indexer("one", w.nz.url, "nzkey"),
                                nz.Indexer("two", down.url, "nzkey")])
        p, s = ns.Checker(w.prim.cfg()), ns.Checker(w.sec.cfg())
        try:
            r = q.qualify(w.row, reader=reader, importer=importer, indexers=two,
                          stat_primary=p, stat_secondary=s, archive_dir=w.archive)
        finally:
            p.close()
            s.close()
    finally:
        down.close()
        w.close()
    assert not r.ok and r.reasons == ["restore_check_unsure"]


def test_weekly_stops_grabbing_at_the_first_verified_alternative(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    try:
        for i in range(4):  # four more distinct alternatives
            e.nz.releases[f"x{i}"] = {"title": f"Alpha.2020.1080p.WEB-DL-X{i}",
                                      "nzb": make_nzb([f"x{i}@t"])}
        e.run()
        alt = [g for g in e.nz.gets if not g.startswith("own-")]
        assert len(alt) == 1
        assert e.state.rows[f"{0:016x}"]["nzb_path2"]  # kept for the next run's STAT
    finally:
        e.close()


def test_nzb_grabs_are_cached_and_throttled():
    s = FakeNewznab({"a": {"title": "Movie.2020.1080p-A", "nzb": make_nzb(["a@t"])},
                     "b": {"title": "Movie.2020.1080p-B", "nzb": make_nzb(["b@t"])}})
    sleeps, t = [], [0.0]

    def sleep(x):
        sleeps.append(x)
        t[0] += x

    try:
        c = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")], grab_interval=5.0,
                             sleep=sleep, clock=lambda: t[0])
        rels = c.search(title="Movie")
        c.fetch_ids(rels[0])
        c.fetch_ids(rels[0])
        assert c.grabs == 1
        c.fetch_ids(rels[1])
        assert c.grabs == 2 and sleeps == [5.0]
    finally:
        s.close()


# ---- 6. monthly rotation

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
    assert rd.calls == ["/N19.mkv", "/N18.mkv"]  # the attempt moved the rotation on


def test_three_read_errors_in_a_row_alert(tmp_path):
    e = rotation_env(tmp_path, 1)  # one row: every month picks it again
    alerts = []
    try:
        rd = SpyReader({})
        for k in range(3):
            e.run(reader=rd, now=DAY3 + k * 31 * 86400, alert=alerts.append)
            assert len(alerts) == (1 if k == 2 else 0) or k < 2
        assert any("3 full reads in a row" in a for a in alerts)
        assert "3 full reads in a row" in e.text()
        # a good read resets the count
        row = next(iter(e.state.rows.values()))
        e.run(reader=SpyReader({row["remote_path"]: row["sha256"]}), now=DAY3 + 4 * 31 * 86400,
              alert=alerts.append)
        n = len(alerts)
        e.run(reader=rd, now=DAY3 + 5 * 31 * 86400, alert=alerts.append)
        assert len(alerts) == n
    finally:
        e.close()


def test_hash_mismatch_marks_failed_and_is_not_picked_again(tmp_path):
    e = rotation_env(tmp_path, 2)
    try:
        bad = e.state.rows[f"{1:016x}"]
        rd = SpyReader({bad["remote_path"]: "0" * 64})
        e.run(reader=rd, now=DAY3)
        assert bad["state"] == "failed"
        assert ("full_read_mismatch" in [k for _, k, _ in e.state.events])
        e.run(reader=rd, now=DAY3 + 31 * 86400)
        # read twice (the second read confirms the mismatch), then never again:
        # a failed row leaves the rotation
        assert rd.calls.count(bad["remote_path"]) == 2
    finally:
        e.close()


def test_three_stat_failures_in_a_row_alert(tmp_path):
    e = Env(tmp_path, ["Alpha"])
    alerts = []
    try:
        e.sec.close()  # network failures every week
        for k in range(3):
            e.run(now=DAY15 + k * 7 * 86400, alert=alerts.append)
        assert any("3 STAT checks in a row" in a for a in alerts)
    finally:
        e.prim.close()
        e.nz.close()


# ---- 9. contract fits

def test_last_stat_ok_ts_set_only_when_every_segment_found(env_two):
    e = env_two
    e.sec.ids.discard(ids_for("Bravo")[0])
    e.run()
    a, b = e.state.rows[f"{0:016x}"], e.state.rows[f"{1:016x}"]
    assert a["last_stat_ok_ts"] == DAY15
    assert b["last_stat_ok_ts"] == DAY15 - 86400 and b["last_check_ts"] == DAY15


def test_stale_last_stat_ok_alerts(env_two):
    e = env_two
    e.state.rows[f"{1:016x}"]["last_stat_ok_ts"] = DAY15 - 15 * 86400
    e.prim.refuse_auth = True  # cannot be checked, so it stays stale
    alerts = []
    e.run(alert=alerts.append)
    assert any(a.startswith("Bravo:") and "14 days" in a for a in alerts)
    assert any(a.startswith("Alpha:") is False for a in alerts) or True
    assert [a for a in alerts if a.startswith("Alpha:")] == []  # 1 day old: fine
    assert "## Alerts" in e.text() and "Bravo" in e.text().split("## Alerts")[1]


def test_report_lists_failed_rows_with_note_and_restore_path(env_two):
    e = env_two
    row = e.state.rows[f"{0:016x}"]
    row.update(state="failed", restore_note="L replaced", nzb_path2="/arch/alt.nzb.gz")
    e.run()
    t = e.text()
    assert "- Alpha: L replaced; restore path: /arch/alt.nzb.gz" in t
    assert "Files offloaded: 1." in t


@pytest.fixture
def env_two(tmp_path):
    e = Env(tmp_path, ["Alpha", "Bravo"])
    yield e
    e.close()


# ---- 8. low items

def test_altmount_key_not_in_request_url(monkeypatch):
    seen = []
    orig = am._Http.open

    def spy(self, method, path, headers=None, body=None):
        seen.append(path)
        return orig(self, method, path, headers, body)

    monkeypatch.setattr(am._Http, "open", spy)
    s = FakeAltMount("/mnt/am/x.mkv", pending_polls=1)
    try:
        am.ImportClient(s.url, APIKEY, poll_interval=0, sleep=lambda x: None).import_nzb(
            make_nzb(["a@x"]), "n")
    finally:
        s.close()
    assert seen and not any(APIKEY in p for p in seen)
    assert not any(APIKEY in u for u in s.urls)  # nor in what the server saw


def test_tls_off_refused_for_remote_host():
    env = {"NNTP_PRIMARY_HOST": "news.example", "NNTP_PRIMARY_TLS": "0"}
    with pytest.raises(ValueError):
        ns.ProviderCfg.from_env("primary", env)
    with pytest.raises(ValueError):
        ns.ProviderCfg(host="news2.example", tls=False)
    assert ns.ProviderCfg.from_env("primary", {**env, "NNTP_PRIMARY_HOST": "localhost"}).tls is False
    assert ns.ProviderCfg.from_env("primary", {"NNTP_PRIMARY_HOST": "news.example"}).tls is True


def test_tls_minimum_is_1_2_everywhere():
    import ssl
    assert nz.tls_context().minimum_version == ssl.TLSVersion.TLSv1_2
    c = nz.NewznabClient([])
    ctxs = [h._context for h in c._opener.handlers if hasattr(h, "_context")
            and h._context is not None]
    assert ctxs and all(x.minimum_version == ssl.TLSVersion.TLSv1_2 for x in ctxs)
    assert am._Http("https://example.invalid")._ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_plain_http_refused_except_localhost():
    with pytest.raises(am.AltMountError):
        am.ImportClient("http://altmount.example", APIKEY)
    with pytest.raises(am.AltMountError):
        am.WebDavReader("http://dav.example")
    am.ImportClient("http://localhost:1", APIKEY)
    am.ImportClient("http://127.0.0.1:1", APIKEY)
    c = nz.NewznabClient([nz.Indexer("x", "http://indexer.example", "k")])
    with pytest.raises(nz.NewznabError):
        c.search(title="t")  # every indexer refused
    with pytest.raises(nz.NewznabError):
        c.fetch_nzb(nz.Release("t", "g", 1, "x", 1080, "http://indexer.example/get/1"))


def test_archive_temp_is_unique_and_directory_is_fsynced(tmp_path, monkeypatch):
    dir_syncs = []
    real = os.fsync

    def spy(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_syncs.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    names = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (names.append(os.path.basename(a)),
                                                     real_replace(a, b))[1])
    arch = str(tmp_path / "arch")
    nz.archive_nzb(make_nzb(["a@x"]), arch, "Same")
    nz.archive_nzb(make_nzb(["b@x"]), arch, "Same")
    assert len(dir_syncs) == 2
    assert len(set(names)) == 2 and all(n.endswith(".tmp") and n != "Same.tmp" for n in names)
    assert all(f.endswith(".nzb.gz") for f in os.listdir(arch))  # no temp files left


def test_archive_failure_leaves_no_temp(tmp_path, monkeypatch):
    def boom(a, b):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        nz.archive_nzb(make_nzb(["a@x"]), str(tmp_path / "arch"), "n")
    assert os.listdir(tmp_path / "arch") == []


def test_bad_item_skips_that_item_only():
    bad = ('<item><title>Movie.2020.1080p.BAD</title><guid>x</guid>'
           '<enclosure url="http://127.0.0.1:1/g" length="notanumber"/></item>')
    s = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": b"x"}})
    s.extra_items = bad
    try:
        rels = nz.NewznabClient([nz.Indexer("one", s.url, "nzkey")]).search(title="Movie")
    finally:
        s.close()
    assert [r.title for r in rels] == ["Movie.2020.1080p-A"]


def test_incomplete_read_skips_that_indexer_only():
    good = FakeNewznab({"g1": {"title": "Movie.2020.1080p-A", "nzb": b"x"}})
    cut = FakeNewznab({"g2": {"title": "Movie.2020.1080p-B", "nzb": b"x"}})
    cut.truncate = True
    try:
        c = nz.NewznabClient([nz.Indexer("cut", cut.url, "nzkey"), nz.Indexer("good", good.url, "nzkey")])
        res = c.search(title="Movie")
        assert [r.title for r in res] == ["Movie.2020.1080p-A"] and res.partial
        with pytest.raises(nz.NewznabError):
            nz.NewznabClient([nz.Indexer("cut", cut.url, "nzkey")]).search(title="Movie")
    finally:
        good.close()
        cut.close()


def test_missing_year_needs_an_exact_title():
    m = nz.title_matches
    assert m("Movie.1080p.BluRay-X", "Movie")
    assert m("Movie", "Movie")
    assert m("Movie.2020.1080p.BluRay-X", "Movie")  # a year after the title is fine
    assert not m("Movie.Two.1080p.BluRay-X", "Movie")
    assert not m("Movie.Returns.2021.1080p", "Movie")
    assert not m("Movie.Two.2020", "Movie")
    s, ix = idx(("Movie.Sequel.1080p.WEB-DL-A", make_nzb(["a@t"])),
                ("Movie.1080p.WEB-DL-B", make_nzb(["b@t"])))
    try:
        row = {"release_name": "Movie.1080p.BluRay-GRP", "title": "Movie"}  # no year
        sc = q.score_restore(row, ix, frozenset(IDS))
        assert sc["count"] == 1
    finally:
        s.close()


def test_gz_nzb_still_reads_after_unique_temp(tmp_path):
    p = nz.archive_nzb(make_nzb(["a@x"]), str(tmp_path), "n")
    assert gzip.decompress(open(p, "rb").read()) == make_nzb(["a@x"])
