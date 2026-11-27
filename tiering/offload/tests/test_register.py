import os

import pytest

from fakes_intake import FakeArr, FakeState
from tiering.offload import register as rg


def mk(tmp_path, name, data=b"x" * 100, mtime=None):
    p = tmp_path / "lib" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, ns=(mtime, mtime))
    return str(p)


def movie(path, **kw):
    return {"title": "Movie", "year": 2020, "imdbId": "tt1",
            "movieFile": {"path": path, "size": 100, "sceneName": "Movie.2020.1080p.BluRay-GRP"}, **kw}


@pytest.fixture
def arrs(tmp_path):
    made = []

    def make(**kw):
        a = FakeArr(**kw)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


def run(st, radarr=None, sonarr=None, apply=True, tmp_path=None, **kw):
    pm = f"/data={tmp_path}/lib" if tmp_path else None
    return rg.register(None, apply, state=st, radarrs=[rg.ArrClient(a.url, a.key) for a in radarr or []],
                       sonarrs=[rg.ArrClient(a.url, a.key) for a in sonarr or []], arr_map=pm, **kw)


def test_new_movie_row_with_identity_and_path_map(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    out = run(st, [r], tmp_path=tmp_path)
    assert out["new"] == 1 and out["applied"]
    (row,) = st.rows.values()
    assert row["lib_path"] == f"{tmp_path}/lib/m.mkv" and row["state"] == "whole"
    assert len(row["id"]) == 16 and int(row["id"], 16) >= 0
    assert (row["title"], row["year"], row["imdb_id"]) == ("Movie", 2020, "tt1")
    assert row["release_name"] == "Movie.2020.1080p.BluRay-GRP" and row["size"] == 100
    assert row["mtime_ns"] == os.stat(row["lib_path"]).st_mtime_ns
    assert row.get("sha256") is None
    assert r.requests and all(k == r.key for _, _, k in r.requests)
    assert not any(r.key in q for _, q, _ in r.requests)  # the key is never in a URL


def test_release_name_falls_back_to_file_name(tmp_path, arrs):
    mk(tmp_path, "Some.Name.mkv")
    m = movie("/data/Some.Name.mkv")
    m["movieFile"].pop("sceneName")
    st = FakeState()
    run(st, [arrs(movies=[m])], tmp_path=tmp_path)
    assert next(iter(st.rows.values()))["release_name"] == "Some.Name"


def test_episode_rows_and_multi_sonarr(tmp_path, arrs):
    mk(tmp_path, "s1e1.mkv")
    mk(tmp_path, "s1e2.mkv")
    mk(tmp_path, "multi.mkv")
    a = arrs(series=[{"id": 7, "title": "Show", "year": 2019, "tvdbId": 55, "imdbId": "tt9"}],
             episodes={7: [
                 {"seasonNumber": 1, "episodeNumber": 1,
                  "episodeFile": {"path": "/data/s1e1.mkv", "size": 100}},
                 {"seasonNumber": 1, "episodeNumber": 2,
                  "episodeFile": {"path": "/data/multi.mkv", "size": 100}},
                 {"seasonNumber": 1, "episodeNumber": 3,
                  "episodeFile": {"path": "/data/multi.mkv", "size": 100}},
                 {"seasonNumber": 1, "episodeNumber": 4}]})
    b = arrs(series=[{"id": 1, "title": "Other", "year": 2020, "tvdbId": 66}],
             episodes={1: [{"seasonNumber": 2, "episodeNumber": 5,
                            "episodeFile": {"path": "/data/s1e2.mkv", "size": 100}}]})
    st = FakeState()
    out = run(st, sonarr=[a, b], tmp_path=tmp_path)
    assert out["new"] == 3
    by = {os.path.basename(r["lib_path"]): r for r in st.rows.values()}
    assert (by["s1e1.mkv"]["season"], by["s1e1.mkv"]["episode"], by["s1e1.mkv"]["tvdb_id"]) == (1, 1, 55)
    assert by["multi.mkv"]["episode"] == 2  # one row for a multi-episode file
    assert (by["s1e2.mkv"]["title"], by["s1e2.mkv"]["season"], by["s1e2.mkv"]["episode"]) == ("Other", 2, 5)


def test_apply_false_writes_nothing(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    st = FakeState()
    out = run(st, [arrs(movies=[movie("/data/m.mkv")])], apply=False, tmp_path=tmp_path)
    assert out["new"] == 1 and out["applied"] is False
    assert st.rows == {} and not {"upsert", "update_if", "log_event"} & set(st.calls)


def test_second_run_is_unchanged_and_never_duplicates(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    out = run(st, [r], tmp_path=tmp_path)
    assert out["unchanged"] == 1 and out["new"] == 0 and len(st.rows) == 1


def test_changed_size_or_mtime_clears_sha_and_refreshes_identity(tmp_path, arrs):
    p = mk(tmp_path, "m.mkv", mtime=10**18)
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    (row,) = st.rows.values()
    row["sha256"] = "a" * 64
    assert run(st, [r], tmp_path=tmp_path)["unchanged"] == 1 and row["sha256"] == "a" * 64
    os.utime(p, ns=(10**18 + 5, 10**18 + 5))  # same size, new mtime
    r.movies[0]["title"] = "Movie Renamed"
    out = run(st, [r], tmp_path=tmp_path)
    assert out["updated"] == 1
    assert row["sha256"] is None and row["mtime_ns"] == 10**18 + 5 and row["title"] == "Movie Renamed"
    row["sha256"] = "b" * 64
    mk(tmp_path, "m.mkv", data=b"y" * 150, mtime=10**18 + 5)
    r.movies[0]["movieFile"]["size"] = 150
    run(st, [r], tmp_path=tmp_path)
    assert row["sha256"] is None and row["size"] == 150


@pytest.mark.parametrize("rt,ms", [
    ("1:42:13", 6133000), ("42:13", 2533000), ("0:22:01.5", 1321500), ("00:00:00", None),
    ("", None), (None, None), (5400, None), ("abc", None), ("1:2:3:4", None), ("-1:00", None),
    ("1:nan:00", None)])
def test_play_ms_reads_the_arrs_measured_run_time(rt, ms):
    assert rg.play_ms({"mediaInfo": {"runTime": rt}}) == ms
    assert rg.play_ms({}) is None and rg.play_ms({"mediaInfo": None}) is None


def test_play_ms_comes_from_the_movie_and_episode_file(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    mk(tmp_path, "e.mkv")
    m = movie("/data/m.mkv")
    m["movieFile"]["mediaInfo"] = {"runTime": "1:50:00"}
    s = arrs(series=[{"id": 7, "title": "Show", "tvdbId": 55}], episodes={7: [
        {"seasonNumber": 1, "episodeNumber": 1,
         "episodeFile": {"path": "/data/e.mkv", "size": 100, "mediaInfo": {"runTime": "21:30"}}}]})
    st = FakeState()
    run(st, [arrs(movies=[m])], [s], tmp_path=tmp_path)
    by = {os.path.basename(r["lib_path"]): r for r in st.rows.values()}
    assert by["m.mkv"]["play_ms"] == 110 * 60 * 1000 and by["e.mkv"]["play_ms"] == 1290 * 1000


def test_play_ms_is_refreshed_and_goes_with_a_changed_file(tmp_path, arrs):
    p = mk(tmp_path, "m.mkv", mtime=10**18)
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    (row,) = st.rows.values()
    assert row["play_ms"] is None
    row["sha256"] = "a" * 64
    r.movies[0]["movieFile"]["mediaInfo"] = {"runTime": "1:00:00"}  # the arr scanned it later
    assert run(st, [r], tmp_path=tmp_path)["updated"] == 1
    assert row["play_ms"] == 3600 * 1000 and row["sha256"] == "a" * 64
    assert run(st, [r], tmp_path=tmp_path)["unchanged"] == 1
    r.movies[0]["movieFile"].pop("mediaInfo")  # no answer is not a new value
    assert run(st, [r], tmp_path=tmp_path)["unchanged"] == 1 and row["play_ms"] == 3600 * 1000
    os.utime(p, ns=(10**18 + 5, 10**18 + 5))  # another file, not scanned yet: its time is unknown
    run(st, [r], tmp_path=tmp_path)
    assert row["play_ms"] is None and row["sha256"] is None


def test_identity_only_change_keeps_sha(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    (row,) = st.rows.values()
    row["sha256"] = "a" * 64
    r.movies[0]["imdbId"] = "tt2"
    run(st, [r], tmp_path=tmp_path)
    assert row["imdb_id"] == "tt2" and row["sha256"] == "a" * 64


def test_rows_not_whole_are_left_alone(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    (row,) = st.rows.values()
    row["state"] = "offloaded"
    r.movies[0]["title"] = "Changed"
    out = run(st, [r], tmp_path=tmp_path)
    assert out["skipped_state"] == 1 and row["title"] == "Movie"


def test_update_lost_to_a_state_change_is_counted_not_written(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    (row,) = st.rows.values()
    r.movies[0]["title"] = "Changed"
    real = st.update_if

    def racing(db, id, expect, **f):
        st.rows[id]["state"] = "demoting"  # the runner got there first
        return real(db, id, expect, **f)

    st.update_if = racing
    out = run(st, [r], tmp_path=tmp_path)
    assert out["updated"] == 0 and out["skipped_state"] == 1 and row["title"] == "Movie"


def test_never_deletes_and_logs_unlisted_only_when_every_arr_answered(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    s = arrs()
    st = FakeState()
    run(st, [r], [s], tmp_path=tmp_path)
    r.movies.clear()
    r.fail = True
    out = run(st, [r], [s], tmp_path=tmp_path)
    assert out["errors"] == 1 and out["unlisted"] == 0 and len(st.rows) == 1 and not st.events
    r.fail = False
    out = run(st, [r], [s], tmp_path=tmp_path)
    assert out["unlisted"] == 1 and len(st.rows) == 1
    assert [e[1] for e in st.events] == ["register_unlisted"]
    out = run(st, [r], [s], apply=False, tmp_path=tmp_path)
    assert len(st.events) == 1 and len(st.rows) == 1


def test_unlisted_is_not_logged_unless_both_radarr_and_sonarr_are_configured(tmp_path, arrs):
    st = FakeState([{"id": "a" * 16, "lib_path": "/x/a.mkv", "state": "whole"}])
    r = arrs()
    assert run(st, [r], tmp_path=tmp_path)["unlisted"] == 0
    assert run(st, None, [r], tmp_path=tmp_path)["unlisted"] == 0
    assert run(st, tmp_path=tmp_path)["unlisted"] == 0
    assert not st.events
    assert run(st, [r], [arrs()], tmp_path=tmp_path)["unlisted"] == 1


def test_size_comes_from_the_real_file_not_the_arr(tmp_path, arrs):
    lib = mk(tmp_path, "m.mkv", data=b"x" * 200)
    r = arrs(movies=[movie("/data/m.mkv")])  # the arr says 100
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    assert st.by_path(None, lib)["size"] == 200
    assert run(st, [r], tmp_path=tmp_path)["unchanged"] == 1


def test_a_changed_file_clears_every_fact_about_the_old_bytes(tmp_path, arrs):
    lib = mk(tmp_path, "m.mkv", data=b"n" * 200)
    st = FakeState([{"id": "a" * 16, "lib_path": lib, "state": "whole", "size": 100,
                     "mtime_ns": 1, "sha256": "old", "head_b3": "h", "tail_b3": "t",
                     "head_len": 1, "tail_len": 2, "nzb_path": "/arch/old.nzb.gz",
                     "remote_path": "/old/Movie.mkv", "restore_score": 2, "title": "Movie"}])
    r = arrs(movies=[movie("/data/m.mkv")])
    run(st, [r], tmp_path=tmp_path)
    row = st.rows["a" * 16]
    assert row["size"] == 200
    for k in ("sha256", "head_b3", "tail_b3", "head_len", "tail_len", "nzb_path", "remote_path"):
        assert row[k] is None, k


def test_unchanged_rows_are_not_rewritten_when_ids_differ_in_type(tmp_path, arrs):
    mk(tmp_path, "e1.mkv")
    s = arrs(series=[{"id": 7, "title": "Show", "year": 2019, "tvdbId": 55, "imdbId": "tt9"}],
             episodes={7: [{"seasonNumber": 1, "episodeNumber": 1,
                            "episodeFile": {"path": "/data/e1.mkv", "size": 100}}]})
    st = FakeState()
    run(st, None, [s], tmp_path=tmp_path)
    for row in st.rows.values():  # a database that stores every id as text
        row["tvdb_id"], row["season"], row["year"] = "55", "1", "2019"
    out = run(st, None, [s], tmp_path=tmp_path)
    assert out["updated"] == 0 and out["unchanged"] == 1


# ---- an arr rename of an offloaded file

ID = "0123456789abcdef"


def offloaded_row(tmp_path, old_name, **kw):
    return {"id": ID, "lib_path": f"{tmp_path}/lib/{old_name}", "state": "offloaded",
            "size": 100, "mtime_ns": 5, "sha256": "a" * 64, **kw}


def link(tmp_path, name, ident=ID, ext=".mkv"):
    p = tmp_path / "lib" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(f"/tfs/{ident}{ext}", p)
    return str(p)


def test_arr_rename_of_an_offloaded_file_moves_the_row_to_the_new_path(tmp_path, arrs):
    link(tmp_path, "new.mkv")  # the arr renamed old.mkv (a tierfs symlink) to new.mkv
    st = FakeState([offloaded_row(tmp_path, "old.mkv")])
    out = run(st, [arrs(movies=[movie("/data/new.mkv")])], tmp_path=tmp_path)
    assert out["renamed"] == 1 and out["new"] == 0 and out["renamed_skipped"] == 0
    assert list(st.rows) == [ID], "no second row for the symlink"
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/new.mkv"
    assert st.rows[ID]["state"] == "offloaded" and st.rows[ID]["sha256"] == "a" * 64


def test_a_second_link_to_the_same_entry_is_not_adopted(tmp_path, arrs):
    link(tmp_path, "old.mkv")
    link(tmp_path, "copy.mkv")  # both names hold the link: a copy, not a rename
    st = FakeState([offloaded_row(tmp_path, "old.mkv")])
    out = run(st, [arrs(movies=[movie("/data/copy.mkv")])], tmp_path=tmp_path)
    assert out["renamed"] == 0 and out["renamed_skipped"] == 1 and out["new"] == 0
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/old.mkv"


def test_only_offloaded_or_failed_rows_are_adopted(tmp_path, arrs):
    link(tmp_path, "new.mkv")
    st = FakeState([offloaded_row(tmp_path, "old.mkv", state="promoting")])
    out = run(st, [arrs(movies=[movie("/data/new.mkv")])], tmp_path=tmp_path)
    assert out["renamed"] == 0 and out["renamed_skipped"] == 1
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/old.mkv"


def test_a_link_with_another_extension_or_mount_is_not_ours(tmp_path, arrs, monkeypatch):
    link(tmp_path, "new.mkv", ext=".mp4")
    st = FakeState([offloaded_row(tmp_path, "old.mkv")])
    run(st, [arrs(movies=[movie("/data/new.mkv")])], tmp_path=tmp_path)
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/old.mkv"
    monkeypatch.setenv("TIERFS_MOUNT", "/elsewhere")
    os.unlink(tmp_path / "lib" / "new.mkv")
    link(tmp_path, "new.mkv")
    run(st, [arrs(movies=[movie("/data/new.mkv")])], tmp_path=tmp_path)
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/old.mkv"


def test_adopting_writes_nothing_on_a_dry_run(tmp_path, arrs):
    link(tmp_path, "new.mkv")
    st = FakeState([offloaded_row(tmp_path, "old.mkv")])
    out = run(st, [arrs(movies=[movie("/data/new.mkv")])], apply=False, tmp_path=tmp_path)
    assert out["renamed"] == 1
    assert st.rows[ID]["lib_path"] == f"{tmp_path}/lib/old.mkv"


def test_pool_paths_swaps_the_disk_for_each_pool():
    there = {"/mnt/cache/media/tv/e.mkv", "/mnt/nvme/tv/e.mkv"}
    dm = [("/mnt/user/media", "/mnt/disk1/media"), ("/mnt/user/media", "/mnt/disk2/media")]
    assert rg.pool_paths("/mnt/user/media/tv/e.mkv", dm, ["cache", "nvme"], exists=there.__contains__) \
        == ["/mnt/cache/media/tv/e.mkv"]  # the same pool copy from both disks is one path
    assert rg.pool_paths("/mnt/user/media/tv/e.mkv", [("/mnt/user/media", "/mnt/disk1")], ["nvme"],
                         exists=lambda p: p == "/mnt/nvme/tv/e.mkv") == ["/mnt/nvme/tv/e.mkv"]
    assert rg.pool_paths("/mnt/user/other/e.mkv", dm, ["cache"], exists=lambda p: True) == []
    assert rg.pool_paths("/mnt/user/media/e.mkv", [("/mnt/user/media", "/srv/a")], ["cache"],
                         exists=lambda p: True) == []  # a disk prefix not under /mnt gives none


def test_pool_names_skips_disks_and_share_mounts(tmp_path):
    for n in ("disk1", "disk12", "user", "user0", "disks", "remotes", "addons", "rootshare", "cache", "nvme"):
        (tmp_path / n).mkdir()
    (tmp_path / "afile").write_text("x")
    assert rg.pool_names(env={}, mnt=str(tmp_path)) == ["cache", "nvme"]
    assert rg.pool_names(env={"TIERING_POOLS": " fast, cache "}, mnt=str(tmp_path)) == ["fast", "cache"]
    assert rg.pool_names(env={}, mnt=str(tmp_path / "nope")) == []


def test_a_new_file_still_on_a_pool_is_counted_on_pool_not_missing(tmp_path, arrs):
    mk(tmp_path, "here.mkv")
    r = arrs(movies=[movie("/data/here.mkv"), movie("/data/new.mkv"), movie("/data/gone.mkv")])
    st = FakeState()
    pooled = {f"{tmp_path}/lib/new.mkv"}
    out = run(st, [r], tmp_path=tmp_path, on_pool=pooled.__contains__)
    assert (out["listed"], out["new"], out["on_pool"], out["missing_local"]) == (3, 1, 1, 1), out
    assert [os.path.basename(x["lib_path"]) for x in st.rows.values()] == ["here.mkv"]
    mk(tmp_path, "new.mkv")  # the mover put it on the array: registered on the next pass
    out = run(st, [r], tmp_path=tmp_path, on_pool=lambda p: False)
    assert (out["new"], out["on_pool"], out["missing_local"]) == (1, 0, 1), out


def test_real_file_at_a_lost_rows_path_is_a_new_whole_row(tmp_path, arrs):
    p = mk(tmp_path, "m.mkv")
    old = {"id": "00000000000000aa", "state": "failed", "lib_path": p,
           "restore_note": "lost: re-download requested"}
    st = FakeState([old])
    out = run(st, [arrs(movies=[movie("/data/m.mkv")])], tmp_path=tmp_path)
    assert out["lost_replaced"] == 1 and out["new"] == 1
    assert st.rows["00000000000000aa"]["lib_path"] is None
    assert st.rows["00000000000000aa"]["restore_note"] == "lost: re-downloaded"
    fresh = [r for r in st.rows.values() if r["id"] != "00000000000000aa"]
    assert len(fresh) == 1 and fresh[0]["state"] == "whole" and fresh[0]["lib_path"] == p


def test_a_lost_row_whose_path_is_still_a_symlink_is_left_alone(tmp_path, arrs):
    p = tmp_path / "lib" / "m.mkv"
    p.parent.mkdir(parents=True)
    os.symlink("/tfs/00000000000000aa.mkv", p)
    old = {"id": "00000000000000aa", "state": "failed", "lib_path": str(p),
           "restore_note": "lost: re-download requested"}
    st = FakeState([old])
    out = run(st, [arrs(movies=[movie("/data/m.mkv")])], tmp_path=tmp_path)
    assert out["lost_replaced"] == 0 and st.rows["00000000000000aa"]["lib_path"] == str(p)
