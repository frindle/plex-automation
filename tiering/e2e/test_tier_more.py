"""Scenarios 3 and 10 of SCENARIOS.md, and the size of the part that stays local."""
import os
import threading

import stack as S

MOVIES = [f"Movies/Film {c} (200{i})/Film.{c}.200{i}.1080p.WEB-GRP.mkv"
          for i, c in enumerate("ABCD", 1)]


def test_a_fifth_of_the_file_stays_local_at_the_front(stack):
    """The head that stays on disk is 20% of the file, rounded up to a whole chunk."""
    f = stack.add("m", MOVIES[0], size=120 * S.MIB)
    stack.start()
    stack.wait_state(f, "offloaded", timeout=120)
    row = stack.row(f)
    fifth = f["size"] // 5
    assert fifth <= row["head_len"] < fifth + S.CHUNK, row["head_len"]
    kept = row["head_len"] + row["tail_len"]
    assert abs(os.lstat(row["stub_path"]).st_blocks * 512 - kept) <= 2 * S.MIB
    stack.assert_intact()


def test_sync_of_one_episode_brings_the_season_back(stack):
    """Scenario 3: a download of one offloaded episode has no Plex session. The
    reader gets the right bytes and the whole season comes back."""
    season = stack.add_season(1, n=4)
    stack.start()
    stack.wait_state(season, "offloaded", timeout=120)
    assert stack.player_read(season[1], timeout=30) == season[1]["sha256"]
    stack.wait_state(season, "whole", timeout=120)
    stack.assert_intact()
    assert stack.leftovers() == []


def test_four_files_read_at_once(stack):
    """Scenario 10: four offloaded files are opened by players at the same moment.
    Every reader gets its own file's bytes and every file ends whole."""
    fs = [stack.add(f"m{i}", rel) for i, rel in enumerate(MOVIES)]
    stack.start()
    stack.wait_state(fs, "offloaded", timeout=120)
    got = {}

    def read(f):
        try:
            got[f["key"]] = stack.player_read(f, timeout=60)
        except OSError as e:
            got[f["key"]] = f"error: {e}"

    ts = [threading.Thread(target=read, args=(f,)) for f in fs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert got == {f["key"]: f["sha256"] for f in fs}
    stack.wait_state(fs, "whole", timeout=60)
    assert stack.leftovers() == []
