"""Run tierfs and the offload runner together: the main process of the `tiering` container (tiering/Dockerfile,
docker-compose.tiering.yml).

tierfs is the FUSE mount that Plex and the arrs read offloaded files through. The runner moves files off and back
and works through that mount. So the mount comes up first and goes down last:
  1. Start tierfs and wait until its /healthz answers (SUPERVISE_TIERFS_WAIT_S, default 120 s). If it exits first
     or never answers, stop it and exit non-zero: Docker's restart policy tries again.
  2. Start the runner. Whenever the runner exits, start it again after a back-off (10 s, doubling up to 5 min, and
     back to 10 s once a run lasted 10 min). The mount stays up meanwhile, so playback is never touched.
  3. If tierfs exits on its own, stop the runner and exit non-zero, so Docker restarts both. The runner never runs
     without the mount. A tierfs that died leaves a dead FUSE mount at TIERFS_MOUNT, on the host too (the bind is
     shared), and Docker cannot bind that path again ("file exists" / "transport endpoint is not connected"), so
     the restart would fail for good. So before it starts tierfs and whenever it exits, the supervisor detaches any
     FUSE mount still at TIERFS_MOUNT; the detach reaches the host the same way the mount did.
  4. On SIGTERM or SIGINT (docker stop): stop the runner first (SIGTERM, SIGKILL after SUPERVISE_RUNNER_STOP_S,
     default 60), then tierfs, which unmounts (SUPERVISE_TIERFS_STOP_S, default 20). The compose file's
     stop_grace_period covers both.
  5. Pausing offloading without touching the mount: while the pause file exists (SUPERVISE_PAUSE_FILE, default
     /state/runner.paused, so `${TIERING_STATE_DIR}/runner.paused` on the host), the runner is stopped and not
     started again. Delete the file and it starts within a second.
`python -m offload.supervise --health` is the healthcheck: exit 0 when both /healthz endpoints answer 200, or
tierfs's does and the runner is paused.

Both programs read their own settings from the environment. TIERFS_HTTP and TIERING_HTTP (defaults 127.0.0.1:9109
and 127.0.0.1:8471) are read here too, so the checks reach the same ports.
For tests: SUPERVISE_TIERFS and SUPERVISE_RUNNER replace the commands (JSON lists), and SUPERVISE_BACKOFF="min,max",
SUPERVISE_RESET_S and the stop timeouts shorten the timings.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

TIERFS_CMD = ["/usr/local/bin/tierfs"]
RUNNER_CMD = [sys.executable, "-u", "-m", "offload.runner"]
TICK = 0.2
# Health checks go straight to localhost, never through an http_proxy from the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str) -> None:
    print(f"supervise: {msg}", file=sys.stderr, flush=True)


def _num(env, name, default) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError:
        return float(default)


def local_url(spec: str | None, default: str) -> str:
    """The /healthz URL for a listen address such as 127.0.0.1:9109, :9109 or 0.0.0.0:9109."""
    host, _, port = (spec or default).rpartition(":")
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{port}/healthz"


class Settings:
    def __init__(self, env):
        self.tierfs = json.loads(env["SUPERVISE_TIERFS"]) if env.get("SUPERVISE_TIERFS") else list(TIERFS_CMD)
        self.runner = json.loads(env["SUPERVISE_RUNNER"]) if env.get("SUPERVISE_RUNNER") else list(RUNNER_CMD)
        self.tierfs_url = local_url(env.get("TIERFS_HTTP"), "127.0.0.1:9109")
        self.runner_url = local_url(env.get("TIERING_HTTP"), "127.0.0.1:8471")
        self.wait_s = _num(env, "SUPERVISE_TIERFS_WAIT_S", 120)
        lo, _, hi = (env.get("SUPERVISE_BACKOFF") or "10,300").partition(",")
        self.backoff_min = float(lo)
        self.backoff_max = max(self.backoff_min, float(hi or lo))
        self.reset_s = _num(env, "SUPERVISE_RESET_S", 600)
        self.runner_stop_s = _num(env, "SUPERVISE_RUNNER_STOP_S", 60)
        self.tierfs_stop_s = _num(env, "SUPERVISE_TIERFS_STOP_S", 20)
        self.pause_file = env.get("SUPERVISE_PAUSE_FILE") or "/state/runner.paused"
        self.mount = env.get("TIERFS_MOUNT") or None

    def paused(self) -> bool:
        return os.path.exists(self.pause_file)


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def fuse_mounted(path: str, mountinfo: str | None = None) -> bool:
    """Whether a FUSE file system is mounted exactly at path. Read from mountinfo, never by touching the path: a
    dead FUSE mount answers every stat with ENOTCONN."""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo") as f:
                mountinfo = f.read()
        except OSError:
            return False
    want = os.path.normpath(path)
    for ln in mountinfo.splitlines():
        left, sep, right = ln.partition(" - ")
        f, r = left.split(), right.split()
        if sep and len(f) > 4 and r and _unescape(f[4]) == want and r[0].startswith("fuse"):
            return True
    return False


def detach(path: str | None) -> None:
    """Lazily unmount every FUSE mount stacked at path (MNT_DETACH, fusermount3 -u -z as the fallback)."""
    if not path:
        return
    for _ in range(8):  # one per stacked mount; never loops forever
        if not fuse_mounted(path):
            return
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.umount2(os.fsencode(path), 2) != 0:  # 2 = MNT_DETACH
            err = os.strerror(ctypes.get_errno())
            r = subprocess.run(["fusermount3", "-u", "-z", path], capture_output=True)
            if r.returncode != 0:
                log(f"could not detach the FUSE mount at {path}: {err}")
                return
        log(f"detached a FUSE mount left at {path}")


def healthy(url: str, timeout: float = 3.0) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # refused, timed out, HTTP error, garbled reply: all mean "not healthy"
        return False


def stop(name: str, proc, timeout: float) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"stopping {name}")
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        log(f"{name} did not stop within {timeout:.0f}s: killing it")
        proc.kill()
        proc.wait()


def _exit_code(rc) -> int:
    return rc if isinstance(rc, int) and rc > 0 else 1


class Supervisor:
    def __init__(self, settings: Settings, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
                 check=healthy, detach=detach):
        self.s = settings
        self.popen = popen
        self.clock = clock
        self.sleep = sleep
        self.check = check
        self.detach = detach
        self.signal = None
        self.runner_starts = 0

    def on_signal(self, signum, _frame=None) -> None:
        self.signal = signum

    def _wait_for_tierfs(self, tierfs):
        """None once tierfs answers, else the exit code to return."""
        s = self.s
        deadline = self.clock() + s.wait_s
        while not self.check(s.tierfs_url):
            if self.signal:
                stop("tierfs", tierfs, s.tierfs_stop_s)
                return 0
            rc = tierfs.poll()
            if rc is not None:
                log(f"tierfs exited ({rc}) before its health check answered")
                return _exit_code(rc)
            if self.clock() > deadline:
                log(f"tierfs did not answer its health check within {s.wait_s:.0f}s")
                stop("tierfs", tierfs, s.tierfs_stop_s)
                return 1
            self.sleep(0.5)
        return None

    def run(self) -> int:
        self.detach(self.s.mount)
        try:
            return self._run()
        finally:
            self.detach(self.s.mount)

    def _run(self) -> int:
        s = self.s
        log("starting tierfs")
        tierfs = self.popen(s.tierfs)
        rc = self._wait_for_tierfs(tierfs)
        if rc is not None:
            return rc
        log("tierfs is up")
        runner = None
        started = next_start = self.clock()
        backoff = s.backoff_min
        was_paused = False
        while True:
            if self.signal:
                log(f"signal {self.signal}: stopping the runner, then tierfs")
                stop("runner", runner, s.runner_stop_s)
                stop("tierfs", tierfs, s.tierfs_stop_s)
                return 0
            rc = tierfs.poll()
            if rc is not None:
                log(f"tierfs exited ({rc}): stopping the runner so Docker restarts the container")
                stop("runner", runner, s.runner_stop_s)
                return _exit_code(rc)
            now = self.clock()
            if s.paused():
                if not was_paused:
                    log(f"{s.pause_file} exists: the runner is paused (tierfs stays up); delete it to resume")
                    was_paused = True
                stop("runner", runner, s.runner_stop_s)
                runner, next_start, backoff = None, now, s.backoff_min
            elif was_paused:
                log("pause file gone: resuming the runner")
                was_paused = False
            elif runner is None:
                if now >= next_start:
                    log("starting the runner")
                    runner = self.popen(s.runner)
                    self.runner_starts += 1
                    started = now
            else:
                rc = runner.poll()
                if rc is not None:
                    ran = now - started
                    if ran >= s.reset_s:
                        backoff = s.backoff_min
                    log(f"runner exited ({rc}) after {ran:.0f}s; starting it again in {backoff:.0f}s "
                        "(tierfs stays up)")
                    runner, next_start = None, now + backoff
                    backoff = min(backoff * 2, s.backoff_max)
            self.sleep(TICK)


def health(s: Settings, check=healthy) -> int:
    paused = s.paused()
    bad = [name for name, url in (("tierfs", s.tierfs_url), ("runner", s.runner_url))
           if not (name == "runner" and paused) and not check(url)]
    print("unhealthy: " + ", ".join(bad) if bad else "ok (runner paused)" if paused else "ok")
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    s = Settings(os.environ)
    if argv == ["--health"]:
        return health(s)
    if argv:
        print("usage: python -m offload.supervise [--health]", file=sys.stderr)
        return 2
    sup = Supervisor(s)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, sup.on_signal)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
