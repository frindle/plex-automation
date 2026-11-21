import logging
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
                       sonarrs=[rg.ArrClient(a.url, a.key) for a in sonarr or []], path_map=pm, **kw)


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
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    r.movies.clear()
    r.fail = True
    out = run(st, [r], tmp_path=tmp_path)
    assert out["errors"] == 1 and out["unlisted"] == 0 and len(st.rows) == 1 and not st.events
    r.fail = False
    out = run(st, [r], tmp_path=tmp_path)
    assert out["unlisted"] == 1 and len(st.rows) == 1
    assert [e[1] for e in st.events] == ["register_unlisted"]
    out = run(st, [r], apply=False, tmp_path=tmp_path)
    assert len(st.events) == 1 and len(st.rows) == 1


def test_file_missing_locally_is_skipped(tmp_path, arrs):
    st = FakeState()
    out = run(st, [arrs(movies=[movie("/data/gone.mkv")])], tmp_path=tmp_path)
    assert out["missing_local"] == 1 and st.rows == {}


def test_one_arr_down_does_not_stop_the_others(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    good = arrs(movies=[movie("/data/m.mkv")])
    bad = arrs()
    bad.fail = True
    st = FakeState()
    out = run(st, [bad, good], tmp_path=tmp_path)
    assert out["new"] == 1 and out["errors"] == 1


def test_key_never_logged(tmp_path, arrs, caplog):
    r = arrs()
    r.fail = True
    with caplog.at_level(logging.DEBUG):
        run(FakeState(), [r], tmp_path=tmp_path)
    assert r.key not in caplog.text and r.url not in caplog.text
    assert r.key not in repr(rg.ArrClient(r.url, r.key))


def test_path_map_longest_prefix_and_boundaries():
    pm = rg.parse_path_map("/data=/mnt/a, /data/tv=/mnt/b ,junk")
    assert rg.map_path("/data/tv/x.mkv", pm) == "/mnt/b/x.mkv"
    assert rg.map_path("/data/m.mkv", pm) == "/mnt/a/m.mkv"
    assert rg.map_path("/database/m.mkv", pm) == "/database/m.mkv"
    assert rg.map_path("/other/m.mkv", pm) == "/other/m.mkv"


def test_env_config_comma_list_and_key_rules():
    env = {"RADARR_URL": "http://127.0.0.1:1", "RADARR_API_KEY": "k",
           "SONARR_URL": "http://127.0.0.1:1, http://localhost:2", "SONARR_API_KEY": "s"}
    rad, son = rg.clients_from_env(env)
    assert len(rad) == 1 and len(son) == 2
    with pytest.raises(rg.ArrError):
        rg.clients_from_env({**env, "SONARR_API_KEY": "a,b,c"})
    assert rg.clients_from_env({}) == ([], [])


def test_plain_http_refused_except_localhost_or_opt_in():
    with pytest.raises(rg.ArrError):
        rg.ArrClient("http://arr.example", "k")
    rg.ArrClient("http://localhost:1", "k")
    rg.ArrClient("http://arr.example", "k", allow_http=True)


def test_uses_only_contract_state_functions(tmp_path, arrs):
    mk(tmp_path, "m.mkv")
    r = arrs(movies=[movie("/data/m.mkv")])
    st = FakeState()
    run(st, [r], tmp_path=tmp_path)
    run(st, [r], tmp_path=tmp_path)
    assert set(st.calls) <= {"by_path", "upsert", "update_if", "list_files", "log_event"}
