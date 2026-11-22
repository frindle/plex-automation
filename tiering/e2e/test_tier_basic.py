"""Scenarios 1, 7 and 9 of SCENARIOS.md, plus the restore round trip, through the
real runner."""
import os
import time

import stack as S

MOVIE = "Movies/Alpha Movie (2001)/Alpha.Movie.2001.1080p.WEB-GRP.mkv"


def tree(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = os.path.join(dp, n)
            ls = os.lstat(p)
            out[p] = (ls.st_mode, ls.st_size, ls.st_mtime_ns, ls.st_ino, ls.st_nlink)
    return out


def test_offload_then_play_brings_it_back(stack):
    """Scenario 1, then the restore path: an idle file is offloaded, and a play
    brings every byte back from the remote."""
    f = stack.add("m", MOVIE)
    stack.start()
    stack.wait_state(f, "offloaded")
    row = stack.row(f)
    lib = f["disk"]
    assert os.path.islink(lib)
    assert os.readlink(lib) == os.path.join(stack.mount, row["id"] + ".mkv")
    st = os.stat(lib)
    assert (st.st_size, st.st_mtime_ns) == (f["size"], f["mtime_ns"])
    stub = os.lstat(row["stub_path"])
    assert stub.st_ino == f["ino"], "the stub is not the original inode"
    kept = row["head_len"] + row["tail_len"]
    assert kept < f["size"]
    assert abs(stub.st_blocks * 512 - kept) <= 2 * S.MIB, "the middle was not punched"
    assert row["nzb_path"] and os.path.exists(row["nzb_path"])
    assert row["nzb_path2"] and os.path.exists(row["nzb_path2"]), "no restore NZB archived"
    stack.assert_intact()

    stack.plex.play(f["rk"])
    stack.wait_state(f, "whole")
    assert not os.path.islink(lib)
    assert S.sha256_file(lib) == f["sha256"]
    assert os.lstat(lib).st_mtime_ns == f["mtime_ns"]
    assert os.lstat(lib).st_blocks * 512 >= f["size"]
    assert stack.leftovers() == []
    time.sleep(3)  # a played file is not offloaded again
    assert stack.state_of(f) == "whole"


def test_dry_run_changes_nothing(stack):
    """Scenario 9: without TIERING_APPLY the runner plans and touches nothing."""
    f = stack.add("m", MOVIE)
    stack.gate.down = True  # a first applying run only registers the file
    stack.start()
    S.wait_until(lambda: stack.gate.asked, 30, what="the first run to reach the gate")
    stack.stop()
    stack.gate.down = False
    before = tree(stack.disks[0])
    rows = stack.rows()
    stack.start(TIERING_APPLY="0")
    S.wait_until(lambda: "plan: demote" in stack.log() or stack.runner.poll() is not None,
                 30, what="a planned demote in the log")
    time.sleep(2)
    stack.stop()
    assert "plan: demote" in stack.log(), stack.log_tail()
    assert tree(stack.disks[0]) == before
    assert os.listdir(stack.manifests) == [] and os.listdir(stack.stub_roots[0]) == []
    assert stack.rows() == rows
    stack.assert_intact([f])


def test_upgrade_during_offload_keeps_the_new_file(stack):
    """Scenario 7: the arr replaces L after the stub was linked and before the swap.
    The new file must survive untouched."""
    f = stack.add("m", MOVIE)
    stack.start(kill_at="offload.manifest_written")
    stack.wait_killed()
    new = os.urandom(3 * S.MIB)
    tmp = f["disk"] + ".part"
    with open(tmp, "wb") as fh:
        fh.write(new)
    os.replace(tmp, f["disk"])
    new_sha = S.sha256_file(f["disk"])
    stack.start()
    S.wait_until(lambda: stack.state_of(f) in ("whole", "failed"), 60, what="the row to settle")
    time.sleep(2)
    assert not os.path.islink(f["disk"])
    assert S.sha256_file(f["disk"]) == new_sha, "the upgraded file was changed"
    assert not [p for p in stack.leftovers() if p.endswith(".tierfs-tmp")]
