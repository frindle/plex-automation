"""Scenario 8: kill -9 the runner at every step of an offload and of a promote.

After each kill the file must still read back whole through its path. After a
restart the work must finish, the bytes must match the original, and nothing
half-done may be left behind.
"""
import os

import pytest

import stack as S

MOVIE = "Movies/Alpha Movie (2001)/Alpha.Movie.2001.1080p.WEB-GRP.mkv"

OFFLOAD_POINTS = [
    "offload.linked", "offload.step2", "offload.chunks_written", "offload.manifest_written",
    "offload.step3", "offload.tmp_symlink", "offload.exchanged", "offload.tmp_unlinked",
    "offload.step4", "offload.step5", "offload.punched", "offload.step6",
]
PROMOTE_POINTS = [
    "promote.fill1", "promote.fill2", "promote.step1", "promote.step2", "promote.tmp_linked",
    "promote.exchanged", "promote.tmp_unlinked", "promote.step3", "promote.step4",
    "promote.stub_unlinked", "promote.manifest_removed",
]


def finish_whole(stack, f):
    """Play the file and check the full round trip back to a plain local file."""
    stack.plex.play(f["rk"])
    stack.wait_state(f, "whole")
    lib = f["disk"]
    assert not os.path.islink(lib)
    assert S.sha256_file(lib) == f["sha256"]
    assert os.lstat(lib).st_mtime_ns == f["mtime_ns"]
    assert os.lstat(lib).st_nlink == 1
    assert stack.leftovers() == []


@pytest.mark.parametrize("point", OFFLOAD_POINTS)
def test_kill_during_offload(stack, point):
    f = stack.add("m", MOVIE)
    stack.start(kill_at=point)
    stack.wait_killed()
    stack.assert_intact([f])  # readable right after the crash, before any restart
    stack.start()
    S.wait_until(lambda: stack.state_of(f) in ("offloaded", "whole")
                 and not [p for p in stack.leftovers() if p.endswith(".tierfs-tmp")],
                 90, what=f"the offload to settle after a kill at {point}\n" + stack.log_tail())
    stack.assert_intact([f])
    finish_whole(stack, f)


@pytest.mark.parametrize("point", PROMOTE_POINTS)
def test_kill_during_promote(stack, point):
    f = stack.add("m", MOVIE)
    stack.start()
    stack.wait_state(f, "offloaded")
    stack.stop()
    stack.plex.play(f["rk"])
    stack.start(kill_at=point)
    stack.wait_killed()
    stack.assert_intact([f])
    stack.start()
    stack.wait_state(f, "whole")
    finish_whole(stack, f)


def test_kill_twice_in_one_offload(stack):
    """Two crashes in a row, one on each side of the swap."""
    f = stack.add("m", MOVIE)
    for point in ("offload.tmp_symlink", "offload.punched"):
        stack.start(kill_at=point)
        stack.wait_killed()
        stack.assert_intact([f])
    stack.start()
    stack.wait_state(f, "offloaded")
    stack.assert_intact([f])
    finish_whole(stack, f)


def test_player_read_after_crashed_offload_gets_the_file(stack):
    """A crash right after the punch leaves the row `offloading` with L a symlink and
    a hole in the stub. A player that opens the file then must still get it."""
    f = stack.add("m", MOVIE)
    stack.start(kill_at="offload.punched")
    stack.wait_killed()
    stack.start()
    S.wait_until(lambda: stack.state_of(f) == "offloaded", 60, what="the resumed offload")
    assert stack.player_read(f) == f["sha256"]
