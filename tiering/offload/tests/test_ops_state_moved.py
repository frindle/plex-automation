"""A row failed by someone else (a bad_chunk) while an offload runs must stop the offload
before the punch; the stub keeps head, middle and tail, and L stays the tierfs symlink."""
import os

import pytest

from tiering.offload import ops
from tiering.offload import state as st
from tiering.offload.tests.test_ops import ID, Env


def _fail_after(env, at):
    inner = env.hook()

    def hook(step):
        inner(step)
        if step == at:  # the runner's bad_chunk handler, between two steps
            st.set_state(env.db, ID, "failed", restore_note="bad_chunk at offset 8")
    return hook


@pytest.mark.parametrize("at", [4, 5])
def test_failed_row_is_not_punched_or_overwritten(tmp_path, at):
    env = Env(tmp_path)
    r = env.offload(after_step=_fail_after(env, at))
    assert not r.ok
    row = st.get(env.db, ID)
    assert row["state"] == "failed" and row["restore_note"] == "bad_chunk at offset 8"
    with open(env.stub, "rb") as f:
        assert f.read() == env.data  # nothing punched: head, middle and tail all there
    assert os.path.islink(env.lib) and os.readlink(env.lib) == env.entry
    kinds = [e["kind"] for e in st.events(env.db, ID)]
    assert "offload_state_moved" in kinds and "offloaded" not in kinds


def test_failed_between_step_6_record_and_final_write_is_kept(tmp_path):
    env = Env(tmp_path)
    r = env.offload(after_step=_fail_after(env, 6))
    assert not r.ok and st.get(env.db, ID)["state"] == "failed"
