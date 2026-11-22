"""offload.supervise, the `tiering` container's main process: tierfs first and last, the runner restarted on its
own with a back-off while the mount stays up, the pause file, the healthcheck, and the real thing as a process
with stand-ins for tierfs and the runner."""
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from tiering.offload import supervise as sv

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ------------------------------------------------------------------ fake clock and processes

class Proc:
    def __init__(self, name, world, stubborn=False):
        self.name, self.world, self.rc, self.stubborn = name, world, None, stubborn
        self.started = world.t

    def poll(self):
        return self.rc

    def terminate(self):
        self.world.events.append(("term", self.name, round(self.world.t, 1)))
        if not self.stubborn:
            self.rc = -15

    def kill(self):
        self.world.events.append(("kill", self.name, round(self.world.t, 1)))
        self.rc = -9

    def wait(self, timeout=None):
        if self.rc is None:
            self.world.t += timeout or 0
            raise subprocess.TimeoutExpired(self.name, timeout)
        return self.rc


class World:
    """A clock that moves only when the supervisor sleeps; `hook(world)` runs after every sleep."""

    def __init__(self, hook=None, healthy=lambda w, url: True, stubborn_runner=False):
        self.t = 1000.0
        self.events, self.procs = [], {"tierfs": [], "runner": []}
        self.hook, self.healthy, self.stubborn_runner = hook, healthy, stubborn_runner
        self.sup = None

    def popen(self, cmd):
        name = cmd[0]
        p = Proc(name, self, stubborn=(name == "runner" and self.stubborn_runner))
        self.procs[name].append(p)
        self.events.append(("start", name, round(self.t, 1)))
        return p

    def clock(self):
        return self.t

    def sleep(self, d):
        self.t += d
        assert self.t < 100000, "the supervisor never returned"
        if self.hook:
            self.hook(self)

    def check(self, url):
        return self.healthy(self, url)

    @property
    def runner(self):
        return self.procs["runner"][-1] if self.procs["runner"] else None

    @property
    def tierfs(self):
        return self.procs["tierfs"][0]

    def starts(self, name):
        return [t for (what, n, t) in self.events if what == "start" and n == name]


def settings(tmp_path, **env):
    base = {"SUPERVISE_TIERFS": '["tierfs"]', "SUPERVISE_RUNNER": '["runner"]',
            "SUPERVISE_PAUSE_FILE": str(tmp_path / "runner.paused")}
    base.update(env)
    return sv.Settings(base)


def run(tmp_path, hook, **kw):
    env = kw.pop("env", {})
    w = World(hook, **kw)
    w.detached = []
    sup = sv.Supervisor(settings(tmp_path, **env), popen=w.popen, clock=w.clock, sleep=w.sleep, check=w.check,
                        detach=lambda p: w.detached.append(
                            (p, len(w.procs["tierfs"]), w.tierfs.rc if w.procs["tierfs"] else None)))
    w.sup = sup
    return w, sup.run()


# ------------------------------------------------------------------ order, restarts, exits

def test_runner_restarts_with_a_doubling_backoff_and_tierfs_stays_up(tmp_path):
    def hook(w):
        r = w.runner
        if r and r.rc is None and w.t - r.started >= 1:
            r.rc = 1  # every run crashes after a second
        if len(w.starts("runner")) == 6:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook, env={"SUPERVISE_BACKOFF": "10,40"})
    assert rc == 0
    s = w.starts("runner")
    gaps = [round(b - a) for a, b in zip(s, s[1:])]
    assert gaps == [11, 21, 41, 41, 41]  # 1 s run + 10, 20, 40, then capped at 40
    assert len(w.procs["tierfs"]) == 1
    assert [e for e in w.events if e[1] == "tierfs"][1:] == [("term", "tierfs", round(w.t, 1))]


def test_backoff_is_reset_by_a_long_run(tmp_path):
    def hook(w):
        r = w.runner
        n = len(w.starts("runner"))
        if r and r.rc is None and n <= 2 and w.t - r.started >= 1:
            r.rc = 1
        if r and r.rc is None and n == 3 and w.t - r.started >= 700:
            r.rc = 1
        if n == 4:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook, env={"SUPERVISE_BACKOFF": "10,300", "SUPERVISE_RESET_S": "600"})
    s = w.starts("runner")
    assert [round(b - a) for a, b in zip(s, s[1:])] == [11, 21, 710]  # 10, 20, then back to 10


def test_runner_starts_only_after_tierfs_answers(tmp_path):
    def healthy(w, url):
        return w.t >= 1030

    def hook(w):
        if w.runner:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook, healthy=healthy)
    assert rc == 0
    assert w.starts("runner")[0] >= 1030


def test_tierfs_exiting_before_it_answers_ends_the_container(tmp_path):
    def hook(w):
        if w.t >= 1002:
            w.tierfs.rc = 3

    w, rc = run(tmp_path, hook, healthy=lambda w, url: False)
    assert rc == 3
    assert w.procs["runner"] == []


def test_tierfs_that_never_answers_is_stopped(tmp_path):
    w, rc = run(tmp_path, None, healthy=lambda w, url: False, env={"SUPERVISE_TIERFS_WAIT_S": "5"})
    assert rc == 1
    assert ("term", "tierfs") in [e[:2] for e in w.events]
    assert w.procs["runner"] == []


def test_signal_while_waiting_for_tierfs(tmp_path):
    def hook(w):
        w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook, healthy=lambda w, url: False)
    assert rc == 0
    assert w.tierfs.rc is not None and w.procs["runner"] == []


@pytest.mark.parametrize("tierfs_rc, want", [(-9, 1), (0, 1), (4, 4)])
def test_tierfs_dying_stops_the_runner_and_exits_non_zero(tmp_path, tierfs_rc, want):
    def hook(w):
        if w.runner and w.t >= 1010:
            w.tierfs.rc = tierfs_rc

    w, rc = run(tmp_path, hook)
    assert rc == want
    assert w.runner.rc is not None
    assert ("term", "runner") in [e[:2] for e in w.events]


def test_stop_order_is_runner_then_tierfs(tmp_path):
    def hook(w):
        if w.runner and w.t >= 1005:
            w.sup.signal = signal.SIGINT

    w, rc = run(tmp_path, hook)
    assert rc == 0
    terms = [e[1] for e in w.events if e[0] == "term"]
    assert terms == ["runner", "tierfs"]


def test_a_runner_that_ignores_sigterm_is_killed(tmp_path):
    def hook(w):
        if w.runner and w.t >= 1005:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook, stubborn_runner=True, env={"SUPERVISE_RUNNER_STOP_S": "7"})
    assert rc == 0
    names = [e[:2] for e in w.events if e[0] in ("term", "kill")]
    assert names == [("term", "runner"), ("kill", "runner"), ("term", "tierfs")]


# ------------------------------------------------------------------ pause file

def test_pause_file_stops_the_runner_and_keeps_it_stopped(tmp_path):
    pause = tmp_path / "runner.paused"

    def hook(w):
        if w.t >= 1010 and w.t < 1011:
            pause.touch()
        if w.t >= 1100 and pause.exists():
            pause.unlink()
        if w.t >= 1110:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook)
    assert rc == 0
    s = w.starts("runner")
    assert len(s) == 2
    assert 1100 <= s[1] < 1101  # resumed at once, no back-off
    term = [e for e in w.events if e[:2] == ("term", "runner")]
    assert 1010 <= term[0][2] < 1011
    assert len(w.procs["tierfs"]) == 1 and w.tierfs.rc is not None  # only stopped at the signal


def test_pause_file_present_at_start(tmp_path):
    (tmp_path / "runner.paused").touch()

    def hook(w):
        if w.t >= 1050:
            w.sup.signal = signal.SIGTERM

    w, rc = run(tmp_path, hook)
    assert rc == 0
    assert w.procs["runner"] == []


# ------------------------------------------------------------------ healthcheck and settings

def test_health(tmp_path, capsys):
    s = settings(tmp_path, TIERFS_HTTP="127.0.0.1:1", TIERING_HTTP="127.0.0.1:2")
    up = {s.tierfs_url, s.runner_url}
    assert sv.health(s, check=lambda u: u in up) == 0
    up = {s.tierfs_url}
    assert sv.health(s, check=lambda u: u in up) == 1
    assert "unhealthy: runner" in capsys.readouterr().out
    (tmp_path / "runner.paused").touch()
    assert sv.health(s, check=lambda u: u in up) == 0
    assert "ok (runner paused)" in capsys.readouterr().out
    up = set()
    assert sv.health(s, check=lambda u: u in up) == 1
    assert "unhealthy: tierfs" in capsys.readouterr().out


@pytest.mark.parametrize("spec, url", [
    (None, "http://127.0.0.1:9109/healthz"),
    ("127.0.0.1:9200", "http://127.0.0.1:9200/healthz"),
    (":9200", "http://127.0.0.1:9200/healthz"),
    ("0.0.0.0:9200", "http://127.0.0.1:9200/healthz"),
    ("localhost:9200", "http://localhost:9200/healthz"),
])
def test_local_url(spec, url):
    assert sv.local_url(spec, "127.0.0.1:9109") == url


def test_default_commands_and_timings(tmp_path):
    s = sv.Settings({})
    assert s.tierfs == ["/usr/local/bin/tierfs"]
    assert s.runner[1:] == ["-u", "-m", "offload.runner"]
    assert (s.backoff_min, s.backoff_max, s.reset_s, s.wait_s) == (10, 300, 600, 120)
    assert s.runner_url == "http://127.0.0.1:8471/healthz" and s.pause_file == "/state/runner.paused"


def test_bad_arguments():
    assert sv.main(["--nope"]) == 2


# ------------------------------------------------------------------ a mount left behind

@pytest.mark.parametrize("how", ["signal", "tierfs died"])
def test_a_left_mount_is_detached_before_start_and_after_tierfs_stops(tmp_path, how):
    def hook(w):
        if w.runner and w.t >= 1010:
            if how == "signal":
                w.sup.signal = signal.SIGTERM
            else:
                w.tierfs.rc = -9

    w, rc = run(tmp_path, hook, env={"TIERFS_MOUNT": "/mnt/addons/tierfs"})
    assert rc == (0 if how == "signal" else 1)
    first, last = w.detached
    assert first[:2] == ("/mnt/addons/tierfs", 0)  # before tierfs was started
    assert last[0] == "/mnt/addons/tierfs" and last[2] is not None  # after tierfs exited


def test_no_mount_configured_detaches_nothing(tmp_path):
    w, rc = run(tmp_path, None, healthy=lambda w, url: False, env={"SUPERVISE_TIERFS_WAIT_S": "1"})
    assert w.detached == [(None, 0, None), (None, 1, w.tierfs.rc)]
    sv.detach(None)  # the real one returns at once


MOUNTINFO = r"""22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
40 22 0:40 / /mnt/addons/tierfs rw,relatime shared:5 - xfs /dev/md1 rw
41 40 0:41 / /mnt/addons/tierfs rw,nosuid,nodev shared:6 - fuse.tierfs tierfs rw,user_id=0
42 22 0:42 / /mnt/my\040media rw shared:7 - fuse tierfs rw
43 22 0:43 / /mnt/other rw shared:8 - fuseblk /dev/sdb1 rw
"""


@pytest.mark.parametrize("path, want", [
    ("/mnt/addons/tierfs", True),
    ("/mnt/addons/tierfs/", True),
    ("/mnt/my media", True),
    ("/mnt/addons", False),
    ("/", False),
    ("/mnt/other", True),  # fuseblk is FUSE too, so it would be detached: TIERFS_MOUNT never points at one
])
def test_fuse_mounted_reads_mountinfo(path, want):
    assert sv.fuse_mounted(path, MOUNTINFO) is want


def test_fuse_mounted_ignores_a_plain_mount_at_the_path():
    assert sv.fuse_mounted("/mnt/addons/tierfs", MOUNTINFO.splitlines()[1]) is False


# ------------------------------------------------------------------ the real process

FAKE = r'''
import http.server, os, signal, sys
name, port, logf = sys.argv[1], int(sys.argv[2]), sys.argv[3]
def note(msg):
    with open(logf, "a") as f:
        f.write(f"{name} {msg} {os.getpid()}\n")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", port), H)
def bye(*_):
    note("stopped")
    os._exit(0)
signal.signal(signal.SIGTERM, bye)
note("started")
srv.serve_forever(poll_interval=0.1)
'''


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(pred, timeout=20, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Real:
    def __init__(self, tmp_path):
        self.log = tmp_path / "events.log"
        self.log.touch()
        fake = tmp_path / "fake.py"
        fake.write_text(FAKE)
        tport, rport = _free_port(), _free_port()
        self.env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "tiering"),
                        TIERFS_HTTP=f"127.0.0.1:{tport}", TIERING_HTTP=f"127.0.0.1:{rport}",
                        SUPERVISE_TIERFS=json.dumps([sys.executable, str(fake), "tierfs", str(tport), str(self.log)]),
                        SUPERVISE_RUNNER=json.dumps([sys.executable, str(fake), "runner", str(rport), str(self.log)]),
                        SUPERVISE_BACKOFF="0.3,0.3", SUPERVISE_PAUSE_FILE=str(tmp_path / "runner.paused"),
                        TIERFS_MOUNT="")  # nothing to detach
        self.p = subprocess.Popen([sys.executable, "-m", "offload.supervise"], env=self.env,
                                  cwd=os.path.join(REPO, "tiering"), stderr=subprocess.PIPE, text=True)

    def lines(self):
        return [ln.split() for ln in self.log.read_text().splitlines()]

    def pids(self, name, what="started"):
        return [int(p) for n, w, p in self.lines() if n == name and w == what]

    def health(self):
        return subprocess.run([sys.executable, "-m", "offload.supervise", "--health"], env=self.env,
                              cwd=os.path.join(REPO, "tiering"), capture_output=True, text=True).returncode

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(10)
        for n, w, pid in self.lines():
            if w == "started":
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.fixture
def real(tmp_path):
    r = Real(tmp_path)
    yield r
    r.close()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill 0
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def test_real_process_restart_health_and_stop(real):
    _wait(lambda: real.pids("runner"), what="the runner to start")
    _wait(lambda: real.health() == 0, what="both health checks")
    tierfs_pid = real.pids("tierfs")[0]
    first = real.pids("runner")[0]
    os.kill(first, signal.SIGKILL)
    _wait(lambda: len(real.pids("runner")) == 2, what="the runner to be started again")
    assert _alive(tierfs_pid) and real.pids("tierfs") == [tierfs_pid]
    _wait(lambda: real.health() == 0, what="health after the restart")

    real.p.send_signal(signal.SIGTERM)
    assert real.p.wait(20) == 0
    stopped = [n for n, w, _ in real.lines() if w == "stopped"]
    assert stopped == ["runner", "tierfs"]
    assert not _alive(tierfs_pid) and not _alive(real.pids("runner")[-1])


def test_real_process_tierfs_death_takes_the_runner_down(real):
    _wait(lambda: real.pids("runner"), what="the runner to start")
    runner_pid = real.pids("runner")[0]
    os.kill(real.pids("tierfs")[0], signal.SIGKILL)
    assert real.p.wait(20) == 1
    assert real.pids("runner", "stopped") == [runner_pid]
    assert "tierfs exited" in real.p.stderr.read()


def test_real_process_pause_file(real, tmp_path):
    _wait(lambda: real.pids("runner"), what="the runner to start")
    pause = tmp_path / "runner.paused"
    pause.touch()
    _wait(lambda: real.pids("runner", "stopped"), what="the runner to be paused")
    assert real.health() == 0  # tierfs answers, the runner is paused on purpose
    time.sleep(1)
    assert len(real.pids("runner")) == 1
    pause.unlink()
    _wait(lambda: len(real.pids("runner")) == 2, what="the runner to resume")
    assert len(real.pids("tierfs")) == 1 and real.p.poll() is None
