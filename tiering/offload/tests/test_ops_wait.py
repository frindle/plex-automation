"""Promote for waiting players: fill order, the fill file, retry and failure."""

import os
import threading
import time

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, Env, FakeReader

CS = 4 * MIB
SIZE = 300 * MIB  # head 60 MiB, tail 32 MiB: a 208 MiB middle of 52 chunks
HEAD = 60 * MIB
N = 52


def fill_lines(env):
    try:
        with open(os.path.join(os.path.dirname(env.manifest), f"{ID}.fill")) as f:
            return f.read().split("\n")[:-1]
    except FileNotFoundError:
        return None


@pytest.fixture
def big(tmp_path):
    env = Env(tmp_path, size=SIZE)
    assert env.offload().ok
    return env


class Spy(FakeReader):
    """Checks, on every call, that what it reported earlier is on disk and listed."""

    def __init__(self, env, **kw):
        super().__init__(env.data, **kw)
        self.env = env

    def read_range(self, remote_path, offset, length):
        lines = fill_lines(self.env) or []
        assert lines == [str(o) for o, _ in self.calls], "a chunk was listed out of step"
        for o, n in self.calls:
            with open(self.env.stub, "rb") as f:
                f.seek(o)
                assert f.read(n) == self.env.data[o:o + n], "listed before it was on disk"
        return super().read_range(remote_path, offset, length)


def test_fill_order_follows_a_moving_want(big):
    env = big
    spy = Spy(env)
    wants = iter([None, HEAD + 40 * CS, HEAD + 40 * CS, HEAD + 41 * CS])
    last = [None]

    def want():
        last[0] = next(wants, last[0])
        return last[0]

    r = env.promote(spy, want=want)
    assert r.ok and r.state == "whole"
    offs = [o for o, _ in spy.calls]
    # no want first: the start of the middle; then the cursor jumps to the want,
    # runs to the end of the middle, and wraps round to the chunks it skipped
    expected = [HEAD + i * CS for i in [0] + list(range(40, N)) + list(range(1, 40))]
    assert offs == expected
    assert len(set(offs)) == N
    assert os.path.exists(env.lib) and not os.path.islink(env.lib)


def test_first_want_sets_the_start(big):
    env = big
    spy = Spy(env)
    env.promote(spy, want=lambda: HEAD + 10 * CS)
    assert [o for o, _ in spy.calls][0] == HEAD + 10 * CS


def test_want_inside_the_next_64mib_keeps_the_cursor(big):
    env = big
    spy = Spy(env)
    seq = iter([HEAD + 10 * CS, HEAD + 12 * CS])  # 2 chunks ahead of the cursor after one read: inside the span

    last = [None]

    def want():
        last[0] = next(seq, last[0])
        return last[0]

    env.promote(spy, want=want)
    offs = [o for o, _ in spy.calls]
    assert offs[:4] == [HEAD + i * CS for i in (10, 11, 12, 13)]  # sequential, never skipped ahead


def _by_count(spy, steps):
    """want() that answers by how many chunks the spy has fetched: steps is
    [(fetched_below, want), ...] and the last want holds after that."""
    def want():
        n = len(spy.calls)
        for lim, w in steps:
            if n < lim:
                return w
        return steps[-1][1]
    return want


def test_want_for_chunks_already_on_disk_keeps_the_cursor(big):
    # the viewer seeks to chunk 40; then Plex opens the file again and asks for the
    # start of the middle, which is on disk: the fill stays with the viewer
    env = big
    spy = Spy(env)
    env.promote(spy, want=_by_count(spy, [(20, HEAD), (25, HEAD + 40 * CS), (N, HEAD)]))
    offs = [o for o, _ in spy.calls]
    assert offs == [HEAD + i * CS for i in list(range(20)) + list(range(40, N)) + list(range(20, 40))]


def test_want_with_a_hole_inside_the_span_still_moves_the_cursor(big):
    # chunk 10 is on disk but 20 is not, and 20 is inside the 64 MiB after it: a
    # reader there is about to wait, so the fill goes back for it
    env = big
    spy = Spy(env)
    env.promote(spy, want=_by_count(spy, [(20, HEAD), (25, HEAD + 40 * CS), (N, HEAD + 10 * CS)]))
    offs = [o for o, _ in spy.calls]
    assert offs == [HEAD + i * CS for i in
                    list(range(20)) + list(range(40, 45)) + list(range(20, 40)) + list(range(45, N))]


def test_resume_trusts_the_fill_file(big):
    env = big

    class DiesAfter(Spy):
        def read_range(self, remote_path, offset, length):
            if len(self.calls) == 5:
                raise ConnectionError("remote down")
            return super().read_range(remote_path, offset, length)

    first = DiesAfter(env)
    r = env.promote(first)
    assert not r.ok and r.state == "promoting"
    done = [o for o, _ in first.calls]
    assert len(done) == 5 and fill_lines(env) == [str(o) for o in done]
    second = FakeReader(env.data)
    r = env.promote(second)
    assert r.ok
    again = [o for o, _ in second.calls]
    assert not set(again) & set(done) and len(again) == N - 5


def test_resume_ignores_a_torn_last_line_and_a_failed_line(big):
    env = big
    fp = os.path.join(os.path.dirname(env.manifest), f"{ID}.fill")
    with open(fp, "w") as f:
        f.write(f"{HEAD}\nfailed {HEAD + CS}\n{HEAD + 2 * CS}")  # last line has no newline
    # chunk 0 is trusted but was never written: put it in the stub as a promote would
    with open(env.stub, "r+b") as f:
        f.seek(HEAD)
        f.write(env.data[HEAD:HEAD + CS])
    reader = FakeReader(env.data)
    r = env.promote(reader)
    assert r.ok
    offs = [o for o, _ in reader.calls]
    assert HEAD not in offs and HEAD + CS in offs and HEAD + 2 * CS in offs


def test_fetch_error_retried_with_backoff_per_chunk(big, monkeypatch):
    env = big
    clock = [1000.0]
    sleeps = []
    monkeypatch.setattr(ops, "_mono", lambda: clock[0])
    monkeypatch.setattr(ops, "_sleep", lambda d: (sleeps.append(d), clock.__setitem__(0, clock[0] + d)))
    monkeypatch.setenv("TIERING_PROMOTE_RETRY_S", "10")

    class Flaky(FakeReader):
        def read_range(self, remote_path, offset, length):
            self.calls.append((offset, length))
            if self.calls_per.get(offset, 0) < 3:  # every chunk fails 3 times: 1+2+4 s each
                self.calls_per[offset] = self.calls_per.get(offset, 0) + 1
                raise ConnectionError("blip")
            return self.data[offset:offset + length]

        calls_per = {}

    flaky = Flaky(env.data)
    flaky.calls_per = {}
    r = env.promote(flaky)
    assert r.ok  # a 10 s budget per chunk, 7 s used by each one
    assert sleeps[:3] == [1.0, 2.0, 4.0] and len(sleeps) == 3 * N


def test_retry_budget_runs_out_and_stays_resumable(big, monkeypatch):
    env = big
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(ops, "_mono", lambda: clock[0])
    monkeypatch.setattr(ops, "_sleep", lambda d: (sleeps.append(d), clock.__setitem__(0, clock[0] + d)))
    monkeypatch.setenv("TIERING_PROMOTE_RETRY_S", "20")
    r = env.promote(FakeReader(env.data, fail_times=10**6))
    assert not r.ok and r.state == "promoting"
    assert sum(sleeps) <= 20 and sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert fill_lines(env) == []  # nothing was listed
    assert env.promote(FakeReader(env.data)).ok


def test_two_mismatches_write_failed_and_fail_the_row(big):
    env = big
    bad = HEAD + 7 * CS + 100
    reader = FakeReader(env.data, corrupt_at=bad)
    r = env.promote(reader)
    assert not r.ok and r.state == "failed"
    assert st.get(env.db, ID)["state"] == "failed"
    lines = fill_lines(env)
    assert lines[-1] == f"failed {HEAD + 7 * CS}"
    assert lines[:-1] == [str(HEAD + i * CS) for i in range(7)]
    assert [o for o, _ in reader.calls].count(HEAD + 7 * CS) == 2  # fetched again once
    assert os.path.islink(env.lib)


def test_one_transient_mismatch_is_refetched(big):
    env = big

    class OnceBad(FakeReader):
        seen = 0

        def read_range(self, remote_path, offset, length):
            b = super().read_range(remote_path, offset, length)
            if offset == HEAD + 3 * CS and OnceBad.seen == 0:
                OnceBad.seen = 1
                return bytes(length)
            return b

    r = env.promote(OnceBad(env.data))
    assert r.ok
    assert fill_lines(env) is None


def test_fill_file_removed_on_success_with_manifest_and_chunks(big):
    env = big
    assert env.promote().ok
    d = os.path.dirname(env.manifest)
    assert os.listdir(d) == []


def test_restart_after_failed_drops_the_failed_line(big):
    env = big
    bad = HEAD + 2 * CS
    assert not env.promote(FakeReader(env.data, corrupt_at=bad)).ok
    assert fill_lines(env)[-1].startswith("failed")
    seen = []

    class Look(FakeReader):
        def read_range(self, remote_path, offset, length):
            seen.append(fill_lines(env))
            return super().read_range(remote_path, offset, length)

    assert env.promote(Look(env.data)).ok
    assert not any(ln.startswith("failed") for ln in seen[0])


# ---- every permanent failure ends the fill file with a `failed` line, so a
# ---- waiting player gets EIO at once instead of waiting out TIERFS_WAIT_S


def small(tmp_path):
    env = Env(tmp_path)
    assert env.offload().ok
    return env, st.get(env.db, ID)["head_len"]


def assert_ends_failed(env, offset=None):
    lines = fill_lines(env)
    assert lines and lines[-1].startswith("failed "), lines
    assert "" not in lines  # no blank line left by a torn-line repair
    if offset is not None:
        assert lines[-1] == f"failed {offset}"


def test_permanent_short_remote_writes_failed(tmp_path):
    env, head = small(tmp_path)
    trunc = FakeReader(env.data[:14 * MIB])  # the middle is [10, 18) MiB
    for _ in (1, 2):
        assert env.promote(trunc).state == "promoting"
        assert not any(ln.startswith("failed") for ln in fill_lines(env))  # transient: players keep waiting
    r = env.promote(trunc)
    assert r.state == "failed"
    assert_ends_failed(env, 14 * MIB)
    assert str(head) in fill_lines(env)  # what was listed stays listed


def test_reader_errors_after_the_retry_budget_write_failed_but_stay_resumable(tmp_path, monkeypatch):
    """A remote that answers with errors (not bad data) must not leave a waiting player
    for the whole TIERFS_WAIT_S: the fill file gets a `failed` line for a moment, the row
    stays `promoting`, the line comes off again (a later read must be able to ask for the
    file), and the next attempt finishes."""
    env, head = small(tmp_path)
    held = []
    monkeypatch.setattr(ops, "_sleep", lambda d: held.append((d, fill_lines(env))))
    gone = FakeReader(env.data, fail_times=10**6)
    r = env.promote(gone)  # TIERING_PROMOTE_RETRY_S is 0 in the tests
    assert not r.ok and r.state == "promoting"
    hold = [lines for d, lines in held if d == ops.FAILED_HOLD_S]
    assert len(hold) == 1 and hold[0] and hold[0][-1] == f"failed {head}", held
    assert "failed" not in " ".join(fill_lines(env) or []), "a later read must not fail at once"
    assert st.get(env.db, ID)["state"] == "promoting"
    assert env.promote().ok  # the remote is back
    assert not os.path.exists(os.path.join(env.root, "manifests", f"{ID}.fill"))


def test_fill_refusal_writes_failed(tmp_path):
    env, head = small(tmp_path)
    os.link(env.stub, env.stub + ".second")  # the stub is no longer a single-link file
    r = env.promote()
    assert not r.ok and r.state == "failed"
    assert st.get(env.db, ID)["state"] == "failed"
    assert_ends_failed(env, head)


def test_sha256_mismatch_leaves_a_failed_fill_file_and_a_retry_refetches(tmp_path):
    env, head = small(tmp_path)
    good = env.sha
    st.set_state(env.db, ID, "offloaded", sha256="0" * 64)
    r = env.promote()
    assert not r.ok and r.state == "failed"
    assert fill_lines(env) == [f"failed {head}"]  # chunks cannot be trusted, players told
    st.set_state(env.db, ID, "failed", sha256=good)
    reader = FakeReader(env.data)
    assert env.promote(reader).ok
    assert len(reader.calls) == 2  # both middle chunks fetched again
    assert fill_lines(env) is None


def test_failed_swap_writes_failed(tmp_path):
    env, head = small(tmp_path)

    def stop_after_step(step):
        env.fake_tierfs()
        if step == 3:  # chunks are in; L is replaced before the swap
            os.unlink(env.lib)
            with open(env.lib, "wb") as f:
                f.write(b"someone else's file")

    r = env.promote(after_step=stop_after_step)
    assert not r.ok and r.state == "failed"
    assert_ends_failed(env)


def test_chunk_mismatch_failed_line_has_no_blank_line(tmp_path):
    env, head = small(tmp_path)
    r = env.promote(FakeReader(env.data, corrupt_at=head + 5))
    assert r.state == "failed"
    assert_ends_failed(env, head)


def test_mark_failed_closes_a_torn_last_line(tmp_path):
    p = str(tmp_path / "x.fill")
    with open(p, "w") as f:
        f.write("100\n20")  # torn
    ops._mark_failed(p, 300)
    assert open(p).read() == "100\n20\nfailed 300\n"


# ---- data safety: the bytes are on disk before the line that vouches for them


def test_fdatasync_runs_between_the_write_and_the_fill_line(tmp_path, monkeypatch):
    env, head = small(tmp_path)
    log = []
    real_pwrite, real_sync, real_write = os.pwrite, os.fdatasync, os.write

    def pwrite(fd, data, off):
        log.append(("pwrite", off))
        return real_pwrite(fd, data, off)

    def fdatasync(fd):
        log.append(("fdatasync",))
        return real_sync(fd)

    def write(fd, data):
        if data.strip().isdigit():
            log.append(("line", int(data)))
        return real_write(fd, data)

    monkeypatch.setattr(os, "pwrite", pwrite)
    monkeypatch.setattr(os, "fdatasync", fdatasync)
    monkeypatch.setattr(os, "write", write)
    assert env.promote().ok
    kinds = [e[0] for e in log if e[0] in ("pwrite", "fdatasync", "line")]
    assert kinds.count("line") == 2
    for i, k in enumerate(kinds):
        if k == "line":  # the nearest earlier events are the chunk's write and its sync
            assert kinds[i - 2:i] == ["pwrite", "fdatasync"], kinds


# ---- someone is watching: wide and first; nobody is: one at a time, and it yields


class Wide(FakeReader):
    """Counts the reads in flight at once; each read takes a moment, like the remote."""

    def __init__(self, data, **kw):
        super().__init__(data, **kw)
        self.mu = threading.Lock()
        self.now = self.most = 0

    def read_range(self, remote_path, offset, length):
        with self.mu:
            self.now += 1
            self.most = max(self.most, self.now)
        try:
            time.sleep(0.01)
            return super().read_range(remote_path, offset, length)
        finally:
            with self.mu:
                self.now -= 1


def test_watched_promote_fetches_several_chunks_at_once(big, monkeypatch):
    env = big
    monkeypatch.setenv("TIERING_PROMOTE_STREAM_FETCHES", "4")
    reader, pauses = Wide(env.data), []
    r = env.promote(reader, urgent=lambda: True, pause=lambda: pauses.append(1))
    assert r.ok and r.state == "whole"
    assert reader.most == 4, "a watched file is fetched 4 chunks at a time"
    assert pauses == [], "a watched promote never yields"
    assert sorted(o for o, _ in reader.calls) == [HEAD + i * CS for i in range(N)]
    assert env.read_lib() == env.data


def test_unwatched_promote_is_one_at_a_time_and_yields_before_each_chunk(big):
    env = big
    reader, pauses = Wide(env.data), []
    r = env.promote(reader, urgent=lambda: False, pause=lambda: pauses.append(reader.now))
    assert r.ok and r.state == "whole"
    assert reader.most == 1
    assert len(pauses) == N and set(pauses) == {0}, "it yields with nothing in flight"


def test_promote_turns_wide_when_play_is_pressed(big, monkeypatch):
    env = big
    monkeypatch.setenv("TIERING_PROMOTE_STREAM_FETCHES", "6")
    reader, pauses = Wide(env.data), []
    watched = lambda: len(reader.calls) >= 5  # noqa: E731
    r = env.promote(reader, urgent=watched, pause=lambda: pauses.append(1))
    assert r.ok and r.state == "whole"
    assert reader.most > 1 and len(pauses) == 5


def test_watched_wide_fill_starts_at_the_want_and_lists_every_chunk(big):
    env = big
    reader = Wide(env.data)
    r = env.promote(reader, urgent=lambda: True, want=lambda: HEAD + 30 * CS)
    assert r.ok
    first = sorted(o for o, _ in reader.calls[:6])
    assert first == [HEAD + i * CS for i in range(30, 36)], "the first wave is at the want"


def test_watched_wide_mismatch_fails_the_row_with_a_failed_line(big):
    env = big
    bad = HEAD + 9 * CS + 7
    r = env.promote(Wide(env.data, corrupt_at=bad), urgent=lambda: True)
    assert not r.ok and r.state == "failed"
    assert fill_lines(env)[-1] == f"failed {HEAD + 9 * CS}"
    assert os.path.islink(env.lib)


def test_stream_fetches_setting_is_bounded(monkeypatch):
    for v, want in (("0", 1), ("-3", 1), ("8", 8), ("999", ops.MAX_STREAM_FETCHES),
                    ("x", ops.DEFAULT_STREAM_FETCHES)):
        monkeypatch.setenv("TIERING_PROMOTE_STREAM_FETCHES", v)
        assert ops._stream_fetches() == want
