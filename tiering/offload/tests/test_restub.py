"""Restub: rebuild a lost stub (and L) from the remote's head and tail only."""

import json
import os

import pytest

from tiering.offload import ops, restub
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, MIB, FakeReader, assert_offloaded, tree
from tiering.offload.tests.test_ops import env as env  # noqa: F401 (fixture)


class Kill(BaseException):
    """A crash: not caught by anything that catches Exception."""


class Gone(FakeReader):
    """The articles of the tail are gone: the remote answers zeros."""

    def __init__(self, data, tail_from):
        super().__init__(data)
        self.tail_from = tail_from

    def read_range(self, remote_path, offset, length):
        b = super().read_range(remote_path, offset, length)
        return bytes(len(b)) if offset >= self.tail_from else b


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("TIERING_RESTUB_RETRY_S", "0")
    monkeypatch.setattr(ops, "_sleep", lambda s: None)


def offloaded(env):  # noqa: F811
    assert env.offload().ok
    assert_offloaded(env)
    return st.get(env.db, ID)


def run(env, reader=None, **kw):  # noqa: F811
    kw.setdefault("apply", True)
    kw.setdefault("alerts", str(env.root / "alerts.jsonl"))
    return restub.restub(
        env.db, reader or FakeReader(env.data), manifest_dir=str(env.root / "manifests"),
        tierfs_mount=str(env.root / "mount"), stub_roots=[str(env.root / "stubs")],
        path_map="", **kw)


def alerts(env):  # noqa: F811
    p = env.root / "alerts.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def only_edges(reader, row):
    """Every read the reader saw is inside the head or the tail, never the middle."""
    hl, tl, size = row["head_len"], row["tail_len"], row["size"]
    for off, n in reader.calls:
        assert off + n <= hl or off >= size - tl, (off, n)
    assert sum(n for _o, n in reader.calls) == hl + tl


def test_missing_stub_rebuilt_from_head_and_tail_only(env):  # noqa: F811
    row = offloaded(env)
    os.unlink(env.stub)
    rd = FakeReader(env.data)
    s = run(env, rd)
    assert s["rebuilt"] == [ID] and not s["failed"] and not s["error"]
    assert_offloaded(env)
    only_edges(rd, row)
    assert st.get(env.db, ID)["state"] == "offloaded"
    assert not os.path.lexists(env.stub + restub.TMP_SUFFIX)


def test_corrupted_head_rebuilt(env):  # noqa: F811
    offloaded(env)
    with open(env.stub, "r+b") as f:
        f.seek(100)
        f.write(b"\x00" * 16)
    s = run(env)
    assert s["rebuilt"] == [ID]
    assert_offloaded(env)


def test_corrupted_tail_rebuilt(env):  # noqa: F811
    offloaded(env)
    with open(env.stub, "r+b") as f:
        f.seek(len(env.data) - 10)
        f.write(b"\x00" * 4)
    assert run(env)["rebuilt"] == [ID]
    assert_offloaded(env)


def test_missing_stub_and_l_both_restored(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.lib)
    s = run(env)
    assert s["rebuilt"] == [ID]
    assert_offloaded(env)
    assert os.readlink(env.lib) == env.entry


def test_missing_l_only_needs_no_remote(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.lib)
    rd = FakeReader(env.data, fail_times=99)  # would fail if asked
    s = run(env, rd)
    assert s["rebuilt"] == [ID] and rd.calls == []
    assert os.readlink(env.lib) == env.entry


def test_healthy_is_noop_and_idempotent(env):  # noqa: F811
    offloaded(env)
    before = tree(env.root)
    rd = FakeReader(env.data)
    s = run(env, rd)
    assert s["ok"] == [ID] and not s["rebuilt"] and rd.calls == []
    assert tree(env.root) == before
    os.unlink(env.stub)
    run(env)
    s = run(env, rd)  # second run after a repair does nothing
    assert s["ok"] == [ID] and not s["rebuilt"]


def test_missing_manifest_reported_not_rebuilt(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.manifest)
    rd = FakeReader(env.data)
    s = run(env, rd)
    assert ID in s["no_manifest"] and not s["rebuilt"] and rd.calls == []
    assert not os.path.lexists(env.stub)


def test_missing_chunk_list_reported(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.chunks)
    assert ID in run(env)["no_manifest"]
    assert not os.path.lexists(env.stub)


def test_damaged_chunk_list_reported(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    with open(env.chunks, "r+b") as f:
        f.write(b"\xff")
    assert ID in run(env)["no_manifest"]


def test_article_gone_fails_row_with_alert_and_leaves_nothing(env):  # noqa: F811
    row = offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.lib)
    s = run(env, Gone(env.data, row["size"] - row["tail_len"]))
    assert ID in s["failed"] and not s["rebuilt"]
    r = st.get(env.db, ID)
    assert r["state"] == "failed" and "cannot be read" in r["restore_note"]
    assert [a["kind"] for a in alerts(env)] == ["restub_unreadable"]
    assert not os.path.lexists(env.stub) and not os.path.lexists(env.stub + restub.TMP_SUFFIX)
    assert not os.path.lexists(env.lib)  # nothing half-written at L


def test_failed_row_not_retried(env):  # noqa: F811
    row = offloaded(env)
    os.unlink(env.stub)
    run(env, Gone(env.data, row["size"] - row["tail_len"]))
    rd = FakeReader(env.data)
    s = run(env, rd)
    assert not s["rebuilt"] and rd.calls == []


def test_altmount_down_refuses_and_writes_nothing(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    before = tree(env.root)
    s = run(env, FakeReader(env.data, fail_times=99))
    assert "down" in s["error"] and not s["rebuilt"]
    assert tree(env.root) == before
    assert st.get(env.db, ID)["state"] == "offloaded"


def test_remote_dies_midway_is_resumable_not_failed(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)

    class Dies(FakeReader):
        def read_range(self, remote_path, offset, length):
            if offset == 0 and length > 1:
                raise ConnectionError("gone mid-run")
            return super().read_range(remote_path, offset, length)

    s = run(env, Dies(env.data))
    assert "went away" in s["error"]
    assert st.get(env.db, ID)["state"] == "offloaded"
    assert not os.path.lexists(env.stub) and not os.path.lexists(env.stub + restub.TMP_SUFFIX)
    assert run(env)["rebuilt"] == [ID]
    assert_offloaded(env)


def test_dry_run_writes_nothing(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.lib)
    before = tree(env.root)
    rd = FakeReader(env.data)
    s = run(env, rd, apply=False)
    assert s["would_rebuild"] == [ID] and not s["rebuilt"] and s["dry_run"]
    assert rd.calls == [] and tree(env.root) == before
    assert not alerts(env)


def test_apply_needs_env_switch(env, monkeypatch):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    monkeypatch.delenv("TIERING_APPLY")
    assert "TIERING_APPLY" in run(env)["error"]
    assert not os.path.lexists(env.stub)


def test_cap_defers_the_rest(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    s = run(env, cap=0)
    assert s["deferred"] == [ID] and not s["rebuilt"]
    assert not os.path.lexists(env.stub)


def test_resume_after_crash_with_stale_temp(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    with open(env.stub + restub.TMP_SUFFIX, "wb") as f:  # a killed run left this
        f.write(b"junk")
    assert run(env)["rebuilt"] == [ID]
    assert_offloaded(env)
    assert not os.path.lexists(env.stub + restub.TMP_SUFFIX)


def test_crash_between_stub_and_l_resumes(env, monkeypatch):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.lib)

    def boom(plan):
        raise Kill

    monkeypatch.setattr(restub, "_restore_l", boom)
    with pytest.raises(Kill):
        run(env)
    monkeypatch.undo()
    assert os.path.isfile(env.stub) and not os.path.lexists(env.lib)
    monkeypatch.setenv("TIERING_APPLY", "1")
    rd = FakeReader(env.data, fail_times=99)  # the stub is good: no remote needed
    s = run(env, rd)
    assert s["rebuilt"] == [ID] and rd.calls == []
    assert_offloaded(env)


def test_crash_while_fetching_leaves_no_temp(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)

    class Dies(FakeReader):
        def read_range(self, remote_path, offset, length):
            if offset == 0 and length > 1:
                raise Kill
            return super().read_range(remote_path, offset, length)

    with pytest.raises(Kill):
        run(env, Dies(env.data))
    assert not os.path.lexists(env.stub) and not os.path.lexists(env.stub + restub.TMP_SUFFIX)
    assert run(env)["rebuilt"] == [ID]


def test_stub_root_not_present_refused(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.rmdir(env.root / "stubs")
    s = run(env)
    assert ID in s["refused"] and "stub root" in s["refused"][ID]
    assert not (env.root / "stubs").exists()  # never created


def test_regular_file_at_l_is_not_touched(env):  # noqa: F811
    offloaded(env)
    os.unlink(env.stub)
    os.unlink(env.lib)
    with open(env.lib, "wb") as f:
        f.write(b"a new import")
    s = run(env)
    assert ID in s["refused"] and not s["rebuilt"]
    assert open(env.lib, "rb").read() == b"a new import"
    assert not os.path.lexists(env.stub)


def test_twin_copy_used_when_first_is_gone(env):  # noqa: F811
    offloaded(env)
    st.upsert(env.db, {"id": ID, "twin_remote_path": "/r2/Movie.mkv"})
    os.unlink(env.stub)

    class Two(FakeReader):
        def read_range(self, remote_path, offset, length):
            b = super().read_range(remote_path, offset, length)
            return bytes(len(b)) if remote_path == "/r/Movie.mkv" else b

    assert run(env, Two(env.data))["rebuilt"] == [ID]
    assert_offloaded(env)


def test_cli_dry_run_and_runner_subcommand(env, monkeypatch, capsys):  # noqa: F811
    from tiering.offload import runner
    offloaded(env)
    os.unlink(env.stub)
    for k, v in {"TIERING_DB": str(env.root / "state.db"),
                 "TIERFS_MANIFESTS": str(env.root / "manifests"),
                 "TIERFS_MOUNT": str(env.root / "mount"),
                 "TIERFS_STUB_ROOTS": str(env.root / "stubs")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("tiering.offload.altmount.WebDavReader", lambda: FakeReader(env.data))
    assert restub.main(["restub"]) == 0
    out = capsys.readouterr().out
    assert "would_rebuild: 1" in out and not os.path.lexists(env.stub)
    assert runner.main(["restub", "--apply"]) == 0
    assert os.path.isfile(env.stub)
