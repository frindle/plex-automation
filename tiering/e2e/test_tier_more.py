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


def test_head_holds_twenty_minutes_of_play(stack):
    """The head holds at least 20 minutes of play, by Plex's play time, else the one the
    arr measured from the file: a 45-minute file keeps 20/45 of itself, a 50-minute one
    Plex has no time for keeps 20/50. Under 30 minutes (a half-hour show), or with no
    play time from either, a file stays whole."""
    mid = stack.add("mid", MOVIES[1], size=90 * S.MIB, minutes=45)
    short = stack.add("short", MOVIES[2], size=60 * S.MIB, minutes=25, arr_minutes=60)
    unknown = stack.add("unknown", MOVIES[3], size=60 * S.MIB, minutes=None)
    by_arr = stack.add("by_arr", MOVIES[0], size=100 * S.MIB, minutes=None, arr_minutes=50)
    stack.start()
    stack.wait_state([mid, by_arr], "offloaded", timeout=120)
    for f, of in ((mid, 45), (by_arr, 50)):
        want = -(-f["size"] * 20 // of)
        assert want <= stack.row(f)["head_len"] < want + S.CHUNK, (f["key"], want)
    S.wait_until(lambda: "demotes skipped by the head rule" in stack.log(), 60,
                 what="the head rule to skip the short and the unknown file")
    log = stack.log()
    assert "under 30 minutes of play 1" in log and "no play time from Plex or the arr" in log
    assert stack.state_of(short) == "whole" and stack.state_of(unknown) == "whole"
    assert not os.path.islink(short["disk"]) and not os.path.islink(unknown["disk"])
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


# ------------------------------------------------------------------ the twin

def twin_offloaded(stack, **over):
    """One file with a twin, offloaded. Returns the file, its row, and the local path of
    the copy the row reads first."""
    f = stack.add("m", MOVIES[0], twin=True)
    stack.start(**over)
    stack.wait_state(f, "offloaded", timeout=120)
    row = stack.row(f)
    assert row["twin_remote_path"] and row["twin_nzb_path"], "qualify kept no twin"
    assert row["twin_remote_path"] != row["remote_path"]
    assert row["twin_nzb_path"] != row["nzb_path"]
    return f, row, row["remote_path"].lstrip("/")


def test_the_upload_is_gone_and_the_twin_serves_the_player(stack):
    """The first copy is gone (every read a 404). The player still gets every byte, and
    the file comes back whole from the twin, inside the retry time of one copy."""
    f, row, first = twin_offloaded(stack, TIERING_PROMOTE_SWITCH_S=1)
    os.rename(os.path.join(stack.remote, first), os.path.join(stack.remote, first) + ".gone")
    assert stack.player_read(f, timeout=60) == f["sha256"]
    stack.wait_state(f, "whole", timeout=60)
    assert not os.path.islink(f["disk"])
    stack.assert_intact()
    assert stack.leftovers() == []


def test_bad_bytes_on_the_upload_come_from_the_twin(stack):
    """Scenario 4 with a twin: one chunk of the first copy is zeros. That chunk is read
    from the twin, and the file ends whole instead of failed."""
    f, row, first = twin_offloaded(stack)
    bad = row["head_len"] + S.CHUNK
    stack.zero(f, bad, bad + S.CHUNK, rel=first)
    assert stack.player_read(f, timeout=60) == f["sha256"]
    stack.wait_state(f, "whole", timeout=60)
    stack.assert_intact()
    assert not any(a.get("kind") in ("promote_failed", "bad_chunk") for a in stack.alerts())
