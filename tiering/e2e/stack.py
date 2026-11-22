"""The whole offload tier on one machine, with stand-ins for everything outside it.

Real: the runner (`python -m offload.runner`, as a subprocess), ops, qualify,
register, weekly, the state DB, the glacier gate's classifier, and tierfs when
E2E_TIERFS_BIN names a built binary (FUSE; used in CI).
Stand-ins: WebDAV remote (fake_webdav.py), AltMount import API, a Newznab
indexer, two NNTP providers, Radarr, Sonarr, the Plex API and the torrent list
behind the gate. Nothing here talks to a real service.

Without E2E_TIERFS_BIN a small emulator keeps `${TIERFS_MOUNT}/<id><ext>` in
step with the manifests, the way tierfs lists them, and `read_through` rebuilds
what tierfs would serve (head and tail from the stub, the middle from filled
chunks or the remote).

Paths. Plex reports `/share/Media/...`, the arrs report `/arr/media/...`, the
torrent client sees `/data/diskN/...`; only the disk paths exist. The maps
between them are the ones under test.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import fake_webdav  # noqa: E402
import tiering_gate  # noqa: E402
from tiering.offload import state as st  # noqa: E402
from tiering.offload.tests import fakes_intake as fk  # noqa: E402

MIB = 1 << 20
CHUNK = 4 * MIB
SHARE = "/share/Media"
ARR = "/arr/media"
ALT_MOUNT = "/mnt/remotes/standin"
TIERFS_BIN = os.environ.get("E2E_TIERFS_BIN", "")
ROOT = os.geteuid() == 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(MIB), b""):
            h.update(b)
    return h.hexdigest()


def wait_until(pred, timeout=60.0, poll=0.1, what="condition"):
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() >= end:
            what = what() if callable(what) else what
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(poll)


# ------------------------------------------------------------------ stand-ins

class _Q(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class _PlexHandler(_Q):
    def do_GET(self):
        o = self.server.owner
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        o.requests += 1
        if o.fail:
            return self._json({}, 500)
        start = int(self.headers.get("X-Plex-Container-Start") or 0)
        size = int(self.headers.get("X-Plex-Container-Size") or 10 ** 6)
        rows = None
        if u.path == "/library/sections":
            return self._json({"MediaContainer": {"Directory": [
                {"key": str(i + 1), "type": t, "title": name}
                for i, (name, t) in enumerate(o.sections)]}})
        if u.path.startswith("/library/sections/") and u.path.endswith("/all"):
            name = o.sections[int(u.path.split("/")[3]) - 1][0]
            rows = [o.row(it) for it in o.items if it["section"] == name]
            if q.get("inProgress"):
                rows = [r for r in rows if r.get("viewOffset")]
        elif u.path == "/status/sessions/history/all":
            rows = sorted(o.plays, key=lambda p: -p["viewedAt"])
        elif u.path == "/status/sessions":
            rows = [o.row(it) for it in o.items if it["ratingKey"] in o.sessions]
        elif u.path == "/library/onDeck":
            rows = []
        if rows is None:
            return self._json({}, 404)
        self._json({"MediaContainer": {"totalSize": len(rows),
                                       "Metadata": rows[start:start + size]}})


class FakePlex(fk._Server):
    """The Plex API calls the runner makes. Mutate items, plays and sessions directly."""

    def __init__(self):
        self.sections = []  # (title, "movie" | "show")
        self.items = []
        self.plays = []
        self.sessions = set()
        self.fail = False
        self.requests = 0
        super().__init__(_PlexHandler)

    @staticmethod
    def row(it):
        r = {k: v for k, v in it.items() if k not in ("files", "section", "videoResolution")}
        r["librarySectionTitle"] = it["section"]
        r["Media"] = [{"videoResolution": it.get("videoResolution", "1080"),
                       "Part": [{"file": f} for f in it["files"]]}]
        return r

    def play(self, rk, at=None):
        self.plays.append({"ratingKey": str(rk), "viewedAt": int(at or time.time()),
                           "type": "movie"})


class _GateHandler(_Q):
    def do_POST(self):
        o = self.server.owner
        n = int(self.headers.get("Content-Length") or 0)
        paths = [p for p in self.rfile.read(n).decode().splitlines() if p.strip()]
        o.asked += paths
        if o.down:
            return self._json({}, 503)
        rows = tiering_gate.classify_paths(paths, o.torrents, keep_seeding=o.keep_seeding)
        b = tiering_gate.to_tsv(rows).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
        if o.after_ask:
            o.after_ask(paths)


class FakeGate(fk._Server):
    """`/tiering/torrent-gate` over the real classifier and a torrent list the test sets."""

    def __init__(self):
        self.torrents = {}
        self.keep_seeding = False
        self.down = False
        self.asked = []
        self.after_ask = None  # called with the paths once an answer has gone out
        super().__init__(_GateHandler)


class _AmHandler(fk._AmHandler):
    def do_POST(self):
        o = self.server.owner
        with o.lock:
            before = len(o.uploads)
            super().do_POST()
            if len(o.uploads) > before:
                o.job_dir[list(o.jobs)[-1]] = o.uploads[-1][0][:-len(".nzb")]

    def _history(self, o, q, query):
        if q.get("apikey", [""])[0] != o.key:
            return self._json({"status": False, "error": "Invalid API key"})
        jid = query.get("nzo_ids", [""])[0]
        slots = []
        if jid in o.job_dir:
            status = "Failed" if o.fail else "Completed"
            slots = [{"nzo_id": jid, "status": status,
                      "storage": "" if o.fail else f"{ALT_MOUNT}/{o.job_dir[jid]}"}]
        self._json({"history": {"slots": slots, "noofslots": len(slots)}})


class FakeAltMount(fk.FakeAltMount):
    """Import API stand-in: each job lands in the remote directory named after its NZB."""

    def __init__(self):
        self.job_dir = {}
        self.lock = threading.Lock()
        fk.FakeAltMount.__init__(self, storage="")
        self.httpd.RequestHandlerClass = _AmHandler


# ------------------------------------------------------------------ the stack

class Stack:
    def __init__(self, root, disks=1):
        self.root = str(root)
        self.real = bool(TIERFS_BIN)
        self.procs = []
        self.runner = None
        self.files = {}  # key -> dict(disk, share, arr, release, sha256, size, rk, ...)
        self._subvols, self._tmpfs = [], []
        self.disks = [self._make_disk(i + 1) for i in range(disks)]
        for d in self.disks:
            os.makedirs(os.path.join(d, "Media"))
            os.makedirs(os.path.join(d, ".stubs"))
        self.mount = self._dir("tierfs")
        self.state = self._dir("state")
        self.manifests = self._dir("state/manifests")
        self.remote = self._dir("remote")
        self.nzb = self._dir("state/nzb")
        self.events = os.path.join(self.state, "events.jsonl")
        self.db_path = os.path.join(self.state, "tiering.db")
        self.zero_file = os.path.join(self.state, "zeros.json")
        self.log_path = os.path.join(self.state, "runner.log")
        os.environ["FAKE_DAV_ZERO_FILE"] = self.zero_file

        self.dav = fake_webdav.make_server(self.remote, 0, "dav", "davpass")
        self.dav_port = self.dav.server_address[1]
        threading.Thread(target=self.dav.serve_forever, daemon=True).start()
        self.plex = FakePlex()
        self.gate = FakeGate()
        self.alt = FakeAltMount()
        self.indexer = fk.FakeNewznab()
        self.nntp1, self.nntp2 = fk.FakeNntp(()), fk.FakeNntp(())
        self.radarr, self.sonarr = fk.FakeArr(), fk.FakeArr()
        self.http_port = free_port()
        self.tierfs_http = free_port()
        self._emu_stop = threading.Event()
        self.bgsum = ""
        if self.real:
            self._start_tierfs()
        else:
            threading.Thread(target=self._emulate, daemon=True).start()

    # ---- layout

    def _dir(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(p, exist_ok=True)
        return p

    def _make_disk(self, n):
        """Each disk is its own filesystem (own st_dev), as on the array."""
        p = os.path.join(self.root, f"disk{n}")
        r = subprocess.run(["btrfs", "subvolume", "create", p], capture_output=True)
        if r.returncode == 0:
            self._subvols.append(p)
            return p
        os.makedirs(p)
        if ROOT and subprocess.run(["mount", "-t", "tmpfs", "-o", "size=900m", "tmpfs", p],
                                   capture_output=True).returncode == 0:
            self._tmpfs.append(p)
        return p

    @property
    def stub_roots(self):
        return [os.path.join(d, ".stubs") for d in self.disks]

    def env(self, **over):
        e = dict(os.environ)
        e.update({
            "TIERING_DB": self.db_path, "TIERING_APPLY": "1", "TIERING_NEW_DAYS": "0",
            "TIERING_DEMOTE_WINDOW": "00:00-24:00", "TIERING_PASS_SECONDS": "1",
            "TIERING_HTTP": f"127.0.0.1:{self.http_port}",
            "TIERING_PATH_MAP": ",".join(f"{SHARE}={d}/Media" for d in self.disks),
            "TIERING_ARR_PATH_MAP": f"{ARR}={SHARE}",
            "TIERING_GATE_PATH_MAP": ",".join(
                f"{d}=/data/disk{i + 1}" for i, d in enumerate(self.disks)),
            "TIERING_API": self.gate.url,
            "TIERING_PROC_CANARY": open("/proc/self/comm").read().strip(),
            "TIERING_PROMOTE_RETRY_S": "3",
            "TIERING_NZB_DIR": self.nzb,
            "TIERFS_MOUNT": self.mount, "TIERFS_MANIFESTS": self.manifests,
            "TIERFS_STUB_ROOTS": ",".join(self.stub_roots), "TIERFS_EVENTS": self.events,
            "TIERFS_REMOTE_BASE": f"http://127.0.0.1:{self.dav_port}",
            "TIERFS_REMOTE_USER": "dav", "TIERFS_REMOTE_PASS": "davpass",
            "PLEX_URL": self.plex.url, "PLEX_TOKEN": "standin-token",
            "RADARR_URL": self.radarr.url, "RADARR_API_KEY": self.radarr.key,
            "SONARR_URL": self.sonarr.url, "SONARR_API_KEY": self.sonarr.key,
            "ALTMOUNT_URL": self.alt.url, "ALTMOUNT_API_KEY": self.alt.key,
            "ALTMOUNT_MOUNT_PATH": ALT_MOUNT,
            "NEWZNAB_INDEXERS": json.dumps(
                [{"name": "standin", "url": self.indexer.url, "apikey": self.indexer.key}]),
            "NEWZNAB_GRAB_INTERVAL": "0", "NEWZNAB_MIN_INTERVAL_S": "0",
            "NEWZNAB_GRAB_BUDGET": "1000", "NEWZNAB_SEARCH_BUDGET": "1000",
            "NNTP_PRIMARY_HOST": "127.0.0.1", "NNTP_PRIMARY_PORT": str(self.nntp1.port),
            "NNTP_PRIMARY_USER": "nu", "NNTP_PRIMARY_PASS": "np", "NNTP_PRIMARY_TLS": "0",
            "NNTP_SECONDARY_HOST": "127.0.0.1", "NNTP_SECONDARY_PORT": str(self.nntp2.port),
            "NNTP_SECONDARY_USER": "nu", "NNTP_SECONDARY_PASS": "np", "NNTP_SECONDARY_TLS": "0",
            "E2E_UNPRIV": "" if ROOT else "1",
        })
        e.update({k: str(v) for k, v in over.items()})
        return {k: v for k, v in e.items() if v != "" or k == "E2E_UNPRIV"}

    # ---- media

    def add(self, key, rel, *, section="Movies", kind="movie", size=48 * MIB, disk=0,
            show=None, season=None, episode=None, alt=True, added=None, resolution="1080",
            in_arr=True, in_remote=True):
        """One library file with every stand-in told about it. `rel` is the path under
        Media/. `alt` adds a second, different upload (the restore path)."""
        disk_path = os.path.join(self.disks[disk], "Media", rel)
        os.makedirs(os.path.dirname(disk_path), exist_ok=True)
        with open(disk_path, "wb") as fh:
            left = size
            while left:
                b = os.urandom(min(MIB, left))
                fh.write(b)
                left -= len(b)
        old = time.time() - 400 * 86400
        os.utime(disk_path, (old, old))
        release = os.path.splitext(os.path.basename(rel))[0]
        f = {"key": key, "rel": rel, "disk": disk_path, "share": f"{SHARE}/{rel}",
             "arr": f"{ARR}/{rel}", "release": release, "size": size,
             "sha256": sha256_file(disk_path), "mtime_ns": os.lstat(disk_path).st_mtime_ns,
             "ino": os.lstat(disk_path).st_ino, "rk": str(100 + len(self.files)),
             "remote": f"{release}/{os.path.basename(rel)}"}
        self.files[key] = f
        if in_remote:
            os.makedirs(os.path.join(self.remote, release), exist_ok=True)
            shutil.copyfile(disk_path, os.path.join(self.remote, f["remote"]))
        f["ids"] = [f"{release}.p{i}@standin" for i in range(6)]
        f["alt_ids"] = [f"{release}.alt{i}@standin" for i in range(6)]
        self.indexer.releases[f"{key}-exact"] = {"title": release,
                                                 "nzb": fk.make_nzb(f["ids"], release)}
        for n in (self.nntp1, self.nntp2):
            n.ids |= set(f["ids"])
        if alt:
            title = release.rsplit("-", 1)[0] + "-OTHER"
            self.indexer.releases[f"{key}-alt"] = {"title": title,
                                                   "nzb": fk.make_nzb(f["alt_ids"], title)}
            for n in (self.nntp1, self.nntp2):
                n.ids |= set(f["alt_ids"])
        if (section, "movie" if kind == "movie" else "show") not in self.plex.sections:
            self.plex.sections.append((section, "movie" if kind == "movie" else "show"))
        item = {"ratingKey": f["rk"], "type": kind, "title": release, "section": section,
                "files": [f["share"]], "addedAt": int(added or old),
                "videoResolution": resolution}
        if kind == "episode":
            item.update(grandparentRatingKey=str(show), parentIndex=season, index=episode)
        self.plex.items.append(item)
        f["item"] = item
        if in_arr and kind == "movie":
            name, _, year = os.path.basename(os.path.dirname(rel)).rpartition(" (")
            self.radarr.movies.append({"title": name, "year": int(year.rstrip(")")), "movieFile": {
                "path": f["arr"], "size": size, "sceneName": release}})
        elif in_arr:
            if not self.sonarr.series:
                self.sonarr.series.append({"id": 1, "title": "Show Name", "tvdbId": 1})
            self.sonarr.episodes.setdefault(1, []).append({
                "seasonNumber": season, "episodeNumber": episode,
                "episodeFile": {"path": f["arr"], "size": size, "sceneName": release}})
        return f

    def add_season(self, season, n=4, prefix="Show.Name", **kw):
        return [self.add(f"s{season}e{e}",
                         f"TV/Show Name/Season {season:02d}/{prefix}.S{season:02d}E{e:02d}."
                         f"1080p.WEB-GRP.mkv", section=kw.pop("section", "TV"), kind="episode",
                         show=9, season=season, episode=e, **dict(kw))
                for e in range(1, n + 1)]

    # ---- runner

    def start(self, kill_at="", **over):
        assert self.runner is None or self.runner.poll() is not None, "runner already running"
        self.http_port = free_port()  # a fresh one each start: the last may be taken by now
        env = self.env(E2E_KILL_AT=kill_at, **over)
        log = open(self.log_path, "ab")
        self.runner = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "runner_shim.py")] + (
                ["--once"] if over.get("E2E_ONCE") else []),
            cwd=os.path.join(REPO, "tiering"), env=env, stdout=log, stderr=log)
        self.procs.append(self.runner)
        return self.runner

    def stop(self, sig=signal.SIGTERM):
        p = self.runner
        if p is not None and p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(10)

    def wait_killed(self, timeout=90):
        """Wait for the runner to die at its kill point (SIGKILL on itself)."""
        try:
            rc = self.runner.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("the runner never reached the kill point\n" + self.log_tail())
        assert rc == -signal.SIGKILL, f"runner exited {rc}, not by SIGKILL\n{self.log_tail()}"

    def log_tail(self, n=40):
        out = ""
        for name, path in (("runner", self.log_path),
                           ("tierfs", os.path.join(self.state, "tierfs.log"))):
            try:
                with open(path, errors="replace") as fh:
                    lines = fh.readlines()[-n:]
            except OSError:
                continue
            if lines:
                out += f"--- {name} log\n" + "".join(lines)
        return out

    def log(self):
        try:
            with open(self.log_path, errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    # ---- state

    def rows(self):
        if not os.path.exists(self.db_path):
            return []
        db = st.open_db(self.db_path)
        try:
            return st.list_files(db)
        finally:
            db.close()

    def row(self, f):
        for r in self.rows():
            if r["lib_path"] == f["share"]:
                return r
        return None

    def db_events(self, f=None):
        db = st.open_db(self.db_path)
        try:
            return st.events(db, self.row(f)["id"] if f else None)
        finally:
            db.close()

    def state_of(self, f):
        r = self.row(f)
        return r["state"] if r else None

    def wait_state(self, fs, state, timeout=90):
        fs = fs if isinstance(fs, list) else [fs]
        try:
            wait_until(lambda: all(self.state_of(f) == state for f in fs), timeout,
                       what=f"state {state}")
        except AssertionError as e:
            got = {f["key"]: self.state_of(f) for f in fs}
            raise AssertionError(f"{e}; states {got}\n{self.log_tail()}") from None

    def alerts(self):
        p = os.path.join(self.state, "alerts.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def emit(self, ev, f, offset=None, ts=None):
        """Write a tierfs event (emulator mode: the test is the player)."""
        rec = {"ts": ts or time.time(), "id": self.row(f)["id"], "ev": ev,
               "bytes_local": 0, "bytes_remote": 0}
        if offset is not None:
            rec["offset"] = offset
        with open(self.events, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def tierfs_events(self, ev=None):
        if not os.path.exists(self.events):
            return []
        with open(self.events) as fh:
            out = [json.loads(x) for x in fh if x.strip()]
        return [e for e in out if ev is None or e["ev"] == ev]

    def zero(self, f, start, end):
        """The remote answers [start, end) of this file with zeros (dead articles)."""
        with open(self.zero_file, "w") as fh:
            json.dump({f["remote"]: [[start, end]]}, fh)

    # ---- what a reader sees

    def manifest(self, f):
        r = self.row(f)
        p = os.path.join(self.manifests, f"{r['id']}.json") if r else ""
        if not p or not os.path.exists(p):
            return None
        with open(p) as fh:
            return json.load(fh)

    def filled(self, id):
        out, failed = set(), False
        try:
            with open(os.path.join(self.manifests, f"{id}.fill")) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("failed"):
                        failed = True
                    elif line.isdigit():
                        out.add(int(line))
        except OSError:
            pass
        return out, failed

    def read_through(self, f, timeout=120) -> str:
        """sha256 of the bytes a background reader gets through L. Raises OSError when
        the path is dangling or the read fails."""
        lib = f["disk"]
        if not os.path.islink(lib):
            return sha256_file(lib)
        if self.real:
            r = subprocess.run([self.bgsum, lib], capture_output=True, timeout=timeout)
            if r.returncode != 0:
                raise OSError(r.stderr.decode(errors="replace").strip())
            return r.stdout.split()[0].decode()
        target = os.readlink(lib)
        assert os.path.dirname(target) == self.mount, f"L points outside the mount: {target}"
        stub = os.path.realpath(target)  # the emulator's entry -> the stub
        if not os.path.lexists(target):
            raise OSError(f"dangling: tierfs lists no {os.path.basename(target)}")
        mpath = os.path.join(self.manifests,
                             os.path.splitext(os.path.basename(target))[0] + ".json")
        if not os.path.exists(mpath):
            return sha256_file(stub)  # tierfs with no manifest would have dropped the entry
        with open(mpath) as fh:
            man = json.load(fh)
        got, _ = self.filled(man["id"])
        h = hashlib.sha256()
        size, head, tail = man["size"], man["head_len"], man["tail_len"]
        with open(stub, "rb") as s, open(os.path.join(self.remote, f["remote"]), "rb") as r:
            pos = 0
            while pos < size:
                n = min(MIB, size - pos)
                local = pos < head or pos >= size - tail or \
                    (head + (pos - head) // CHUNK * CHUNK) in got
                src = s if local else r
                src.seek(pos)
                b = src.read(n)
                if len(b) != n:
                    raise OSError(f"short read at {pos}")
                h.update(b)
                pos += n
        return h.hexdigest()

    def player_read(self, f, timeout=60) -> str:
        """A player reads the whole file through L. Real tierfs: a plain read (tierfs
        waits for the promote). Emulator: emit `want`, then wait like tierfs does:
        EIO on a `failed` fill line or when nothing arrives in time."""
        lib = f["disk"]
        if self.real or not os.path.islink(lib):
            r = subprocess.run(["sha256sum", lib], capture_output=True, timeout=timeout + 30)
            if r.returncode != 0:
                raise OSError(r.stderr.decode(errors="replace").strip())
            return r.stdout.split()[0].decode()
        row = self.row(f)
        self.emit("want", f, offset=row["head_len"])
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not os.path.islink(lib):
                return sha256_file(lib)
            if self.filled(row["id"])[1]:
                raise OSError("EIO: fill failed")
            time.sleep(0.1)
        raise OSError("EIO: wait timed out")

    def stub_of(self, f):
        r = self.row(f)
        return r.get("stub_path") if r else None

    def leftovers(self):
        """Half-state: swap temp files, and stubs or manifests with no offloaded row."""
        out = []
        for d in self.disks:
            for dp, _dn, fn in os.walk(os.path.join(d, "Media")):
                out += [os.path.join(dp, n) for n in fn if n.endswith(".tierfs-tmp")]
        live = {r["id"] for r in self.rows() if r["state"] != "whole"}
        for d in self.stub_roots + [self.manifests]:
            for n in os.listdir(d):
                if n.split(".")[0] not in live:
                    out.append(os.path.join(d, n))
        return out

    def assert_intact(self, fs=None):
        """The shared assertions: every L resolves and reads back as the original."""
        for f in (fs or self.files.values()):
            assert os.path.lexists(f["disk"]), f"{f['key']}: L is missing"
            got = self.read_through(f)
            assert got == f["sha256"], (f"{f['key']}: content changed ({self.state_of(f)}): got {got}\n"
                                        + self.why_changed(f))
            assert os.stat(f["disk"]).st_size == f["size"], f"{f['key']}: size changed"

    def why_changed(self, f) -> str:
        """Where the bytes read through L first differ from the remote copy."""
        if not self.real:
            return ""
        cmp = os.path.join(self.root, "bgcmp")
        if not os.path.exists(cmp):
            shutil.copy(shutil.which("cmp"), cmp)
        r = subprocess.run([cmp, f["disk"], os.path.join(self.remote, f["remote"])],
                           capture_output=True, timeout=120)
        row = self.row(f) or {}
        return (f"cmp: {r.stdout.decode(errors='replace').strip()} "
                f"{r.stderr.decode(errors='replace').strip()}\n"
                f"head_len={row.get('head_len')} tail_len={row.get('tail_len')} "
                f"size={row.get('size')} remote_path={row.get('remote_path')}\n" + self.log_tail(15))

    # ---- tierfs

    def _emulate(self):
        while not self._emu_stop.wait(0.05):
            try:
                want = {}
                for n in os.listdir(self.manifests):
                    if n.endswith(".json"):
                        try:
                            with open(os.path.join(self.manifests, n)) as fh:
                                m = json.load(fh)
                            want[f"{m['id']}{m['ext']}"] = m["stub_path"]
                        except (OSError, ValueError, KeyError):
                            continue
                have = set(os.listdir(self.mount))
                for n in have - set(want):
                    os.unlink(os.path.join(self.mount, n))
                for n, stub in want.items():
                    if n not in have and os.path.exists(stub):
                        os.symlink(stub, os.path.join(self.mount, n))
            except OSError:
                pass

    def _start_tierfs(self):
        self.bgsum = os.path.join(self.root, "bgsum")
        # A script's process name is the script's file name, which is what tierfs
        # reads. (A renamed copy of sha256sum will not do: where the coreutils are
        # one binary, the name picks the tool.)
        with open(self.bgsum, "w") as fh:
            fh.write(f"#!{sys.executable}\nimport hashlib, sys\nh = hashlib.sha256()\n"
                     "with open(sys.argv[1], 'rb') as f:\n"
                     "    for b in iter(lambda: f.read(1 << 20), b''):\n"
                     "        h.update(b)\nprint(h.hexdigest())\n")
        os.chmod(self.bgsum, 0o755)
        env = self.env(TIERFS_POLL="500ms", TIERFS_WAIT_S=os.environ.get("E2E_WAIT_S", "30"),
                       TIERFS_HTTP=f"127.0.0.1:{self.tierfs_http}", TIERFS_MAX_STREAMS="4",
                       TIERFS_BACKGROUND_COMMS="Plex Media Scan,bgsum,bgcmp")
        log = open(os.path.join(self.state, "tierfs.log"), "ab")
        self.tierfs = subprocess.Popen([TIERFS_BIN], env=env, stdout=log, stderr=log)
        self.procs.append(self.tierfs)
        wait_until(lambda: os.path.ismount(self.mount), 20, what="the tierfs mount")

    def close(self):
        self.stop(signal.SIGKILL)
        self._emu_stop.set()
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self.real:
            subprocess.run(["fusermount3", "-u", "-z", self.mount], capture_output=True)
        self.dav.shutdown()
        self.dav.server_close()
        for s in (self.plex, self.gate, self.alt, self.indexer, self.nntp1, self.nntp2,
                  self.radarr, self.sonarr):
            s.close()
        for p in self._tmpfs:
            subprocess.run(["umount", "-l", p], capture_output=True)
        for p in self._subvols:
            subprocess.run(["btrfs", "subvolume", "delete", p], capture_output=True)
