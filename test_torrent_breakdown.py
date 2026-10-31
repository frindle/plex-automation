"""Frozen tests for torrent_breakdown.breakdown (pure function)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import torrent_breakdown as T  # noqa: E402

DAY = 86400
MOV = "/data/Media/Movies"
TV = "/data/Media/TV Shows"
ASIAN = "/data/Media/TV Shows - Asian"


def tor(name="x", label="", state="Seeding", days=1, path="/data/Downloads/Complete"):
    return {"name": name, "label": label, "state": state, "progress": 100.0,
            "seeding_time": int(days * DAY), "save_path": path}


def test_sonarr_group_by_label_state_and_age():
    r = T.breakdown({
        "a": tor(label="sonarr", state="Seeding", days=0),
        "b": tor(label="Sonarr", state="Seeding", days=13.9),
        "c": tor(label="sonarr-upgrade", state="Paused", days=14),
        "d": tor(label="sonarr", state="Seeding", days=20.9),
        "e": tor(label="SONARR-priority", state="Error", days=21),
        "f": tor(label="radarr", days=99),
        "g": tor(label="", days=99),
    })
    s = r["sonarr"]
    assert r["total"] == 7 and s["count"] == 5, r
    assert s["by_label"] == {"sonarr": 3, "sonarr-upgrade": 1, "sonarr-priority": 1}, s
    assert s["by_state"] == {"Seeding": 3, "Paused": 1, "Error": 1}, s
    assert s["by_age"] == {"<14d": 2, "14-21d": 2, "21d+": 1}, s


def test_sonarr_group_episode_vs_season_pack():
    r = T.breakdown({
        "a": tor(name="Show.S01E05.1080p.WEB", label="sonarr"),
        "b": tor(name="Show.S01.1080p.WEB.COMPLETE", label="sonarr"),
        "c": tor(name="Other.s02e10.720p", label="sonarr-upgrade"),
        "d": tor(name="Movie.S01E01.fake", label="radarr"),
    })
    assert r["sonarr"]["by_kind"] == {"episode": 2, "season-pack": 1}, r["sonarr"]


def test_library_seed_group_by_label_and_root():
    r = T.breakdown({
        "a": tor(label="", path=MOV + "/Film (2020)"),
        "b": tor(label="Radarr", path=MOV),
        "c": tor(label="sonarr", path=TV + "/Show/Season 1"),
        "d": tor(label="", path=ASIAN + "/Drama"),
        "e": tor(label="", path=TV.lower() + "/other"),
        "f": tor(label="", path="/data/Media/Movies2/x"),
        "g": tor(label="", path="/data/Downloads/Complete"),
        "h": {"name": "h", "state": "Seeding"},
    })
    ls = r["library_seed"]
    assert ls["count"] == 5, ls
    assert ls["by_label"] == {"(none)": 3, "radarr": 1, "sonarr": 1}, ls
    assert ls["by_root"] == {MOV: 2, TV: 2, ASIAN: 1}, ls


def test_unscoped_counts_everything_else():
    r = T.breakdown({
        "a": tor(label="sonarr"),
        "b": tor(label="", path=MOV + "/x"),
        "c": tor(label="radarr", path="/data/Downloads/Complete"),
        "d": tor(label="", path="/data/Downloads/Complete"),
        "e": {"name": "e"},
    })
    assert r["unscoped"] == 3 and r["total"] == 5, r
    assert "other" not in r and "sample_names" not in str(r), r


def test_empty_input_and_missing_fields():
    e = T.breakdown({})
    assert e["total"] == 0 and e["unscoped"] == 0, e
    assert e["sonarr"]["count"] == 0 and e["sonarr"]["by_label"] == {} and e["sonarr"]["by_state"] == {}, e
    assert e["sonarr"]["by_age"] == {"<14d": 0, "14-21d": 0, "21d+": 0}, e
    assert e["sonarr"]["by_kind"] == {"episode": 0, "season-pack": 0}, e
    assert e["library_seed"] == {"count": 0, "by_label": {}, "by_root": {}}, e
    r = T.breakdown({"a": {"label": "sonarr", "seeding_time": None, "state": None, "name": None}})
    assert r["sonarr"]["by_state"] == {"(unknown)": 1} and r["sonarr"]["by_age"]["<14d"] == 1, r
