"""Frozen tests for torrent_breakdown.breakdown (pure function)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import torrent_breakdown as T  # noqa: E402

DAY = 86400


def tor(name, label="", state="Seeding", days=1, path="/data/Downloads/Complete"):
    return {"name": name, "label": label, "state": state, "progress": 100.0,
            "seeding_time": int(days * DAY), "save_path": path}


def test_counts_by_label_lowercased_and_none():
    r = T.breakdown({
        "a": tor("a", "Sonarr"), "b": tor("b", "sonarr"), "c": tor("c", "radarr"),
        "d": tor("d", ""), "e": {"name": "e", "state": "Seeding", "seeding_time": 0, "save_path": "/x"},
    })
    assert r["total"] == 5, r
    assert r["by_label"] == {"sonarr": 2, "radarr": 1, "(none)": 2}, r


def test_counts_by_state():
    r = T.breakdown({"a": tor("a", state="Seeding"), "b": tor("b", state="Seeding"),
                     "c": tor("c", "sonarr", state="Paused"), "d": tor("d", state="Error")})
    assert r["by_state"] == {"Seeding": 2, "Paused": 1, "Error": 1}, r


def test_other_group_excludes_sonarr_and_radarr_and_counts_unknown_labels():
    r = T.breakdown({"a": tor("a", "sonarr"), "b": tor("b", "Radarr-upgrade"), "c": tor("c", ""),
                     "d": tor("d", "mylabel"), "e": tor("e", "SONARR-x")})
    assert r["other"]["count"] == 2, r
    assert sorted(r["other"]["sample_names"]) == ["c", "d"], r


def test_other_age_buckets_and_save_path_prefix():
    r = T.breakdown({
        "a": tor("a", days=0), "b": tor("b", days=13.9), "c": tor("c", days=14),
        "d": tor("d", days=20.9), "e": tor("e", days=21), "f": tor("f", days=90, path="/data/Movies/x/y/z"),
        "g": tor("g", days=1, path="/data/Downloads/Complete/Show/"), "h": tor("h", days=1, path=""),
        "s": tor("s", "sonarr", days=99),
    })
    o = r["other"]
    assert o["count"] == 8, o
    assert o["by_age"] == {"<14d": 4, "14-21d": 2, "21d+": 2}, o
    assert o["by_save_path"] == {"/data/Downloads/Complete": 6, "/data/Movies/x": 1, "(none)": 1}, o


def test_sample_names_capped_at_15_and_empty_input():
    big = {str(i): tor("n%02d" % i) for i in range(40)}
    r = T.breakdown(big)
    names = r["other"]["sample_names"]
    assert len(names) == 15 and len(set(names)) == 15 and set(names) <= {"n%02d" % i for i in range(40)}, names
    assert r["other"]["count"] == 40, r
    e = T.breakdown({})
    assert e["total"] == 0 and e["by_label"] == {} and e["by_state"] == {}, e
    assert e["other"]["count"] == 0 and e["other"]["sample_names"] == [], e
    assert e["other"]["by_age"] == {"<14d": 0, "14-21d": 0, "21d+": 0}, e
    assert e["other"]["by_save_path"] == {}, e
