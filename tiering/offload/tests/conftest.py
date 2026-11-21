import pytest

from tiering.offload import ops


@pytest.fixture(autouse=True)
def _apply_env(monkeypatch):
    """Apply guards: the env switches are set, and the temp dirs count as a real disk
    file system (tests of the guard patch ops._fs_type again)."""
    monkeypatch.setenv("TIERING_APPLY", "1")
    try:  # a process that every PID namespace with /proc can see
        with open("/proc/1/comm") as f:
            canary = f.read().strip()
    except OSError:
        canary = "canary-proc"
    monkeypatch.setenv("TIERING_PROC_CANARY", canary)
    monkeypatch.setattr(ops, "_fs_type", lambda p: "xfs")

@pytest.fixture(autouse=True)
def _no_grab_delay(monkeypatch):
    # The real client spaces NZB grabs apart; tests do not wait for that.
    monkeypatch.setenv("NEWZNAB_GRAB_INTERVAL", "0")
