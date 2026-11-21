"""Where the pieces meet: tierfs `want` events and the runner, reads during an
offload, a dead remote, the path maps, the hot-set rules, the restore path, the
weekly check and the torrent gate.

A test marked `defect` proves a known bug: it fails today and is expected to.
When the bug is fixed the mark must come off (strict xfail).
"""
import os
import time

import pytest

import stack as S
from tiering.offload import state as st

MOVIE = "Movies/Alpha Movie (2001)/Alpha.Movie.2001.1080p.WEB-GRP.mkv"
MOVIE2 = "Movies/Beta Movie (2002)/Beta.Movie.2002.1080p.WEB-GRP.mkv"
MOVIE3 = "Movies/Gamma Movie (2003)/Gamma.Movie.2003.1080p.WEB-GRP.mkv"


def defect(reason):
    return pytest.mark.xfail(strict=True, reason=reason)


def offloaded(stack, *fs):
    stack.start()
    stack.wait_state(list(fs), "offloaded", timeout=120)


def blocks(path):
    return os.lstat(path).st_blocks * 512


def kinds(stack, f=None):
    return [e["kind"] for e in stack.db_events(f)]


# ------------------------------------------------------------------ want

def test_player_read_promotes_at_once_without_plex(stack):
    """Scenario 3 (one file): a download or sync is a read with no Plex session. The
    `want` it raises must bring the whole file back, and the file must then stay."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    t0 = time.monotonic()
    assert stack.player_read(f, timeout=30) == f["sha256"]
    stack.wait_state(f, "whole", timeout=30)
    assert time.monotonic() - t0 < 30
    assert stack.leftovers() == []
    time.sleep(4)  # the read counts as activity: no demote right after
    assert stack.state_of(f) == "whole"


def test_background_read_does_not_promote(stack):
    """A library scan reads through the remote and must not start a promote."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    assert stack.read_through(f) == f["sha256"]
    time.sleep(4)
    assert stack.state_of(f) == "offloaded"
    assert stack.tierfs_events("want") == []


def test_want_outside_the_demote_window_still_promotes(stack):
    """Promotes are never tied to the night window."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    stack.stop()
    stack.start(TIERING_DEMOTE_WINDOW="03:00-03:01" if time.localtime().tm_hour != 3
                else "12:00-12:01")
    assert stack.player_read(f, timeout=30) == f["sha256"]


def test_player_read_of_a_half_offloaded_file(stack):
    """A crash after the punch leaves the row `offloading`. Outside the window nothing
    resumes it, and a player that opens it must still get the file."""
    f = stack.add("m", MOVIE)
    stack.start(kill_at="offload.punched")
    stack.wait_killed()
    stack.start(TIERING_DEMOTE_WINDOW="03:00-03:01" if time.localtime().tm_hour != 3
                else "12:00-12:01")
    time.sleep(3)
    assert stack.state_of(f) == "offloading"
    assert stack.player_read(f, timeout=30) == f["sha256"]


# ------------------------------------------------------------------ dead remote

def test_zeroed_chunk_is_never_written_into_the_file(stack):
    """Scenario 4: the remote answers one chunk with zeros. The promote must stop, the
    player must get an error, and L must stay the symlink with the stub untouched."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    row = stack.row(f)
    bad = row["head_len"] + S.CHUNK
    stack.zero(f, bad, bad + S.CHUNK)
    stub_before = (os.lstat(row["stub_path"]).st_ino, row["head_len"], row["tail_len"])
    with pytest.raises(OSError):
        stack.player_read(f, timeout=40)
    S.wait_until(lambda: stack.state_of(f) == "failed", 30, what="state failed")
    assert os.path.islink(f["disk"]), "L was replaced although the remote copy is bad"
    assert os.lstat(row["stub_path"]).st_ino == stub_before[0]
    got, failed = stack.filled(row["id"])
    assert failed and bad not in got
    assert any(a.get("kind") in ("promote_failed", "bad_chunk") for a in stack.alerts()), \
        stack.alerts()
    # head and tail are still served, and the good chunks still check out
    with open(row["stub_path"], "rb") as s, open(os.path.join(stack.remote, f["remote"]),
                                                 "rb") as r:
        assert s.read(S.MIB) == r.read(S.MIB)


@defect("#83: a remote that answers with errors (not bad data) never writes `failed` to "
        "the fill file, so the player hangs for the whole TIERFS_WAIT_S before its error")
def test_remote_gone_gives_the_player_an_error_quickly(stack):
    """The remote file is gone (every read is a 404). The player must be told within
    the retry time, not left hanging for the full wait."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    os.rename(os.path.join(stack.remote, f["remote"]),
              os.path.join(stack.remote, f["remote"]) + ".gone")
    t0 = time.monotonic()
    with pytest.raises(OSError, match="fill failed"):
        stack.player_read(f, timeout=25)  # TIERING_PROMOTE_RETRY_S is 3 here
    assert time.monotonic() - t0 < 25
    assert os.path.islink(f["disk"])


def test_remote_back_after_an_outage_promotes_again(stack):
    """An outage must not strand the file: once the remote answers again a later read
    brings the file back."""
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    path = os.path.join(stack.remote, f["remote"])
    os.rename(path, path + ".gone")
    with pytest.raises(OSError):
        stack.player_read(f, timeout=8)
    os.rename(path + ".gone", path)
    stack.stop()
    stack.start()  # a restart clears the 60 s spacing between wanted tries
    assert stack.player_read(f, timeout=40) == f["sha256"]


# ------------------------------------------------------------------ reads during an offload

def test_open_reader_holds_the_punch_back(stack):
    """A player has the file open while it is offloaded: no hole may be punched under
    it, and it reads every original byte."""
    f = stack.add("m", MOVIE)
    fd = os.open(f["disk"], os.O_RDONLY)
    try:
        stack.start()
        S.wait_until(lambda: os.path.islink(f["disk"]), 60, what="the swap")
        time.sleep(4)
        assert stack.state_of(f) == "offloading"
        assert blocks(stack.stub_of(f)) >= f["size"], "punched while a reader had it open"
        import hashlib
        h = hashlib.sha256()
        while True:
            b = os.read(fd, S.MIB)
            if not b:
                break
            h.update(b)
        assert h.hexdigest() == f["sha256"]
        stack.assert_intact([f])
    finally:
        os.close(fd)
    S.wait_until(lambda: stack.state_of(f) in ("offloaded", "whole"), 400, poll=1,
                 what="the offload to end after the reader left\n" + stack.log_tail())
    stack.assert_intact([f])


def test_play_that_starts_during_an_offload_wins(stack):
    """The file is being offloaded when someone starts to play it. It must end whole."""
    f = stack.add("m", MOVIE)
    stack.start(kill_at="offload.step4")
    stack.wait_killed()
    stack.plex.sessions.add(f["rk"])
    stack.start()
    stack.wait_state(f, "whole", timeout=60)
    assert S.sha256_file(f["disk"]) == f["sha256"]
    assert stack.leftovers() == []


# ------------------------------------------------------------------ path maps

def test_two_disks_use_their_own_stub_root_and_gate_path(make_stack):
    stack = make_stack(disks=2)
    if os.stat(stack.disks[0]).st_dev == os.stat(stack.disks[1]).st_dev:
        pytest.skip("needs two filesystems (btrfs subvolumes or root for tmpfs)")
    a = stack.add("a", MOVIE, disk=0)
    b = stack.add("b", MOVIE2, disk=1)
    offloaded(stack, a, b)
    for f, n in ((a, 0), (b, 1)):
        row = stack.row(f)
        assert row["lib_path"] == f["share"], "the DB must keep the share path"
        assert os.path.dirname(row["stub_path"]) == stack.stub_roots[n]
        assert f"/data/disk{n + 1}/Media/{f['rel']}" in stack.gate.asked
    assert not [p for p in stack.gate.asked if not p.startswith("/data/disk")]
    stack.assert_intact()
    stack.plex.play(b["rk"])
    stack.wait_state(b, "whole")
    assert S.sha256_file(b["disk"]) == b["sha256"]


def test_same_path_on_two_disks_is_left_alone(make_stack):
    """A share path that exists on two disks cannot be told apart: neither copy is
    touched (here it is half of the listed files, so the register also stops the night)."""
    stack = make_stack(disks=2)
    if os.stat(stack.disks[0]).st_dev == os.stat(stack.disks[1]).st_dev:
        pytest.skip("needs two filesystems")
    a = stack.add("a", MOVIE, disk=0)
    dup = os.path.join(stack.disks[1], "Media", a["rel"])
    os.makedirs(os.path.dirname(dup))
    with open(dup, "wb") as fh:
        fh.write(os.urandom(S.MIB))
    stack.add("b", MOVIE2, disk=1)
    stack.start()
    S.wait_until(lambda: "register" in stack.log(), 30, what="the register to run")
    time.sleep(5)
    assert "ambiguous" in stack.log()
    assert not os.path.islink(a["disk"]) and not os.path.islink(dup)
    assert stack.state_of(a) in (None, "whole")
    assert os.path.getsize(dup) == S.MIB and S.sha256_file(a["disk"]) == a["sha256"]


def test_arr_that_reports_a_disk_path_is_stored_as_the_share_path(stack):
    """The DB only ever holds share paths: a disk path from an arr is mapped back, and
    one that no map entry covers is skipped."""
    a = stack.add("a", MOVIE)
    b = stack.add("b", MOVIE2)
    c = stack.add("c", MOVIE3)
    stack.radarr.movies[0]["movieFile"]["path"] = a["disk"]
    stack.radarr.movies[2]["movieFile"]["path"] = "/mnt/elsewhere/" + c["rel"]
    offloaded(stack, a, b)
    time.sleep(2)
    assert not os.path.islink(c["disk"]) and stack.state_of(c) is None
    assert all(r["lib_path"].startswith(S.SHARE + "/") for r in stack.rows())
    assert stack.row(a)["lib_path"] == a["share"]
    stack.assert_intact()


@defect("#82/#83: an arr rename of an offloaded file strands it: the old row is "
        "l_missing and cannot be promoted, register makes a second row for the symlink")
def test_arr_rename_of_an_offloaded_file_can_still_be_promoted(stack):
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    stack.stop()
    new_rel = "Movies/Alpha Movie (2001)/Alpha.Movie.2001.1080p.WEB-GRP.renamed.mkv"
    new_disk = os.path.join(stack.disks[0], "Media", new_rel)
    os.rename(f["disk"], new_disk)  # the arr renames the symlink it sees
    stack.radarr.movies[0]["movieFile"]["path"] = f"{S.ARR}/{new_rel}"
    f["item"]["files"] = [f"{S.SHARE}/{new_rel}"]
    g = dict(f, disk=new_disk, share=f"{S.SHARE}/{new_rel}")
    stack.start()
    time.sleep(3)
    assert os.path.exists(new_disk), "the renamed path no longer resolves"
    stack.plex.play(f["rk"])
    S.wait_until(lambda: not os.path.islink(new_disk), 30, what="the renamed file to be whole")
    assert S.sha256_file(new_disk) == g["sha256"]


# ------------------------------------------------------------------ rules

def test_season_rules(stack):
    """Scenario 5. Starting an episode keeps its whole season local; one of the last
    three also brings the next season back."""
    s1 = stack.add_season(1, n=5)
    s2 = stack.add_season(2, n=5)
    offloaded(stack, *(s1 + s2))
    stack.plex.play(s1[1]["rk"])  # S01E02 of 5: not one of the last three
    stack.wait_state(s1, "whole", timeout=120)
    time.sleep(4)
    assert {stack.state_of(f) for f in s2} == {"offloaded"}
    stack.plex.play(s1[2]["rk"])  # S01E03: one of the last three
    stack.wait_state(s2, "whole", timeout=120)
    stack.assert_intact()
    assert stack.leftovers() == []


def busy_history(stack, stop):
    """Other plays keep arriving, so the history never shrinks by a fifth at once."""
    import threading

    def run():
        while not stop.wait(0.5):
            stack.plex.play("999999")
    threading.Thread(target=run, daemon=True).start()


def test_each_watch_restarts_the_idle_clock(stack):
    """Scenario 6 with a 20 s idle time."""
    import threading
    f = stack.add("m", MOVIE)
    idle = 20.0
    offloaded(stack, f)
    stack.stop()
    stop = threading.Event()
    busy_history(stack, stop)
    stack.start(TIERING_IDLE_DAYS=f"{idle / 86400:.8f}")
    t1 = time.time()
    stack.plex.play(f["rk"], at=t1)
    stack.wait_state(f, "whole", timeout=30)
    time.sleep(max(0, t1 + 12 - time.time()))
    t2 = time.time()
    stack.plex.play(f["rk"], at=t2)
    time.sleep(max(0, t1 + idle + 4 - time.time()))  # past the first deadline
    assert stack.state_of(f) == "whole", "the second watch did not restart the clock"
    stack.wait_state(f, "offloaded", timeout=idle + 40)
    assert time.time() >= t2 + idle - 1
    stop.set()
    stack.assert_intact([f])


@defect("#83: the history check compares counts inside a sliding idle window. With "
        "fewer than five plays in the window, one play ageing out reads as 'history "
        "wiped' and blocks every demote until the kv row is deleted by hand")
def test_a_play_ageing_out_is_not_a_wiped_history(stack):
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    stack.stop()
    stack.start(TIERING_IDLE_DAYS=f"{10 / 86400:.8f}")
    stack.plex.play(f["rk"])
    stack.wait_state(f, "whole", timeout=30)
    time.sleep(14)
    assert "history_shrunk" not in kinds(stack) and not [
        a for a in stack.alerts() if a.get("kind") == "history_shrunk"], stack.log_tail(5)
    stack.wait_state(f, "offloaded", timeout=30)


def test_new_adult_4k_and_asian_tv(stack):
    """New imports stay local 30 days; Adult never offloads; 4K and Asian TV do."""
    now = time.time()
    new = stack.add("new", MOVIE, added=now - 10 * 86400)
    old = stack.add("old", MOVIE2, added=now - 40 * 86400)
    adult = stack.add("adult", "Adult/Delta Movie (2004)/Delta.Movie.2004.1080p.WEB-GRP.mkv",
                      section="Adult")
    uhd = stack.add("uhd", "4K/Gamma Movie (2003)/Gamma.Movie.2003.2160p.WEB-GRP.mkv",
                    section="4K Movies", resolution="4k")
    asian = stack.add_season(1, n=1, section="Asian TV")[0]
    stack.start(TIERING_NEW_DAYS="")
    stack.wait_state([old, uhd, asian], "offloaded", timeout=120)
    time.sleep(4)
    for f in (new, adult):
        assert stack.state_of(f) == "whole" and not os.path.islink(f["disk"]), f["key"]
        assert os.lstat(f["disk"]).st_ino == f["ino"]
        assert "offload_start" not in kinds(stack, f)
    stack.assert_intact()


@defect("#83: a download or sync of a file that is already local leaves no record, so "
        "'60 days after the last play or sync' is not kept for synced files")
def test_sync_of_a_local_file_restarts_the_idle_clock(stack):
    f = stack.add("m", MOVIE)
    stack.gate.down = True  # keep it whole while it is 'synced'
    stack.start(TIERING_IDLE_DAYS="30")
    S.wait_until(lambda: stack.gate.asked, 30, what="register and a first demote try")
    stack.stop()
    assert S.sha256_file(f["disk"]) == f["sha256"]  # the sync: a full read of a local file
    stack.gate.down = False
    db = st.open_db(stack.db_path)
    st.kv_set(db, "night", None)  # forget tonight's tries
    db.close()
    stack.start(TIERING_IDLE_DAYS="30")
    time.sleep(8)
    assert stack.state_of(f) == "whole", "offloaded minutes after it was synced"


# ------------------------------------------------------------------ restore path

def test_files_without_a_proven_restore_are_never_offloaded(stack):
    ok = stack.add("ok", MOVIE)
    no_alt = stack.add("no_alt", MOVIE2, alt=False)
    dead_alt = stack.add("dead_alt", MOVIE3)
    stack.nntp2.ids -= set(dead_alt["alt_ids"])
    wrong = stack.add("wrong", "Movies/Delta Movie (2004)/Delta.Movie.2004.1080p.WEB-GRP.mkv")
    with open(os.path.join(stack.remote, wrong["remote"]), "r+b") as fh:
        fh.seek(20 * S.MIB)
        fh.write(b"\0" * 4096)  # the remote copy differs by one block
    missing = stack.add("missing", "Movies/Echo Movie (2005)/Echo.Movie.2005.1080p.WEB-GRP.mkv")
    stack.nntp1.ids.discard(missing["ids"][3])
    offloaded(stack, ok)
    S.wait_until(lambda: kinds(stack).count("qualify_failed") >= 4, 60,
                 what="four refusals\n" + stack.log_tail())
    for f in (no_alt, dead_alt, wrong, missing):
        assert stack.state_of(f) == "whole" and os.lstat(f["disk"]).st_ino == f["ino"], f["key"]
        assert blocks(f["disk"]) >= f["size"]
    log = stack.log()
    for reason in ("no_restore_path", "hash_mismatch", "missing_segments_primary"):
        assert reason in log, reason


def test_indexer_down_means_no_offload(stack):
    f = stack.add("m", MOVIE)
    stack.indexer.fail = True
    stack.start()
    S.wait_until(lambda: "not tonight" in stack.log() or "qualify failed" in stack.log(), 40,
                 what=lambda: "the demote to be refused\n" + stack.log_tail(12))
    time.sleep(2)
    assert stack.state_of(f) == "whole" and not os.path.islink(f["disk"])
    assert blocks(f["disk"]) >= f["size"]


def _age_weekly(stack, days=8):
    db = st.open_db(stack.db_path)
    st.kv_set(db, "weekly_last_ts", time.time() - days * 86400)
    db.close()


def test_weekly_check_brings_back_a_file_whose_articles_vanish(stack):
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    stack.stop()
    stack.nntp1.ids.discard(f["ids"][2])
    _age_weekly(stack)
    stack.start()
    stack.wait_state(f, "whole", timeout=60)
    assert "weekly_flag" in kinds(stack, f)
    assert S.sha256_file(f["disk"]) == f["sha256"]
    time.sleep(4)
    assert stack.state_of(f) == "whole", "offloaded again although its articles are gone"


def test_weekly_check_alerts_and_promotes_when_the_restore_nzb_dies(stack):
    f = stack.add("m", MOVIE)
    offloaded(stack, f)
    stack.stop()
    for n in (stack.nntp1, stack.nntp2):
        n.ids -= set(f["alt_ids"])
    _age_weekly(stack)
    stack.start()
    stack.wait_state(f, "whole", timeout=60)
    ks = kinds(stack, f)
    assert "restore_nzb_lost" in ks and "alert" in ks, ks
    assert S.sha256_file(f["disk"]) == f["sha256"]


def test_weekly_check_is_due_before_any_demote(stack):
    """With the weekly check overdue and the providers unreachable, nothing is offloaded."""
    a = stack.add("a", MOVIE)
    offloaded(stack, a)
    stack.stop()
    b = stack.add("b", MOVIE2)
    _age_weekly(stack)
    stack.nntp1.greeting_code = "502"
    stack.nntp2.greeting_code = "502"
    stack.start()
    time.sleep(8)
    assert stack.state_of(b) in (None, "whole") and not os.path.islink(b["disk"])
    assert stack.state_of(a) == "offloaded", "an unreachable provider must not flag a file"


# ------------------------------------------------------------------ torrents

def test_seeding_and_hardlinked_files_are_never_offloaded(stack):
    seeding = stack.add("seeding", MOVIE)
    linked = stack.add("linked", MOVIE2)
    free = stack.add("free", MOVIE3)
    os.makedirs(os.path.join(stack.disks[0], "torrents"))
    os.link(linked["disk"], os.path.join(stack.disks[0], "torrents", "linked.mkv"))
    stack.gate.torrents = {"a" * 40: {
        "save_path": "/data/disk1/Media/Movies", "name": "Alpha Movie (2001)",
        "progress": 100, "state": "Seeding"}}
    offloaded(stack, free)
    time.sleep(3)
    for f in (seeding, linked):
        assert stack.state_of(f) == "whole" and os.lstat(f["disk"]).st_ino == f["ino"], f["key"]
        assert blocks(f["disk"]) >= f["size"]
    assert f"/data/disk1/Media/{seeding['rel']}" in stack.gate.asked
    assert "glacier gate says" in stack.log()


def test_gate_down_means_no_offload(stack):
    f = stack.add("m", MOVIE)
    stack.gate.down = True
    stack.start()
    S.wait_until(lambda: stack.gate.asked, 30, what="a gate request")
    time.sleep(3)
    assert stack.state_of(f) == "whole" and not os.path.islink(f["disk"])


@defect("#83: the gate is asked once, before the long qualify. A torrent that starts to "
        "seed the file in place during qualify is not seen and the file is offloaded")
def test_torrent_that_appears_during_qualify_stops_the_offload(stack):
    f = stack.add("m", MOVIE)

    def appear(_paths):  # right after the gate said `free`
        stack.gate.torrents = {"b" * 40: {
            "save_path": "/data/disk1/Media/Movies", "name": "Alpha Movie (2001)",
            "progress": 100, "state": "Seeding"}}
    stack.gate.after_ask = appear
    stack.start()
    S.wait_until(lambda: stack.gate.asked, 30, what="the gate request")
    time.sleep(8)
    assert stack.state_of(f) == "whole" and not os.path.islink(f["disk"])
