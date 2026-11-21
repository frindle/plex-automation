"""Promote for waiting players: fill order, the fill file, retry and failure."""

import os

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
