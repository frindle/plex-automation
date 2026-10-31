"""Frozen tests: single-episode torrents labeled exactly 'library-seed' are retention candidates."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_retention as E  # noqa: E402

DAY = 86400
NAME = "Married.At.First.Sight.UK.S09E23.1080p.WEB"
SAVE = "/data/Media/TV Shows/Married At First Sight UK"


def tor(name=NAME, label="library-seed", days=351, progress=100.0, state="Seeding",
        save_path=SAVE, files=None):
    if files is None:
        files = [{"path": name + "/" + name + ".mkv"}, {"path": name + "/" + name + ".nfo"}]
    return {"name": name, "label": label, "state": state, "progress": progress,
            "seeding_time": days * DAY, "save_path": save_path, "files": files}


def run(torrents, **kw):
    calls = []
    r = E.episode_retention(torrents, lambda h, rd: calls.append((h, rd)), lambda t: True,
                            env={"EPISODE_RETENTION_APPLY": "1"}, **kw)
    return r, calls


def test_classify_library_seed_goes_through_episode_detection():
    assert E.classify(tor()) == "episode"
    assert E.classify(tor(label="Library-Seed")) == "episode"
    assert E.classify(tor(label="LIBRARY-SEED")) == "episode"
    movie = "Some.Movie.2020.1080p"
    assert E.classify(tor(name=movie)) == "season-pack"
    pack = "Show.S01.1080p"
    files = [{"path": "%s/Show.S01E%02d.mkv" % (pack, i)} for i in range(1, 4)]
    assert E.classify(tor(name=pack, files=files)) == "season-pack"
    nomarker_path = [{"path": NAME + "/movie.mkv"}]
    assert E.classify(tor(files=[{"path": "x/clip.mkv"}])) == "season-pack"
    assert E.classify(tor(files=nomarker_path)) == "season-pack"


def test_classify_light_library_seed():
    assert E.classify_light(tor()) == "episode"
    assert E.classify_light(tor(label="Library-Seed")) == "episode"
    assert E.classify_light(tor(name="Some.Movie.2020.1080p")) == "season-pack"
    # unchanged behaviour for every other label
    for label in ("library-seed2", "library-seeds", "my-library-seed", "library", "", "tv", None):
        assert E.classify(tor(label=label)) == "other", label
        assert E.classify_light(tor(label=label)) == "other", label
    assert E.classify(tor(label="radarr")) == "movie"
    assert E.classify_light(tor(label="radarr")) == "movie"
    assert E.classify(tor(label="sonarr")) == "episode"
    assert E.classify_light(tor(label="sonarr")) == "episode"


def test_library_seed_episode_removed_but_files_kept_under_media():
    assert E.remove_data_for(tor(), lambda t: True) is False
    r, calls = run({"a": tor(), "b": tor(label="library-seed", name="Other.Show.S01E01.720p"),
                    "c": tor(name="Some.Movie.2020.1080p"), "d": tor(progress=99.0),
                    "e": tor(days=20), "f": tor(label="other-label")})
    assert sorted(h for h, _ in calls) == ["a", "b"], r
    assert all(rd is False for _, rd in calls), calls
    assert r["kept"]["season-pack"] == 1 and r["kept"]["incomplete"] == 1
    assert r["kept"]["too-young"] == 1 and r["kept"]["other"] == 1


def test_library_seed_lazy_fetch_files_path():
    light = {k: v for k, v in tor().items() if k != "files"}
    movie = {k: v for k, v in tor(name="Some.Movie.2020.1080p").items() if k != "files"}
    movie_ep = dict(movie, name=NAME)
    files = {"a": tor()["files"], "m": [{"path": "Some.Movie/movie.mkv"}]}
    r, calls = run({"a": light, "m": dict(movie_ep), "z": movie},
                   fetch_files=lambda h: files.get(h, []))
    assert [h for h, _ in calls] == ["a"], r
    assert calls[0][1] is False
    assert r["kept"]["season-pack"] == 2, r
