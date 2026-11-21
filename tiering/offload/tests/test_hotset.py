import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from offload.hotset import Action, Cfg, DAY, desired_whole, plan_actions  # noqa: E402

NOW = 1_000_000_000.0


def ep(rk, show, season, index, added=0, section="TV", res="1080"):
    return {"ratingKey": str(rk), "type": "episode", "grandparentRatingKey": str(show),
            "parentIndex": season, "index": index, "addedAt": added,
            "section": section, "videoResolution": res}


def movie(rk, added=0, section="Movies", res="1080"):
    return {"ratingKey": str(rk), "type": "movie", "addedAt": added,
            "section": section, "videoResolution": res}


def season(show, n, count, first_rk, **kw):
    return [ep(first_rk + i, show, n, i + 1, **kw) for i in range(count)]


def play(rk, ts):
    return {"ratingKey": str(rk), "viewedAt": ts}


def want(items, plays=(), sessions=(), deck=(), synced=(), cfg=Cfg(), now=NOW):
    return desired_whole(items, list(plays), list(sessions), list(deck), synced, now, cfg)


def test_new_import_boundary():
    items = [movie(1, added=NOW - 30 * DAY + 1), movie(2, added=NOW - 30 * DAY)]
    d = want(items)
    assert d["1"][0] == "new" and d["1"][1] == NOW + 1
    assert "2" not in d


def test_play_expiry_boundary():
    items = [movie(1), movie(2)]
    d = want(items, plays=[play(1, NOW - 60 * DAY + 1), play(2, NOW - 60 * DAY)])
    assert d["1"][0] == "played"
    assert "2" not in d


def test_session_ondeck_synced():
    items = [movie(1), movie(2), movie(3)]
    d = want(items, sessions=[{"ratingKey": "1"}], deck=[{"ratingKey": 2}], synced={"3"})
    assert d["1"][0] == "session"
    assert d["2"][0] == "on-deck"
    assert d["3"][0] == "synced"
    assert d["1"][1] == NOW + 60 * DAY


def test_synced_with_timestamps():
    d = want([movie(1)], synced={"1": NOW - 61 * DAY})
    assert d == {}


def test_season_whole_on_any_episode():
    items = season(10, 1, 10, 100) + season(10, 2, 10, 200)
    d = want(items, plays=[play(103, NOW - DAY)])
    assert d["103"][0] == "played"
    assert all(str(100 + i) in d and d[str(100 + i)][1] == NOW - DAY + 60 * DAY
               for i in range(10))
    assert d["100"][0] == "season"
    assert not any(k.startswith("2") for k in d)  # ep 4 is not near the end


def test_last_three_of_ten_pull_next_season():
    items = season(10, 1, 10, 100) + season(10, 2, 10, 200) + season(10, 3, 10, 300)
    for index, pulls in [(7, False), (8, True), (9, True), (10, True)]:
        d = want(items, plays=[play(100 + index - 1, NOW - DAY)])
        assert ("205" in d) is pulls, index
        assert not any(k.startswith("3") for k in d)
        if pulls:
            assert d["205"][0] == "next-season"
            assert len([k for k in d if k.startswith("2")]) == 10


def test_missing_episode_index_uses_highest():
    # season has indexes 1..8 and 10 (9 missing): highest is 10, so 8 is near end.
    items = [ep(100 + i, 10, 1, i) for i in (1, 2, 3, 4, 5, 6, 7, 8, 10)] + season(10, 2, 3, 200)
    assert "200" in want(items, plays=[play(108, NOW)])
    assert "200" not in want(items, plays=[play(107, NOW)])
    assert "200" in want(items, plays=[play(110, NOW)])


def test_specials_never_pull_next_season():
    items = season(10, 0, 2, 50) + season(10, 1, 5, 100)
    d = want(items, plays=[play(51, NOW)])
    assert set(d) == {"50", "51"}


def test_no_next_season_in_library():
    items = season(10, 1, 3, 100)
    assert set(want(items, plays=[play(102, NOW)])) == {"100", "101", "102"}


def test_next_season_expires_with_last_play():
    items = season(10, 1, 4, 100) + season(10, 2, 4, 200)
    d = want(items, plays=[play(103, NOW - 59 * DAY)])
    assert "200" in d
    d = want(items, plays=[play(103, NOW - 61 * DAY)])
    assert d == {}


def test_season_stays_until_last_play_in_it():
    items = season(10, 1, 10, 100)
    d = want(items, plays=[play(100, NOW - 70 * DAY), play(102, NOW - 10 * DAY)])
    assert d["100"] == ("season", NOW + 50 * DAY)


def test_keep_4k_on_and_off():
    items = [movie(1, res="4k"), movie(2, res="1080")]
    assert want(items) == {}
    d = want(items, cfg=Cfg(keep_4k=True))
    assert d["1"] == ("4k-line", float("inf"))
    assert "2" not in d


def test_bitrate_limit():
    def br(rk, kbps):
        m = movie(rk)
        if kbps is not None:
            m["bitrate"] = kbps
        return m
    items = [br(1, 20001), br(2, 20000), br(3, None), br(4, 19999)]
    d = want(items, cfg=Cfg(max_stream_kbps=20000))
    assert d == {"1": ("bitrate", float("inf"))}
    assert want(items, cfg=Cfg(max_stream_kbps=0)) == {}
    assert want(items) == {}


def test_never_offloaded_sections_case_insensitive():
    items = [movie(1, section="adult"), movie(2, section="Asian TV"), movie(3)]
    d = want(items)
    assert d["1"][0] == "section"
    assert "2" not in d and "3" not in d  # Asian TV can be offloaded (Penn, 2026-11-21)
    d = want(items, cfg=Cfg(never_sections=("adult", "ASIAN TV")))
    assert d["1"][0] == "section" and d["2"][0] == "section"
    d = want(items, cfg=Cfg(never_sections=("Movies",)))
    assert set(d) == {"3"}


def test_unknown_ratingkey_activity_ignored():
    assert want([movie(1)], plays=[play(99, NOW)], sessions=[{"ratingKey": "98"}]) == {}


def test_plan_actions_promote_demote_order():
    desired = {"5": ("played", NOW + 1), "3": ("session", NOW + 1), "9": ("new", NOW + 1),
               "7": ("season", NOW + 1)}
    acts = plan_actions(desired, offloaded_rks=["5", "3", "4", "7"], whole_rks=["9", "1", "2"], now=NOW)
    assert acts == [
        Action("promote", "3", "session", 0),
        Action("promote", "5", "played", 1),
        Action("promote", "7", "season", 1),
        Action("demote", "1", "idle", 5),
        Action("demote", "2", "idle", 5),
    ]


def test_never_offloaded_not_demoted():
    items = [movie(1, section="Adult"), movie(2)]
    d = want(items)
    # Caller passes only offload-eligible whole items; item 1 is excluded there,
    # and it is also in desired, so it would not be demoted either way.
    acts = plan_actions(d, [], ["2"], NOW)
    assert [a.rating_key for a in acts] == ["2"]
    assert plan_actions(d, [], ["1", "2"], NOW) == [Action("demote", "2", "idle", 5)]


def test_backtest_synthetic_history():
    day = DAY
    now = 200 * day
    items = (season("A", 1, 10, 100) + season("A", 2, 10, 200)
             + season("B", 1, 8, 300)
             + season("C", 1, 6, 400) + season("C", 2, 6, 500) + season("C", 3, 6, 600)
             + [movie(1), movie(2, added=190 * day)])
    plays = []
    plays += [play(100 + i, (10 + i) * day) for i in range(10)]        # A S1 binge
    plays += [play(200 + i, (150 + i) * day) for i in range(4)]        # A S2 start
    plays += [play(100, d * day) for d in range(30, 100, 5)]           # A S1E1 replays
    plays += [play(300 + i, (100 + i) * day) for i in range(8)]        # B S1
    plays += [play(400 + i, (120 + i) * day) for i in range(6)]        # C S1
    plays += [play(500 + i, (140 + i) * day) for i in range(6)]        # C S2
    plays += [play(600, 190 * day)]                                    # C S3E1
    plays += [play(1, d * day) for d in (30, 60, 90, 120, 125, 135, 145, 150)]
    plays += [play(2, 195 * day), play(2, 196 * day)]
    assert 55 <= len(plays) <= 65

    d = desired_whole(items, plays, [], [], set(), now, Cfg())

    # Independent computation: per (show, season) / movie last play, plus the
    # explicit near-end carry-over, by plain loops over the play list.
    last = {}
    for p in plays:
        rk = int(p["ratingKey"])
        last[rk] = max(last.get(rk, 0), p["viewedAt"])
    seasons = {"A1": (100, 10), "A2": (200, 10), "B1": (300, 8),
               "C1": (400, 6), "C2": (500, 6), "C3": (600, 6)}
    nxt = {"A1": "A2", "C1": "C2", "C2": "C3"}
    hot_until = {k: 0.0 for k in seasons}
    for k, (first, n) in seasons.items():
        for i in range(n):
            t = last.get(first + i)
            if t is None:
                continue
            hot_until[k] = max(hot_until[k], t + 60 * day)
            if i >= n - 3 and k in nxt:
                hot_until[nxt[k]] = max(hot_until[nxt[k]], t + 60 * day)
    expected = set()
    for k, (first, n) in seasons.items():
        if hot_until[k] > now:
            expected |= {str(first + i) for i in range(n)}
    if last[1] + 60 * day > now:
        expected.add("1")
    expected.add("2")  # new import and recent play

    assert set(d) == expected
    assert len(items) == 48
    assert len(d) == 24            # hot-set size
    assert len(items) - len(d) == 24  # cold-start count: files offloadable on day one
    assert {k for k in d if k.startswith("5")} == {str(500 + i) for i in range(6)}
    assert not any(k.startswith(("1", "3", "4")) and len(k) == 3 for k in d)
