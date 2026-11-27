"""The offload runner: one long-running process that applies the hot set.

See tiering/OFFLOAD_DESIGN.md, "Runner". Every pass reads Plex and the tierfs
events, computes the hot set, then promotes what is wanted at once and demotes
what is idle, only inside the night window.

  python -m offload.runner --once     one pass, then exit (cron, testing)
  python -m offload.runner            loop, one pass every 60 s

Dry run unless TIERING_APPLY=1: every planned action is logged and nothing
that changes a file is called.

state.py, ops.py and qualify.py are not imported at module level. They are
reached through a `Deps` object (see `load_deps`), so tests can inject fakes.
"""
from __future__ import annotations

import collections
import fcntl
import importlib
import inspect
import json
import logging
import os
import signal
import stat as stat_mod
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import hotset, paths, plex_watch

log = logging.getLogger("tiering.runner")

DAY = 86400.0
GB = 10**9
PASS_INTERVAL = 60.0
RETRY_DELAY = 300.0
ACTIVE_STATES = ("offloaded", "promoting", "offloading")  # rows a promote can act on
STUCK_AFTER = 3 * 3600.0  # a promote running this long is reported as stuck
REGISTER_LEAD = 3600.0  # register this long before the demote window opens
PROTECTED_COLUMNS = ("id", "state", "created_ts", "updated_ts", "lib_path")
REGISTER_BAD_FRAC = 0.05  # missing_local + ambiguous over this share of listed fails register (on_pool is not counted)
ALERT_EVENTS = ("promote_short_remote", "takeover_orphan")  # ops events, alerted once per id
ONCE_KINDS = ALERT_EVENTS + ("l_missing", "promote_stuck", "lost_file")  # alerted once per id, ever
BACKOFF_MAX = 6 * 3600.0  # promote retry delay: RETRY_DELAY doubling, capped here
FAIL_ALERT_AFTER = 3  # failures in a row per id before an alert
REGISTER_KEYS = ("listed", "missing_local", "ambiguous", "errors")  # required in the result
HISTORY_DROP = 0.20  # a Plex history this much shorter than last time is not trusted
HISTORY_EDGE = 120.0  # plays this close to the window edge are not counted as expected
CHECK_EVERY_H = 24.0  # the integrity check runs nightly: when the last start is this old (TIERING_CHECK_EVERY_H)
CHECK_MAX_H = 4.0  # ... and stops starting rows after this long, to go on the next night (TIERING_CHECK_MAX_H)
REDOWNLOAD_MAX = 3  # lost rows acted on per night (TIERING_REDOWNLOAD_MAX_PER_NIGHT)
WEEKLY_RETRY = 3600.0  # after a failed weekly check, wait this long before trying again
FOLLOW_POLL = 0.25  # the tierfs event file is read this often (a want starts a promote within 1 s)
WANT_SPACING = 60.0  # wanted attempts on one id are at least this far apart
WATCHED_TTL = 600.0  # a file counts as watched this long after its last want or live-session sighting
YIELD_POLL = 0.5  # a paused promote or demote looks this often for the watched promotes to end
HEAD_FRAC = 0.20  # the head kept on disk is at least this share of the file ...
HEAD_MIN_S = 1200.0  # ... and at least this much play time (TIERING_HEAD_MIN_S)
MIN_PLAY_S = 1800.0  # a file with less play time than this stays whole (TIERING_MIN_PLAY_S)
MIN_OFFLOAD_FRAC = 0.20  # a file whose middle would be under this share of it stays whole
ATIME_EVERY = 600.0  # whole files are checked for a read this often (a stat of each)
ATIME_SLACK = 1.0  # an access time this much newer than the one we noted is a read
# Plex reads a new or replaced file for its own analysis (previews, intro and credits
# markers, loudness) soon after it lands. A read this soon after the file's modification
# time or its first registration is that analysis, not a person.
IMPORT_GRACE = 3 * DAY
WANT_FRESH = 900.0  # a want older than this is not a player waiting any more (TIERFS_WAIT_S)
# Used only when the state module does not export COLUMNS.
FALLBACK_COLUMNS = ("sha256", "nzb_path", "nzb_path2", "remote_path", "restore_score",
                    "restore_note", "last_check_ts", "last_stat_ok_ts")


# --------------------------------------------------------------------------
# config


def parse_gate_map(spec: str) -> list[tuple[str, str]]:
    """TIERING_GATE_PATH_MAP: `disk_prefix=deluge_prefix` entries, comma separated.
    Several disk prefixes may map to one Deluge prefix."""
    return paths.parse_path_map(spec)


def gate_path(path: str, pairs):
    """A disk path in Deluge's namespace, or None when no entry covers it. An unmapped
    path must never reach the gate: it would match no torrent and read as free."""
    for disk, deluge in pairs:
        if path == disk or path.startswith(disk + "/"):
            return deluge + path[len(disk):]
    return None


def _covered(disk: str, pairs) -> bool:
    return any(disk == d or disk.startswith(d + "/") for d, _ in pairs)


def parse_window(spec: str) -> tuple[int, int]:
    """"HH:MM-HH:MM" -> (start, end) in minutes after local midnight."""
    try:
        a, b = spec.split("-")
        out = []
        for t in (a, b):
            h, m = t.strip().split(":")
            h, m = int(h), int(m)
            if not ((0 <= h < 24 and 0 <= m < 60) or (h == 24 and m == 0)):
                raise ValueError
            out.append(h * 60 + m)
    except ValueError:
        raise ValueError(f"bad TIERING_DEMOTE_WINDOW {spec!r}; expected HH:MM-HH:MM") from None
    if out[0] == out[1]:
        raise ValueError("TIERING_DEMOTE_WINDOW start and end are equal")
    return out[0], out[1]


def night_start(now: float, window: tuple[int, int]) -> float | None:
    """Start (epoch) of the local night `now` is in, or None outside the window.
    Handles windows that cross midnight."""
    s, e = window
    dt = datetime.fromtimestamp(now)
    mins = dt.hour * 60 + dt.minute
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if s < e:
        if not (s <= mins < e):
            return None
        return (day + timedelta(minutes=s)).timestamp()
    if mins >= s:
        return (day + timedelta(minutes=s)).timestamp()
    if mins < e:
        return (day - timedelta(days=1) + timedelta(minutes=s)).timestamp()
    return None


def next_night_start(now: float, window: tuple[int, int]) -> float:
    """Start of the night `now` is in, or of the next one."""
    cur = night_start(now, window)
    if cur is not None:
        return cur
    day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    t = (day + timedelta(minutes=window[0])).timestamp()
    if t <= now:
        t = (day + timedelta(days=1, minutes=window[0])).timestamp()
    return t


def _norm_only(p: str) -> str:
    """One allowlist or row path, compared exactly: stripped, no trailing slash."""
    p = p.strip()
    return p.rstrip("/") if len(p) > 1 else p


def parse_only_paths(spec) -> frozenset:
    """TIERING_ONLY_PATHS: library paths (as stored, share paths) that may be offloaded.
    Comma or newline separated; `@file` names a file with one path per line. `#` lines
    and blanks are ignored. Empty means no allowlist. Errors never name a path."""
    spec = (spec or "").strip()
    if not spec:
        return frozenset()
    if spec.startswith("@"):
        try:
            with open(spec[1:].strip(), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            raise ValueError(f"TIERING_ONLY_PATHS file cannot be read ({type(e).__name__})")
        out = {_norm_only(x) for x in lines if x.strip() and not x.strip().startswith("#")}
        if not out:  # an empty list must not silently widen the allowlist to everything
            raise ValueError("TIERING_ONLY_PATHS file holds no paths")
        return frozenset(out)
    parts = [x for line in spec.splitlines() for x in line.split(",")]
    out = frozenset(_norm_only(x) for x in parts if x.strip() and not x.strip().startswith("#"))
    if not out:  # set but only commas or comments: never widen to "every file"
        raise ValueError("TIERING_ONLY_PATHS is set but holds no paths")
    return out


def _flag(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    db: str
    tierfs_mount: str = ""
    manifests: str = ""
    events: str = ""
    stub_roots: str = ""
    apply: bool = False
    promote_concurrency: int = 2
    window: tuple = (120, 360)
    demote_bytes_per_night: int = 200 * GB
    path_map: list = field(default_factory=list)
    gate_path_map: list = field(default_factory=list)
    alerts: str = ""
    http: str = "127.0.0.1:8471"
    pass_seconds: float = PASS_INTERVAL
    hot: hotset.Cfg = field(default_factory=hotset.Cfg)
    head_min_s: float = HEAD_MIN_S
    min_play_s: float = MIN_PLAY_S
    only_paths: frozenset = frozenset()  # empty: no allowlist; else only these may be demoted
    orphan_grace_days: float = 7.0  # 0 turns the orphan stub cleanup off
    check_every_h: float = CHECK_EVERY_H
    check_max_h: float = CHECK_MAX_H
    auto_redownload: bool = True  # TIERING_AUTO_REDOWNLOAD (only acts with apply)
    redownload_max: int = REDOWNLOAD_MAX

    @classmethod
    def from_env(cls, env=None) -> "Config":
        e = os.environ if env is None else env
        db = e.get("TIERING_DB", "")
        if not db:
            raise ValueError("TIERING_DB must be set")
        d = hotset.Cfg()
        hot = hotset.Cfg(
            new_days=float(e.get("TIERING_NEW_DAYS") or d.new_days),
            idle_days=float(e.get("TIERING_IDLE_DAYS") or d.idle_days),
            near_end_eps=int(e.get("TIERING_NEAR_END_EPS") or d.near_end_eps),
            max_stream_kbps=int(e.get("TIERING_MAX_STREAM_KBPS") or 0),
            keep_4k=_flag(e.get("TIERING_KEEP_4K")),
            never_sections=tuple(s.strip() for s in
                                 (e.get("TIERING_NEVER_SECTIONS", "Adult")).split(",") if s.strip()),
        )
        gate_map = parse_gate_map(e.get("TIERING_GATE_PATH_MAP", ""))
        apply = e.get("TIERING_APPLY") == "1"
        if apply and not gate_map:
            raise ValueError(
                "TIERING_APPLY=1 needs TIERING_GATE_PATH_MAP (runner paths to the torrent "
                "client's /data paths). Without it the glacier gate reads every file as free")
        stub_roots = e.get("TIERFS_STUB_ROOTS", "")
        if apply and not [r for r in stub_roots.split(",") if r.strip()]:
            raise ValueError(
                "TIERING_APPLY=1 needs TIERFS_STUB_ROOTS (one stub directory per disk; ops "
                "picks the one on the same disk as the file)")
        path_map = paths.parse_path_map(e.get("TIERING_PATH_MAP", ""))
        if apply:
            for var in ("TIERFS_EVENTS", "TIERFS_MOUNT", "TIERFS_MANIFESTS"):
                if not e.get(var):
                    raise ValueError(f"TIERING_APPLY=1 needs {var}")
            evdir = os.path.dirname(os.path.abspath(e["TIERFS_EVENTS"]))
            if not os.path.isdir(evdir):
                raise ValueError(f"TIERFS_EVENTS directory {evdir} does not exist")
            for _share, disk in path_map:
                if not _covered(disk, gate_map):
                    raise ValueError(
                        f"TIERING_GATE_PATH_MAP does not cover the disk prefix {disk} of "
                        "TIERING_PATH_MAP; the gate would never see its files")
        return cls(
            db=db,
            tierfs_mount=os.path.normpath(e["TIERFS_MOUNT"]) if e.get("TIERFS_MOUNT") else "",
            manifests=e.get("TIERFS_MANIFESTS", ""),
            events=e.get("TIERFS_EVENTS", ""),
            stub_roots=stub_roots,
            apply=apply,
            promote_concurrency=max(1, int(e.get("TIERING_PROMOTE_CONCURRENCY") or 2)),
            window=parse_window(e.get("TIERING_DEMOTE_WINDOW") or "02:00-06:00"),
            demote_bytes_per_night=int(float(e.get("TIERING_DEMOTE_GB_PER_NIGHT") or 200) * GB),
            path_map=path_map,
            gate_path_map=gate_map,
            alerts=e.get("TIERING_ALERTS") or os.path.join(
                os.path.dirname(os.path.abspath(db)), "alerts.jsonl"),
            http=e.get("TIERING_HTTP") or "127.0.0.1:8471",
            pass_seconds=float(e.get("TIERING_PASS_SECONDS") or PASS_INTERVAL),
            hot=hot,
            head_min_s=max(0.0, float(e.get("TIERING_HEAD_MIN_S") or HEAD_MIN_S)),
            min_play_s=max(0.0, float(e.get("TIERING_MIN_PLAY_S") or MIN_PLAY_S)),
            only_paths=parse_only_paths(e.get("TIERING_ONLY_PATHS")),
            orphan_grace_days=max(0.0, float(
                7.0 if e.get("TIERING_ORPHAN_GRACE_DAYS") in (None, "")
                else e["TIERING_ORPHAN_GRACE_DAYS"])),
            check_every_h=max(0.1, float(e.get("TIERING_CHECK_EVERY_H") or CHECK_EVERY_H)),
            check_max_h=max(0.01, float(e.get("TIERING_CHECK_MAX_H") or CHECK_MAX_H)),
            auto_redownload=e.get("TIERING_AUTO_REDOWNLOAD", "1").strip().lower()
            not in ("0", "false", "no", "off"),
            redownload_max=max(1, int(e.get("TIERING_REDOWNLOAD_MAX_PER_NIGHT") or REDOWNLOAD_MAX)),
        )


# --------------------------------------------------------------------------
# logging: never let the Plex token reach a log line


class Redact(logging.Filter):
    def filter(self, record):
        tok = os.environ.get("PLEX_TOKEN", "")
        if not tok:
            return True
        msg = record.getMessage()
        if record.exc_info:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
        if tok in msg:
            record.msg, record.args = msg.replace(tok, "***"), ()
        if record.exc_text and tok in record.exc_text:
            record.exc_text = record.exc_text.replace(tok, "***")
        return True


log.addFilter(Redact())


# --------------------------------------------------------------------------
# dependencies (state, ops, qualify, glacier gate)


@dataclass
class Deps:
    """state: module-like with open_db/get/set_state/upsert/list_files/log_event.
    promote(db, row, apply) and demote(db, row, apply) return an ops `Result`.
    qualify(row) returns an object with .ok, .reasons and .fields.
    gate(lib_path) returns "free", "pause" or "wait"; only "free" may be demoted."""
    state: object
    promote: Callable
    demote: Callable
    qualify: Callable
    gate: Callable
    register: Callable | None = None  # register(db, apply) -> dict, once a night
    weekly: Callable | None = None  # weekly(db) runs weekly.run_weekly; flags come as events
    # arrs() -> redownload.Arrs (built on first use) and altmount_ok() -> bool; None: no
    # automatic re-download
    arrs: Callable | None = None
    altmount_ok: Callable | None = None
    # open_holders(path) -> list of pids holding the file open (tierfs included); a -1 entry
    # means "not clear". None: the orphan cleanup cannot tell, so it keeps every stub.
    open_holders: Callable | None = None


def _http_gate(cfg: Config):
    """Glacier gate over the tiering API (TIERING_API). Anything but an
    explicit "free" is "wait". It gets the disk path, mapped into Deluge's
    namespace by TIERING_GATE_PATH_MAP="disk_prefix=deluge_prefix,..."."""
    import urllib.request
    api = os.environ.get("TIERING_API", "").rstrip("/")
    pairs = cfg.gate_path_map

    def gate(path):
        if not api:
            return "wait"
        mapped = gate_path(path, pairs)
        if mapped is None:
            log.warning("gate: %s is under no TIERING_GATE_PATH_MAP entry; not free", path)
            return "wait"
        try:
            req = urllib.request.Request(
                api + "/tiering/torrent-gate?format=tsv",
                data=mapped.encode(), method="POST",
                headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=30) as r:
                first = r.read().decode("utf-8", "replace").splitlines()
            return first[0].split("\t")[0].strip() if first else "wait"
        except (OSError, ValueError):
            return "wait"
    return gate


def _takes(fn, name) -> bool:
    """True when `fn` accepts the keyword argument `name`."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _accepts(fn, name) -> bool:
    """Like _takes, and also true when `fn` has a ``**kwargs``."""
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ps.values())


def _arrs_from_env(cfg: Config):
    """``arrs()``: the Radarr/Sonarr clients for re-downloads, built on first use (a dry run
    or a missing arr setting never stops the start). None when no arr URL is set."""
    cache: list = []

    def arrs():
        if not cache:
            from . import redownload, register
            radarrs, sonarrs = register.clients_from_env()
            if not radarrs and not sonarrs:
                cache.append(None)
            else:
                cache.append(redownload.Arrs(
                    radarrs, sonarrs, paths.parse_path_map(os.environ.get("TIERING_ARR_PATH_MAP")),
                    paths.parse_path_map(os.environ.get("TIERING_PATH_MAP"))))
        return cache[0]
    return arrs


def load_deps(cfg: Config) -> Deps:
    """Wire the real modules. Imported here so the runner imports without them."""
    from . import altmount, ops
    from . import state as st

    reader = altmount.WebDavReader()

    def promote(db, row, apply, want=None, urgent=None, pause=None):
        kw = {k: v for k, v in (("want", want), ("urgent", urgent), ("pause", pause))
              if v is not None and _takes(ops.promote, k)}
        return ops.promote(db, row["id"], reader, apply=apply, manifest_dir=cfg.manifests,
                           path_map=cfg.path_map, **kw)

    def demote(db, row, apply, head_frac=None):
        # lib_path stays the share path; ops resolves the disk path and picks the stub
        # root from TIERFS_STUB_ROOTS by st_dev.
        kw = {"head_frac": head_frac} if head_frac is not None else {}
        return ops.demote(db, row, manifest_dir=cfg.manifests, tierfs_mount=cfg.tierfs_mount,
                          apply=apply, path_map=cfg.path_map, **kw)

    factory = os.environ.get("TIERING_QUALIFY_FACTORY") or \
        f"{__package__}.wiring:qualify_from_env"
    mod, _, fn = factory.partition(":")
    qualify = getattr(importlib.import_module(mod), fn)()
    weekly = None
    if not os.environ.get("TIERING_QUALIFY_FACTORY"):  # a custom factory has its own clients
        wiring = importlib.import_module(f"{__package__}.wiring")
        weekly = wiring.weekly_from_env()
        if cfg.apply:
            # The clients are built on first use, so a dry run starts without Usenet set up.
            # APPLY builds them now: a missing setting stops the start, not the first demote.
            wiring.check()

    def register(db, apply):
        return importlib.import_module(f"{__package__}.register").register(db, apply)

    def open_holders(path):
        # ignore_comm=(): a tierfs reader holding the stub counts as a holder too
        return ops.scan_open_holders(path, ignore_comm=())

    arrs = altmount_ok = None
    if not os.environ.get("TIERING_QUALIFY_FACTORY"):
        arrs = _arrs_from_env(cfg)
        altmount_ok = wiring.altmount_ok_from_env()
    return Deps(st, promote, demote, qualify, _http_gate(cfg), register, weekly, open_holders,
                arrs, altmount_ok)


# --------------------------------------------------------------------------
# tierfs bad_chunk and slot_timeout events


class BadChunkScanner:
    """Tails the tierfs event file (and rotated copies on the first look or
    after a rotation) for `bad_chunk`, `slot_timeout` and `want` events."""

    def __init__(self, path):
        self.path, self.ino, self.pos = path, None, 0

    @staticmethod
    def _read(p, start):
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            return [], start
        cut = data.rfind(b"\n") + 1
        out = []
        for line in data[:cut].splitlines():
            try:
                e = json.loads(line)
                if e.get("ev") in ("bad_chunk", "slot_timeout", "want") and e.get("id"):
                    out.append({"ev": e["ev"], "id": str(e["id"]),
                                "ts": float(e.get("ts") or 0), "offset": e.get("offset")})
            except (ValueError, TypeError, AttributeError):
                continue
        return out, start + cut

    def scan(self):
        if not self.path:
            return []
        try:
            s = os.stat(self.path)
        except OSError:
            return []
        out = []
        if self.ino != s.st_ino or s.st_size < self.pos:
            for i in (3, 2, 1):
                out += self._read(f"{self.path}.{i}", 0)[0]
            self.ino, self.pos = s.st_ino, 0
        ev, self.pos = self._read(self.path, self.pos)
        return out + ev


# --------------------------------------------------------------------------
# the runner


class Runner:
    def __init__(self, cfg: Config, deps: Deps, clock=time.time, sleep=time.sleep):
        self.cfg, self.deps, self.clock, self.sleep = cfg, deps, clock, sleep
        self.state = deps.state
        self.stop = threading.Event()
        self.mu = threading.Lock()
        self.promote_todo: deque = deque()
        self.demote_todo: deque = deque()
        self.promote_running: set = set()
        self.promote_workers = 0
        self.demote_worker = False
        self.threads: list = []
        self._rows: dict = {}
        self.retry_after: dict = {}
        self.desired_rks: set = set()
        self.complete = False
        self.pending_bad: dict = {}
        self.slot_alerted: set = set()  # (night start, id)
        self.ops_alerted: set = set()  # (kind, id), once per id, ever
        self.orphan_logged: set = set()  # (id, what): orphan cleanup notes, logged once per id
        self._seed_alerted()
        self.demote_running: set = set()
        self.fails: dict = {}  # id -> failures in a row
        self.worker_deaths = 0
        self.promote_started: dict = {}  # id -> start time of a running promote
        self.flagged: set = set()  # ids a weekly_flag event asked to promote
        self.ops_seq = 0  # last events row read for alerts
        self.flag_seq = 0  # last events row read for weekly flags
        self.want_last: dict = {}  # id -> start of the last wanted attempt (kept in kv)
        self.want_off: dict = {}  # id -> newest offset a player waits at
        self.watched: dict = {}  # id -> last want or live-session sighting: its promote goes first, wide
        self.durations: dict = {}  # id -> play time in ms, from Plex (sets the head)
        self.tail_events: list = []  # bad_chunk and slot_timeout events for the pass
        self.follow_mu = threading.Lock()
        self.weekly_last = 0.0
        self.weekly_running = False
        self.weekly_tried = False
        self.weekly_retry_at = 0.0
        self._redl = None  # redownload.Redownload, built on first use
        self.lost_hold: set = set()  # ids waiting for a re-download: no promote starts for them
        self.history_ok = True
        self.atime_last = None  # when whole files were last checked for a read
        self.atime_mu = threading.Lock()
        self.reading: set = set()  # ids this runner is reading itself (qualify, offload)
        self.own_read: set = set()  # of those, the ones whose file was really read
        self.read_failed: set = set()
        self.history_alerted: set = set()  # night starts
        self._loaded = False
        self.slot_timeouts = 0  # seen this pass
        self.register_state = {"start": None, "ok": False}
        self.skipped_paths: set = set()
        self.disk: dict = {}
        self.scanner = BadChunkScanner(cfg.events)
        self._db = None
        self.night = self._load_night()
        self.last_pass = None
        self.last_ok = None
        self.passes = 0
        self.errors = 0
        self.counts: dict = {}
        self.heartbeat_path = cfg.db + ".heartbeat"

    # ---- small helpers

    def _may_demote(self, row) -> bool:
        """The TIERING_ONLY_PATHS allowlist; only ever asked about demotes."""
        only = self.cfg.only_paths
        return not only or _norm_only(str(row.get("lib_path") or "")) in only

    def announce_only_paths(self):
        """Once at start: the allowlist's size, and how many of its paths match no row."""
        only = self.cfg.only_paths
        if not only:
            return
        log.info("TIERING_ONLY_PATHS: only %d path(s) may be offloaded", len(only))
        try:
            known = {_norm_only(str(r.get("lib_path") or ""))
                     for r in self.state.list_files(self.db())}
        except Exception as e:
            log.warning("TIERING_ONLY_PATHS: rows not readable (%s)", type(e).__name__)
            return
        missing = len(only - known)
        if missing:
            log.warning("TIERING_ONLY_PATHS: %d path(s) match no row", missing)

    def _open(self):
        return self.state.open_db(self.cfg.db)

    def db(self):
        if self._db is None:
            self._db = self._open()
        return self._db

    def _night_file(self):
        return self.cfg.db + ".night.json"

    def _load_night(self):
        try:
            with open(self._night_file()) as fh:
                d = json.load(fh)
            return {"start": float(d["start"]), "bytes": int(d["bytes"]),
                    "failed": bool(d["failed"])}
        except (OSError, ValueError, KeyError, TypeError):
            return {"start": None, "bytes": 0, "failed": False}

    def _save_night(self):
        if not self.cfg.apply:
            return
        tmp = f"{self._night_file()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.night, fh)
        os.replace(tmp, self._night_file())

    def _tonight(self, now):
        """The night record for now (reset when a new night begins), or None."""
        start = night_start(now, self.cfg.window)
        if start is None:
            return None
        with self.mu:
            if self.night["start"] != start:
                self.night = {"start": start, "bytes": 0, "failed": False}
                self._save_night()
            return self.night

    def _alert(self, kind, id, **detail):
        rec = {"ts": self.clock(), "kind": kind, "id": id, **detail}
        log.error("ALERT %s id=%s %s", kind, id, json.dumps(detail, sort_keys=True, default=str))
        try:
            with open(self.cfg.alerts, "a") as fh:
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        except OSError as e:
            log.error("cannot write alerts file: %s", type(e).__name__)

    def _note_failure(self, now):
        n = self._tonight(now)
        if n is not None:
            with self.mu:
                n["failed"] = True
                self._save_night()

    def _kv_get(self, db, key, default=None):
        f = getattr(self.state, "kv_get", None)
        try:
            return f(db, key, default) if f else default
        except Exception as e:
            log.error("kv read %s failed: %s", key, type(e).__name__)
            return default

    def _kv_set(self, db, key, value):
        f = getattr(self.state, "kv_set", None)
        if f is None or not self.cfg.apply:
            return
        try:
            f(db, key, value)
        except Exception as e:
            log.error("kv write %s failed: %s", key, type(e).__name__)

    def _load_persisted(self, db):
        """State that must survive a restart: the slot_timeout de-dup, the events cursor
        and the weekly flags still waiting for a promote."""
        if self._loaded:
            return
        self._loaded = True
        self.slot_alerted = {(float(n), str(i)) for n, i in self._kv_get(db, "slot_alerted", [])}
        self.ops_seq = int(self._kv_get(db, "ops_seq", 0) or 0)
        self.flag_seq = int(self._kv_get(db, "weekly_flag_cursor", 0) or 0)
        self.weekly_last = float(self._kv_get(db, "weekly_last_ts", 0) or 0)
        saved = self._kv_get(db, "want_last", {}) or {}
        with self.mu:
            self.want_last = {**{str(k): float(v) for k, v in saved.items()}, **self.want_last}
        self.flagged = set(self._kv_get(db, "flagged", []))

    def _demote_block(self, now, promotes_planned: bool = False) -> str:
        """Why demotes may not run right now, or "" when they may. promotes_planned:
        this pass planned a promote, which blocks its demotes even when the promote
        has already finished by the time this is asked."""
        if night_start(now, self.cfg.window) is None:
            return "outside the demote window"
        if not self.complete:
            return "a Plex read failed this pass"
        n = self._tonight(now)
        if n["failed"]:
            return "a hash check failed tonight"
        if not self.history_ok:
            return "the Plex play history looks wiped or truncated"
        if promotes_planned or self.promote_todo or self.promote_running:
            return "a promote is pending"
        if n["bytes"] >= self.cfg.demote_bytes_per_night:
            return "the night's byte cap is reached"
        if self._weekly_due(now):
            return "the weekly check has not finished"
        if self.deps.register is not None and \
                not (self.register_state["start"] == night_start(now, self.cfg.window)
                     and self.register_state["ok"]):
            return "tonight's register did not succeed"
        return ""

    # ---- weekly check

    def _weekly_due(self, now) -> bool:
        if self.deps.weekly is None or not self.cfg.apply:
            return False
        every = self.cfg.check_every_h * 3600.0
        # a little slack: a run that starts a few minutes later each night must not skip one
        slack = min(3600.0, every * 0.1)
        return self.weekly_running or now - self.weekly_last >= every - slack

    def _weekly(self, db, now):
        """Start the nightly check when the last start is a day old: on start-up, and at
        the start of the demote window. It runs on its own thread; demotes wait."""
        if not self._weekly_due(now) or self.weekly_running or now < self.weekly_retry_at:
            return
        if self.weekly_tried and night_start(now, self.cfg.window) is None:
            return
        self.weekly_tried = True
        self.weekly_running = True
        self._spawn(self._weekly_worker)

    def _weekly_worker(self):
        try:
            db = self._open()
            started = self.clock()
            log.info("nightly check start")
            fn = self.deps.weekly
            summary: dict = {}
            kw: dict = {}
            month = datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m")
            if _accepts(fn, "budget_s"):
                kw["budget_s"] = self.cfg.check_max_h * 3600.0
            if _accepts(fn, "summary"):
                kw["summary"] = summary
            if _accepts(fn, "lost_hook"):
                kw["lost_hook"] = lambda lost, info: self._on_lost(db, lost, info)
            if _accepts(fn, "monthly"):  # the full read sample: once in the first week of a month
                first_week = datetime.fromtimestamp(started, timezone.utc).day <= 7
                kw["monthly"] = first_week and self._kv_get(db, "full_read_month") != month
            fn(db, **kw)
            # the start time, not the end: a long run must not push every night later
            self.weekly_last = started
            self._kv_set(db, "weekly_last_ts", started)
            if kw.get("monthly") and summary.get("finished", True):
                self._kv_set(db, "full_read_month", month)
            self._kv_set(db, "weekly_cursor", {"ts": started, "finished": summary.get("finished", True),
                                               "unreached": len(summary.get("unreached") or [])})
            if summary.get("finished") is False:
                log.warning("nightly check stopped at its time budget; %d rows go first tomorrow",
                            len(summary.get("unreached") or []))
            log.info("nightly check done")
        except Exception as e:
            self._alert("weekly_failed", "-", reason=f"{type(e).__name__}: {e}")
            self.weekly_retry_at = self.clock() + WEEKLY_RETRY  # demotes stay blocked meanwhile
        finally:
            self.weekly_running = False

    # ---- lost files and the automatic re-download

    def _l_is_symlink_ours(self, row) -> bool:
        """Strict, for a delete: exactly one disk path for lib_path, and it is a symlink whose
        target is this row's tierfs link (by name, as reconcile and the orphan cleanup do) and
        points into the tierfs mount. Anything else, including "cannot tell", is False."""
        lib = row.get("lib_path")
        if not lib:
            return False
        found = self._find_disk(row)
        if len(found) != 1:
            return False
        ext = os.path.splitext(lib)[1]
        try:
            got = os.readlink(found[0])
        except OSError:
            return False
        mount = self.cfg.tierfs_mount
        if mount and os.path.dirname(os.path.normpath(got)) != os.path.normpath(mount):
            return False
        return os.path.basename(got) == f"{row['id']}{ext}"

    def redownloader(self):
        """The re-download orchestrator, or None when it cannot act (dry run, no arr
        wiring). Built on first use so a missing arr setting never stops the runner."""
        if self._redl is not None:
            return self._redl
        if not self.cfg.apply or self.deps.arrs is None:
            return None
        try:
            arrs = self.deps.arrs()
        except Exception as e:  # noqa: BLE001
            log.error("re-download: arr clients not usable: %s", type(e).__name__)
            return None
        from . import redownload
        ok = self.deps.altmount_ok or (lambda: True)
        self._redl = redownload.Redownload(
            state=self.state, arrs=arrs, l_ok=self._l_is_symlink_ours,
            busy=lambda id: id in self.promote_running or id in self.demote_running,
            healthy=ok, alert=self._alert, kv_get=self._kv_get, kv_set=self._kv_set,
            enabled=self.cfg.auto_redownload, max_per_night=self.cfg.redownload_max,
            clock=self.clock)
        return self._redl

    def _on_lost(self, db, lost, info):
        """Called by the nightly check, once, with the rows it confirmed lost."""
        rd = self.redownloader()
        if rd is None:
            for l in lost:
                self._once("lost_file", str(l["id"]), reason="no automatic re-download")
            return
        try:
            rd.process(db, lost, info, now=self.clock())
        except Exception as e:  # noqa: BLE001
            log.error("re-download pass failed: %s: %s", type(e).__name__, e)

    def _lost_pass(self, db, now):
        """Every pass: a row the nightly check confirmed lost while its promote was still
        running is acted on once the promote stops (on a night that passed the guards), and
        no new promote starts for it meanwhile."""
        rd = self._redl if self._redl is not None else (
            self.redownloader() if self.cfg.apply else None)
        if rd is None:
            self.lost_hold = set()
            return
        try:
            rd.retry_pending(db, now)
            self.lost_hold = set(rd.pending(db))
        except Exception as e:  # noqa: BLE001
            log.error("re-download retry failed: %s", type(e).__name__)

    # ---- reading the world

    def _read_plex(self, now):
        """Returns (data dict, complete flag). `items` is None when unreadable."""
        idle = self.cfg.hot.idle_days * DAY
        ok = True
        data = {}
        self.read_failed = set()
        for name, fn in (("items", lambda: plex_watch.library_items()),
                         ("plays", lambda: plex_watch.play_history(now - idle)),
                         ("progress", lambda: plex_watch.in_progress()),
                         ("sessions", lambda: plex_watch.sessions()),
                         ("deck", lambda: plex_watch.on_deck())):
            try:
                data[name] = fn()
            except Exception as e:
                ok = False
                self.read_failed.add(name)
                data[name] = None if name == "items" else []
                log.error("plex read %s failed: %s: %s", name, type(e).__name__, e)
        return data, ok

    def _expected_plays(self, db, now) -> int:
        """How many plays last pass's history still holds inside the window now. The window
        slides, so a play that aged out is not a lost play: only the stored plays that are
        still inside it count. A database from before `history_times` falls back to the
        stored count."""
        times = self._kv_get(db, "history_times", None)
        if not isinstance(times, list):
            return int(self._kv_get(db, "history_count", 0) or 0)
        # a little slack at the edge: Plex and this clock may disagree by a pass
        edge = now - self.cfg.hot.idle_days * DAY + HISTORY_EDGE
        return sum(1 for t in times if isinstance(t, (int, float)) and t >= edge)

    def _own_plays(self, db, now, data, plays, synced):
        """Our own play record per rating key (history, in-progress, sessions, remote reads),
        kept in the state DB. The hot set sees the later of ours and Plex's, so a Plex
        history that was wiped cannot make a played file look idle. When Plex's history is
        empty or far shorter than last time, tonight's demotes are refused."""
        st = self.state
        seen = {}
        for p in plays:
            ts = p.get("viewedAt")
            if p.get("ratingKey") is not None and isinstance(ts, (int, float)):
                seen[str(p["ratingKey"])] = max(seen.get(str(p["ratingKey"]), 0), float(ts))
        for s_ in data["sessions"] or []:
            if s_.get("ratingKey") is not None:
                seen[str(s_["ratingKey"])] = float(now)
        for rk, ts in (synced.items() if isinstance(synced, dict) else ()):
            if isinstance(ts, (int, float)):
                seen[str(rk)] = max(seen.get(str(rk), 0), float(ts))
        stored = {}
        if getattr(st, "last_plays", None):
            try:
                if self.cfg.apply and seen:
                    st.merge_last_plays(db, seen)
                stored = st.last_plays(db)
            except Exception as e:
                log.error("own play record failed: %s", type(e).__name__)
        # the history check: only when Plex's history was read this pass
        self.history_ok = True
        if "plays" not in self.read_failed:
            times = sorted(float(p["viewedAt"]) for p in data["plays"] or []
                           if isinstance(p.get("viewedAt"), (int, float)))
            n = len(data["plays"] or [])
            expect = self._expected_plays(db, now)
            if expect > 0 and n < expect * (1 - HISTORY_DROP):
                self.history_ok = False
                start = next_night_start(now, self.cfg.window)
                if start not in self.history_alerted:
                    self.history_alerted.add(start)
                    msg = (f"Plex play history has {n} entries, was {expect}; no demotes until it "
                           "recovers. After a deliberate purge, reset with: DELETE FROM kv "
                           "WHERE key IN ('history_count', 'history_times')")
                    if self.cfg.apply:
                        self._alert("history_shrunk", "-", reason=msg)
                    else:
                        log.error("dry run: %s", msg)
            else:
                self._kv_set(db, "history_count", n)
                self._kv_set(db, "history_times", times)
        merged = dict(stored)
        for rk, ts in seen.items():
            merged[rk] = max(merged.get(rk, 0), ts)
        return [{"ratingKey": rk, "viewedAt": ts} for rk, ts in merged.items()]

    # ---- reads of local files (a download or sync of a file that is whole)

    def _atimes_load(self, db) -> dict:
        d = self._kv_get(db, "local_atimes", {})
        return {str(k): float(v) for k, v in d.items()} if isinstance(d, dict) else {}

    def _local_reads(self, db, now, rows, id_to_rk, added=None) -> dict:
        """{rating key: time} for whole files someone read since we last looked. tierfs sees
        only reads of an offloaded file; a download or sync of a file that is still whole
        leaves nothing but its access time. We note each whole file's access time, and a later
        one is a read, so it restarts the idle clock like a play. Our own reads (qualify,
        promote) are noted again as they end (`_rebase_atime`) and never count. A first sight
        of a file only notes its time, at once. A read within IMPORT_GRACE of the file's
        modification time or its row's creation is Plex analysing a new import and is only
        noted. A read is looked for every ATIME_EVERY, and only counts
        when the file system updates access times (relatime does, at most once a day per
        file, which is enough here)."""
        # Between checks only a file we have not seen yet is looked at, to note its time:
        # a file that appears just after a check must not wait a whole interval for it.
        limited = self.atime_last is not None and now - self.atime_last < ATIME_EVERY
        if not limited:
            self.atime_last = now
        out: dict = {}
        with self.atime_mu:
            seen = self._atimes_load(db)
            ids = {r["id"] for r in rows}
            new = {k: v for k, v in seen.items() if k in ids}
            busy = self.promote_running | self.demote_running | self.reading
            for r in rows:
                id = r["id"]
                if r["state"] != "whole" or id in busy or (limited and id in seen):
                    continue
                found = self._find_disk(r)
                if len(found) != 1:
                    continue
                try:
                    ls = os.lstat(found[0])
                except OSError:
                    continue
                if not stat_mod.S_ISREG(ls.st_mode):
                    continue
                at = ls.st_atime
                base = seen.get(id)
                rk = id_to_rk.get(id) or r.get("rating_key")
                landed = max(ls.st_mtime, float((added or {}).get(str(rk)) or 0))
                if base is not None and at > base + ATIME_SLACK and at > landed + IMPORT_GRACE:
                    if rk is not None:
                        out[str(rk)] = max(out.get(str(rk), 0.0), min(at, now))
                new[id] = self._arm_atime(found[0], ls)
            if new != seen:
                self._kv_set(db, "local_atimes", new)
        for rk, ts in out.items():
            log.info("a read of a whole file (rk=%s) restarts its idle clock", rk)
        return out

    def _arm_atime(self, path, ls) -> float:
        """Put a whole file's access time back to its modification time and return the time
        to note. relatime only updates an access time that is not newer than the file's
        change times, so after a read it would ignore the next one for a day; set back, the
        next read of any kind shows. Only the access time and the change time of the entry
        move, never its content or modification time. A dry run leaves the file alone."""
        at = ls.st_atime
        if at > ls.st_mtime + ATIME_SLACK and self.cfg.apply:
            try:
                os.utime(path, ns=(ls.st_mtime_ns, ls.st_mtime_ns))
                return ls.st_mtime
            except OSError:
                pass
        return at

    def _rebase_atime(self, db, id):
        """Note a whole file's access time after a read of our own, so it is not mistaken
        for someone else's."""
        try:
            row = self.state.get(db, id)
            found = self._find_disk(row) if row else []
            if len(found) != 1:
                return
            ls = os.lstat(found[0])
            if not stat_mod.S_ISREG(ls.st_mode):
                return
            with self.atime_mu:
                cur = self._atimes_load(db)
                cur[id] = self._arm_atime(found[0], ls)
                self._kv_set(db, "local_atimes", cur)
        except Exception as e:
            log.error("noting an access time failed: %s", type(e).__name__)

    # ---- reconcile and bad chunks

    def _reconcile(self, db, rows, now):
        for row in rows:
            if row["state"] != "offloaded" or row["id"] in self.promote_running:
                continue
            found = self._find_disk(row)
            if not found:  # L is gone from every disk: alert, never fail the row for it
                self._once("l_missing", row["id"], lib_path=row.get("lib_path"))
                continue
            if len(found) > 1:
                self._log_skip(row, found)
                continue
            disk = found[0]
            ext = os.path.splitext(row.get("lib_path") or "")[1]
            try:
                got = os.readlink(disk)
            except (OSError, TypeError):
                got = None
            # by name, as ops does: a mount spelled another way still counts
            if got is not None and os.path.basename(got) == f"{row['id']}{ext}":
                continue
            if not self.cfg.apply:
                log.warning("dry run: would mark %s failed, L replaced (%s)", row["id"], disk)
                continue
            if self.state.update_if(db, row["id"], "offloaded", state="failed",
                                    restore_note="L replaced"):
                self.state.log_event(db, row["id"], "l_replaced", {"lib_path": row.get("lib_path")})
                log.warning("%s: L replaced, no longer managed", row["id"])

    # ---- orphan stub cleanup

    def _orphan_note(self, id, what, msg, *a):
        """Log one line per (id, what), not once per pass."""
        if (id, what) not in self.orphan_logged:
            self.orphan_logged.add((id, what))
            log.warning("orphan %s: " + msg, id, *a)

    def _orphan_l_is_ours(self, row, ext) -> bool:
        """Something at L, on any disk, is a symlink into tierfs for this id (by name, as
        reconcile does). With no lib_path we cannot tell, so it counts as ours: keep."""
        if not row.get("lib_path"):
            return True
        for disk in self._find_disk(row):
            try:
                got = os.readlink(disk)
            except OSError:
                continue  # not a symlink (a regular file, or gone)
            if os.path.basename(got) == f"{row['id']}{ext}":
                return True
        return False

    def _orphan_stub(self, row, ext):
        """(stub, lstat, None) when the row's stub is exactly <root>/<id><ext>, a regular
        file with one link, directly in a stub root; else (None, None, why)."""
        id = str(row["id"])
        stub = row.get("stub_path") or ""
        if not stub or "/" in id or id in ("", ".", ".."):
            return None, None, "no usable stub_path or id"
        roots = [os.path.normpath(r.strip()) for r in self.cfg.stub_roots.split(",") if r.strip()]
        for root in roots:
            if stub != os.path.join(root, f"{id}{ext}"):
                continue
            try:
                ls = os.lstat(stub)
            except OSError:
                return None, None, "the stub cannot be read"
            if not stat_mod.S_ISREG(ls.st_mode):
                return None, None, "the stub is not a regular file"
            if ls.st_nlink != 1:
                return None, None, f"the stub has {ls.st_nlink} links"
            return stub, ls, None
        return None, None, "the stub is not <stub root>/<id><ext>"

    def _orphans(self, db, rows, now):
        """Remove the stub and manifest of rows an arr upgrade or delete left behind
        (state=failed, restore_note "L replaced") once TIERING_ORPHAN_GRACE_DAYS have
        passed since the row was marked (its updated_ts). Every check must hold, else
        everything stays. Never touches L or the library. Dry run: logs only."""
        grace = self.cfg.orphan_grace_days
        if grace <= 0 or not self.cfg.manifests:
            return
        n = freed = 0
        for row in rows:
            if row["state"] != "failed" or row.get("restore_note") != "L replaced":
                continue
            marked = row.get("updated_ts")
            if not marked or now - float(marked) < grace * DAY:
                continue
            try:
                size = self._orphan_one(db, row)
            except Exception as e:
                log.error("orphan %s: cleanup failed: %s", row.get("id"), type(e).__name__)
                continue
            if size is not None:
                n += 1
                freed += size
        if n and self.cfg.apply:
            self._alert("orphan_stubs_removed", "orphans", count=n, bytes_freed=freed)

    def _orphan_one(self, db, row):
        """Clean one row; returns the stub's size in bytes if it was (or, in a dry run,
        would be) removed, else None."""
        id = str(row["id"])
        ext = os.path.splitext(row.get("lib_path") or "")[1]
        stub, ls, why = self._orphan_stub(row, ext)
        if stub is None:
            self._orphan_note(id, "keep", "kept: %s", why)
            return None
        mfiles = [os.path.join(self.cfg.manifests, f"{id}{x}") for x in (".json", ".chunks", ".fill")]
        try:
            with open(mfiles[0]) as fh:
                man = json.load(fh)
            if not isinstance(man, dict) or man.get("stub_path") != stub:
                raise ValueError
        except (OSError, ValueError):
            self._orphan_note(id, "keep", "kept: the manifest is missing or its stub_path differs")
            return None
        if self._orphan_l_is_ours(row, ext):
            self._orphan_note(id, "keep", "kept: L is (or may be) a tierfs symlink for this id")
            return None
        if self.deps.open_holders is None:
            self._orphan_note(id, "keep", "kept: no open-file check available")
            return None
        if not self.cfg.apply:
            self._orphan_note(id, "dry", "dry run: would remove stub %s (%d bytes) and manifest",
                              stub, ls.st_size)
            return ls.st_size
        try:
            holders = self.deps.open_holders(stub)
        except Exception as e:
            holders = [-1]
            log.error("orphan %s: open check failed: %s", id, type(e).__name__)
        if holders:
            self._orphan_note(id, "keep", "kept: the open-file check is not clear")
            return None
        # right before deleting: the row and L again, and the stub is still the same file
        cur = self.state.get(db, id)
        if not cur or cur.get("state") != "failed" or cur.get("restore_note") != "L replaced" \
                or cur.get("stub_path") != stub or self._orphan_l_is_ours(cur, ext):
            self._orphan_note(id, "keep", "kept: the row or L changed during the check")
            return None
        try:
            now_ls = os.lstat(stub)
            if (now_ls.st_ino, now_ls.st_dev) != (ls.st_ino, ls.st_dev) or now_ls.st_nlink != 1:
                raise OSError
            os.unlink(stub)
        except OSError:
            self._orphan_note(id, "keep", "kept: the stub changed or cannot be removed")
            return None
        for m in mfiles:
            try:
                os.unlink(m)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.error("orphan %s: cannot remove %s: %s", id, os.path.basename(m),
                          type(e).__name__)
        self.state.update_if(db, id, "failed", restore_note="orphan cleaned")
        self.state.log_event(db, id, "orphan_stub_removed", {"id": id, "size": ls.st_size})
        log.warning("orphan %s: stub and manifest removed (%d bytes)", id, ls.st_size)
        return ls.st_size

    def _once(self, kind, id, **detail):
        """Alert once per (kind, id), ever (the alerts file seeds this after a restart)."""
        if (kind, str(id)) in self.ops_alerted:
            return
        if not self.cfg.apply:
            log.error("dry run: %s on %s", kind, id)
            return
        self.ops_alerted.add((kind, str(id)))
        self._alert(kind, id, **detail)

    def _follow_once(self):
        """Read new tierfs events. A `want` starts its promote now; the rest wait
        in tail_events for the next pass."""
        with self.follow_mu:
            for ev in self.scanner.scan():
                if ev["ev"] == "want":
                    self._want(ev)
                else:
                    self.tail_events.append(ev)

    def _want(self, ev):
        """A player waits on a file that is not all on local disk: promote it now,
        ahead of every other promote and outside the concurrency limit."""
        id = ev["id"]
        now = self.clock()
        if now - ev["ts"] > WANT_FRESH:  # nobody waits that long
            return
        off = ev.get("offset")
        with self.mu:
            self.watched[id] = max(self.watched.get(id, 0.0), now)
            if off is not None:
                self.want_off[id] = off
            # a player waits: the retry backoff does not apply, only the 60 s spacing
            if not self.cfg.apply or id in self.promote_running or id in self.demote_running \
                    or now - self.want_last.get(id, -WANT_SPACING) < WANT_SPACING:
                return
            self.want_last[id] = now
            self.promote_running.add(id)
            self.promote_started[id] = now
        self._spawn(lambda: self._want_worker(id))

    def _is_watched(self, id, now=None) -> bool:
        """Someone is watching this file (call with self.mu held or not: a dict read)."""
        t = self.watched.get(id)
        return t is not None and (now if now is not None else self.clock()) - t < WATCHED_TTL

    def _watched_running(self, but=None) -> bool:
        now = self.clock()
        with self.mu:
            return any(o != but and self._is_watched(o, now) for o in self.promote_running)

    def _yield_to_watched(self, id=None):
        """Block while a promote someone is watching runs, unless `id` is watched too.
        Promotes nobody watches call it before each chunk, the demote worker before each file."""
        paused = False
        while not self.stop.is_set():
            if (id is not None and self._is_watched(id)) or not self._watched_running(but=id):
                break
            if not paused:
                paused = True
                log.info("%s paused: a watched file is being fetched",
                         f"promote {id}" if id is not None else "demotes")
            self.stop.wait(YIELD_POLL)
        if paused:
            log.info("%s resumed", f"promote {id}" if id is not None else "demotes")

    def _session_wants(self, sessions, rows_by_rk, now):
        """A live Plex session on a file that is not all on disk is a want: its promote
        starts now, outside the concurrency limit, and the others pause."""
        for s_ in sessions:
            rk = s_.get("ratingKey")
            for r in rows_by_rk.get(str(rk), []) if rk is not None else []:
                if r["state"] in ("offloaded", "promoting"):
                    self._want({"id": r["id"], "ts": now})

    def _want_worker(self, id):
        try:
            db = self._open()
            with self.mu:
                saved = {k: v for k, v in self.want_last.items() if self.clock() - v < DAY}
                self.want_last = dict(saved)
            self._kv_set(db, "want_last", saved)
            self._promote_one(db, id, None, wanted=True)
        except Exception as e:
            log.error("wanted promote %s died: %s: %s", id, type(e).__name__, e)
            with self.mu:
                self.promote_running.discard(id)
                self.promote_started.pop(id, None)

    def follow_forever(self):
        while not self.stop.is_set():
            try:
                self._follow_once()
            except Exception as e:
                log.error("event follower failed: %s", type(e).__name__)
            self.stop.wait(FOLLOW_POLL)

    def _bad_chunks(self, db, now):
        self.slot_timeouts = 0
        night = next_night_start(now, self.cfg.window)
        self.slot_alerted = {k for k in self.slot_alerted if k[0] == night}
        self._follow_once()
        with self.follow_mu:
            evs, self.tail_events = self.tail_events, []
        for ev in evs:
            if ev["ev"] == "slot_timeout":
                self.slot_timeouts += 1
                if (night, ev["id"]) in self.slot_alerted:
                    continue
                self.slot_alerted.add((night, ev["id"]))
                self._kv_set(db, "slot_alerted", sorted(self.slot_alerted))
                if not self.cfg.apply:
                    log.error("dry run: slot_timeout on %s", ev["id"])
                    continue
                self._alert("slot_timeout", ev["id"])
                continue
            self.pending_bad[ev["id"]] = ev
        for id, ev in list(self.pending_bad.items()):
            row = self.state.get(db, id)
            if row is None or row["state"] not in ("offloaded", "promoting", "offloading") \
                    or (not ev.get("held") and ev["ts"] <= (row.get("updated_ts") or 0)):
                del self.pending_bad[id]
                continue
            if id in self.promote_running:
                continue
            if id in self.demote_running:  # the demote's own writes would race ours
                ev["held"] = True  # and its later updated_ts must not drop this event
                continue
            del self.pending_bad[id]
            # The row is not failed here: a bad read may be one bad copy only. A promote
            # (first in line, whether or not the hot set wants the file) verifies every
            # chunk and uses failover and the twin. Only a promote that ends `failed`
            # (mismatch on every copy) makes the file lost.
            note = f"bad_chunk at offset {ev['offset']}; promote requested"
            self._note_failure(now)
            if not self.cfg.apply:
                log.error("dry run: would flag %s for promote: %s", id, note)
                continue
            if not self.state.update_if(db, id, row["state"], restore_note=note):
                ev["held"] = True  # the row moved on under us: look again next pass
                self.pending_bad[id] = ev
                continue
            self.state.log_event(db, id, "bad_chunk", {"offset": ev["offset"]})
            self.flagged.add(id)
            self._kv_set(db, "flagged", sorted(self.flagged))
            self._alert("bad_chunk", id, offset=ev["offset"], lib_path=row.get("lib_path"))

    # ---- nightly register

    def _register(self, db, now):
        """Run register once per night, before the demote window opens. A
        failure is logged and blocks that night's demotes."""
        reg = self.deps.register
        if reg is None:
            return
        start = next_night_start(now, self.cfg.window)
        if self.register_state["start"] == start or now < start - REGISTER_LEAD:
            return
        self.register_state = {"start": start, "ok": False}
        try:
            res = reg(db, self.cfg.apply)
        except Exception as e:
            log.error("register failed (%s: %s); no demotes tonight", type(e).__name__, e)
            self._register_alert(f"{type(e).__name__}: {e}")
            return
        bad = self._register_verdict(res)
        if bad:
            log.error("register unhealthy (%s); no demotes tonight: %s", bad,
                      json.dumps(res, sort_keys=True, default=str))
            self._register_alert(bad, result=res)
            return
        self.register_state["ok"] = True
        log.info("register done: %s", json.dumps(res, sort_keys=True, default=str))

    @staticmethod
    def _register_verdict(res) -> str:
        """"" when the register result is healthy, else why not. Fails closed: a result
        that is not a dict, lacks a required key, holds a non-int, or lists nothing is bad."""
        if not isinstance(res, dict):
            return f"register returned {type(res).__name__}, not a dict"
        for k in REGISTER_KEYS:
            v = res.get(k)
            if k not in res or isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return f"register result key {k!r} is missing or not a count"
        if res["errors"] > 0:
            return f"{res['errors']} errors"
        if res["listed"] == 0:
            return "register listed no files"
        unmatched = res["missing_local"] + res["ambiguous"]
        if unmatched > REGISTER_BAD_FRAC * res["listed"]:
            return f"{unmatched} of {res['listed']} listed files missing or ambiguous"
        return ""

    def _register_alert(self, why, **detail):
        if self.cfg.apply:
            self._alert("register_failed", "register", reason=why, **detail)

    def _seed_alerted(self):
        """After a restart, ops events already alerted stay alerted."""
        try:
            with open(self.cfg.alerts) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("kind") in ONCE_KINDS:
                        self.ops_alerted.add((r["kind"], str(r.get("id"))))
        except OSError:
            pass

    def _ops_events(self, db):
        """Read the events table incrementally (from the last row id). Alert once per id
        for promote_short_remote and takeover_orphan; collect weekly_flag ids for promote."""
        read = getattr(self.state, "events", None)
        if not callable(read):
            return
        try:
            evs = read(db, after_seq=min(self.ops_seq, self.flag_seq))
        except Exception as e:
            log.error("cannot read ops events: %s", type(e).__name__)
            return
        for e in evs:
            kind, id = e.get("kind"), str(e.get("id"))
            seq = int(e.get("seq") or 0)
            if kind == "weekly_flag" and seq > self.flag_seq:
                self.flagged.add(id)
            elif kind in ALERT_EVENTS and seq > self.ops_seq:
                self._once(kind, id, **(e.get("detail") or {}))
        top = max([int(e.get("seq") or 0) for e in evs] + [0])
        self.ops_seq = max(self.ops_seq, top)
        self.flag_seq = max(self.flag_seq, top)
        self._kv_set(db, "ops_seq", self.ops_seq)
        self._kv_set(db, "weekly_flag_cursor", self.flag_seq)
        self._kv_set(db, "flagged", sorted(self.flagged))

    def _find_disk(self, r):
        """Every disk path of a row's share path. With no TIERING_PATH_MAP the share path
        is used as is, as ops does."""
        lib = r.get("lib_path") or ""
        if self.cfg.path_map:
            return paths.disk_paths(lib, self.cfg.path_map)
        return [lib] if lib and os.path.lexists(lib) else []

    def _log_skip(self, r, found):
        lib = r.get("lib_path")
        if lib not in self.skipped_paths:
            self.skipped_paths.add(lib)
            log.warning("skipping %s: lib_path maps to %d disk paths, need exactly 1 (%s)",
                        r["id"], len(found), lib)

    def _resolve_disk(self, r):
        """The one disk path for a row's share path, or None (logged once per pass)."""
        found = self._find_disk(r)
        if len(found) == 1:
            return found[0]
        self._log_skip(r, found)
        return None

    # ---- the pass

    def run_pass(self, wait: bool = False) -> dict:
        now = self.clock()
        self.last_pass = now
        self.passes += 1
        try:
            return self._pass(now, wait)
        finally:
            self._heartbeat(now)

    def _pass(self, now, wait):
        db = self.db()
        cfg, st = self.cfg, self.state
        self._load_persisted(db)
        self.skipped_paths = set()
        self._register(db, now)
        self._weekly(db, now)
        self._lost_pass(db, now)
        data, complete = self._read_plex(now)
        items = data["items"]
        if items is None:
            self.complete = False
            self.promote_todo.clear()
            self.demote_todo.clear()
            raise RuntimeError("library read failed; pass skipped")

        rows = st.list_files(db)
        self._reconcile(db, rows, now)
        self._orphans(db, rows, now)
        self._bad_chunks(db, now)
        self._ops_events(db)
        rows = st.list_files(db)

        path_rk = {}
        by_rk_item = {}
        path_ms = {}
        for it in items:
            by_rk_item[str(it["ratingKey"])] = it
            for f in it.get("files") or []:
                path_rk[f] = str(it["ratingKey"])  # Plex's path is the share path
            path_ms.update(it.get("durations") or {})
        self.durations = {r["id"]: path_ms[r["lib_path"]] for r in rows
                          if path_ms.get(r.get("lib_path"))}
        rows_by_rk: dict = {}
        id_to_rk = {}
        for r in rows:
            rk = path_rk.get(r.get("lib_path")) or r.get("rating_key")
            if rk is None:
                continue
            rk = str(rk)
            if cfg.apply and r.get("rating_key") != rk:
                st.upsert(db, {"id": r["id"], "rating_key": rk})
            r["rating_key"] = rk
            rows_by_rk.setdefault(rk, []).append(r)
            id_to_rk[r["id"]] = rk

        self._session_wants(data["sessions"] or [], rows_by_rk, now)

        synced = {}
        if cfg.events:
            try:
                synced = plex_watch.read_tierfs_events(
                    cfg.events, now - cfg.hot.idle_days * DAY, id_to_rk)
            except Exception as e:
                complete = False
                log.error("tierfs events read failed: %s", type(e).__name__)
        added = {rk: it.get("addedAt") for rk, it in by_rk_item.items()}
        for rk, ts in self._local_reads(db, now, rows, id_to_rk, added).items():
            synced[rk] = max(synced.get(rk, 0.0), ts)
        plays = list(data["plays"] or [])
        for p in data["progress"] or []:
            plays.append({**p, "viewedAt": p.get("viewedAt") or now})
        plays = self._own_plays(db, now, data, plays, synced)

        desired = hotset.desired_whole(items, plays, data["sessions"] or [],
                                       data["deck"] or [], synced, now, cfg.hot)
        never = {s.lower() for s in cfg.hot.never_sections}
        offloaded_rks, whole_rks = set(), set()
        for rk, rs in rows_by_rk.items():
            if any(r["state"] in ACTIVE_STATES and r["id"] not in self.demote_running
                   for r in rs):
                offloaded_rks.add(rk)
            elif all(r["state"] == "whole" for r in rs):
                it = by_rk_item.get(rk)
                sec = str((it or {}).get("section") or "").strip().lower()
                if it is not None and sec not in never:
                    whole_rks.add(rk)
        acts = hotset.plan_actions(desired, offloaded_rks, whole_rks, now)

        # ---- promotes
        order = self._order_promotes([a for a in acts if a.kind == "promote"],
                                     by_rk_item, data["sessions"] or [])
        pjobs = []
        for a in order:
            for r in sorted(rows_by_rk[a.rating_key], key=lambda r: r["id"]):
                if r["state"] in ACTIVE_STATES and r["id"] not in self.demote_running:
                    pjobs.append((a, r))
        # a promote already under way is always resumed; a weekly-flagged row is promoted
        # whether or not the hot set wants it (the remote copy is the one at risk)
        queued = {r["id"] for _, r in pjobs}
        extra = [r for r in rows if r["id"] not in queued and r["id"] not in self.demote_running
                 and (r["state"] == "promoting"
                      or (r["state"] in ACTIVE_STATES and r["id"] in self.flagged))]
        pjobs = [(hotset.Action("promote", r.get("rating_key"),
                                "flagged" if r["id"] in self.flagged else "resume", 1), r)
                 for r in sorted(extra, key=lambda r: r["id"])] + pjobs
        keep = {r["id"] for r in rows if r["state"] in ACTIVE_STATES}
        if self.flagged - keep:  # promoted, gone or no longer ours
            self.flagged &= keep
            self._kv_set(db, "flagged", sorted(self.flagged))
        for id, t0 in list(self.promote_started.items()):
            if now - t0 > STUCK_AFTER:
                self._once("promote_stuck", id, running_seconds=int(now - t0))
        for a, r in pjobs:
            log.info("plan: promote %s rk=%s id=%s (%s)", r["lib_path"], a.rating_key,
                     r["id"], a.reason)
        with self.mu:
            self.complete = complete
            self.desired_rks = set(desired)
            self.promote_todo = deque(r["id"] for _, r in pjobs)
            self._rows = {r["id"]: r for _, r in pjobs}
        if cfg.apply:
            self._spawn_promote_workers()

        # ---- demotes
        dcands = []
        for a in acts:
            if a.kind == "demote":
                for r in sorted(rows_by_rk[a.rating_key], key=lambda r: r["id"]):
                    dcands.append(r)
        for r in rows:  # an offload a crash left half done is resumed, not restarted
            if r["state"] == "offloading":
                dcands.insert(0, r)
        if cfg.only_paths:  # before disk, qualify or anything remote: others never offload
            kept = [r for r in dcands if self._may_demote(r)]
            if not cfg.apply:
                for r in dcands:
                    if not self._may_demote(r):
                        log.debug("plan: demote %s id=%s skipped: not in TIERING_ONLY_PATHS",
                                  r["lib_path"], r["id"])
                if len(kept) != len(dcands):
                    log.info("demotes skipped by TIERING_ONLY_PATHS: %d", len(dcands) - len(kept))
            dcands = kept
        self.disk = {}
        usable = []
        for r in dcands:
            d = self._resolve_disk(r)
            if d is not None:
                self.disk[r["id"]] = d
                usable.append(r)
        dcands = usable
        block = self._demote_block(now, promotes_planned=bool(pjobs))
        n = self._tonight(now)
        tried = n.setdefault("tried", []) if n is not None else []
        todo = [] if block else [r for r in dcands if r["id"] not in tried]
        if block:
            log.info("demotes skipped: %s", block)
        heads, skipped = {}, collections.Counter()
        for r in todo:
            if r["state"] == "offloading":
                continue  # its head was set when it started
            frac, why = self._head_frac(r)
            if frac is None:
                log.debug("demote %s skipped: %s", r["id"], why)
                skipped[why] += 1
            else:
                heads[r["id"]] = frac
        if skipped:  # one line a pass, not one per file: most of a TV library is short
            log.info("demotes skipped by the head rule: %s",
                     ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
        todo = [r for r in todo if r["state"] == "offloading" or r["id"] in heads]
        if not cfg.apply:
            used = n["bytes"] if n else 0
            for r in todo:
                size = int(r.get("size") or 0)
                if used + size > cfg.demote_bytes_per_night:
                    break
                used += size
                log.info("plan: demote %s id=%s (idle, %d bytes, head %d%%)", self.disk[r["id"]],
                         r["id"], size, round(100 * heads.get(r["id"], 0)))
        else:
            with self.mu:
                self.demote_todo = deque(r["id"] for r in todo)
            self._spawn_demote_worker()

        self.counts = {"promotes_planned": len(pjobs), "promotes_running": len(self.promote_running),
                       "demotes_planned": len(todo), "demote_blocked": block,
                       "plex_complete": complete, "night_bytes": (n or {}).get("bytes", 0),
                       "slot_timeouts": self.slot_timeouts,
                       "paths_skipped": len(self.skipped_paths)}
        if complete:
            self.last_ok = now
        if wait:
            self.join()
        return self.counts

    def _head_frac(self, row):
        """The head share for a new offload: max(20 %, TIERING_HEAD_MIN_S of play time), so a
        player always starts with that much on local disk. The play time is Plex's, else the
        one Radarr or Sonarr measured from the file (``play_ms``; never a show's listed slot
        length, which runs longer than the file and would shrink the head). (None, why) when
        neither has one, the file plays under TIERING_MIN_PLAY_S (a half-hour show), or the
        head would leave too little to offload."""
        ms = self.durations.get(row["id"]) or row.get("play_ms")
        if not ms or ms <= 0:
            return None, "no play time from Plex or the arr (the head is sized by it)"
        if ms < self.cfg.min_play_s * 1000.0:
            return None, f"under {self.cfg.min_play_s / 60:.0f} minutes of play"
        frac = max(HEAD_FRAC, self.cfg.head_min_s * 1000.0 / ms)
        if frac > 1.0 - MIN_OFFLOAD_FRAC:
            return None, f"too short for a {self.cfg.head_min_s / 60:.0f}-minute head"
        return frac, ""

    @staticmethod
    def _order_promotes(acts, by_rk, sessions):
        """Live-session items first, then the rest of those seasons in episode
        order, then everything else (plan order)."""
        live = [str(s["ratingKey"]) for s in sessions if s.get("ratingKey") is not None]
        want = {a.rating_key: a for a in acts}
        out, seen = [], set()

        def take(rk):
            if rk in want and rk not in seen:
                seen.add(rk)
                out.append(want[rk])

        for rk in sorted(live):
            take(rk)
        for rk in sorted(live):
            it = by_rk.get(rk) or {}
            if it.get("type") != "episode":
                continue
            show, sn = it.get("grandparentRatingKey"), it.get("parentIndex")
            eps = [e for e in by_rk.values() if e.get("type") == "episode"
                   and e.get("grandparentRatingKey") == show and e.get("parentIndex") == sn]
            for e in sorted(eps, key=lambda e: (e.get("index") is None, e.get("index") or 0,
                                                str(e["ratingKey"]))):
                take(str(e["ratingKey"]))
        for a in acts:
            take(a.rating_key)
        return out

    # ---- workers

    def _spawn(self, target):
        t = threading.Thread(target=target, daemon=True)
        self.threads = [x for x in self.threads if x.is_alive()] + [t]
        t.start()

    def join(self):
        for t in list(self.threads):
            t.join()

    def _spawn_promote_workers(self):
        with self.mu:
            n = max(min(self.cfg.promote_concurrency - self.promote_workers,
                        len(self.promote_todo)), 0)
            self.promote_workers += n
        for _ in range(n):
            self._spawn(self._promote_worker)

    def _next_promote(self):
        with self.mu:
            now = self.clock()
            while self.promote_todo:
                id = self.promote_todo.popleft()
                if id in self.promote_running or self.retry_after.get(id, 0) > now \
                        or id in self.demote_running or id in self.lost_hold:
                    continue
                self.promote_running.add(id)
                self.promote_started[id] = now
                return id, self._rows.get(id)
            return None, None

    def _backoff(self, id, what, reason):
        """Exponential retry delay (capped), and an alert after repeated failures."""
        with self.mu:
            n = self.fails[id] = self.fails.get(id, 0) + 1
            self.retry_after[id] = self.clock() + min(RETRY_DELAY * 2 ** (n - 1), BACKOFF_MAX)
        if n == FAIL_ALERT_AFTER:
            self._alert(f"{what}_retrying", id, failures=n, reason=reason)

    def _promote_worker(self):
        try:
            db = self._open()
            while not self.stop.is_set():
                id, row = self._next_promote()
                if id is None:
                    return
                self._promote_one(db, id, row)
        except Exception as e:
            self.worker_deaths += 1
            log.error("promote worker died: %s: %s", type(e).__name__, e)
            self._alert("promote_worker_died", "-", reason=f"{type(e).__name__}: {e}")
        finally:
            with self.mu:
                self.promote_workers -= 1

    def _promote_one(self, db, id, row, wanted=False):
        """Promote one row (already in promote_running). A wanted promote only runs for
        a row that is still offloaded or promoting."""
        try:
            cur = self.state.get(db, id) or row
            if cur is None or cur["state"] not in (ACTIVE_STATES if not wanted
                                                  else ("offloaded", "promoting")):
                return
            log.info("promote start %s%s", id, " (wanted)" if wanted else "")
            kw = {"want": lambda: self.want_off.get(id), "urgent": lambda: self._is_watched(id),
                  "pause": lambda: self._yield_to_watched(id)}
            res = self.deps.promote(db, cur, True, **{k: v for k, v in kw.items()
                                                      if _takes(self.deps.promote, k)})
            self._after(db, id, "promote", res)
        except Exception as e:
            log.error("promote %s raised %s: %s", id, type(e).__name__, e)
            self._note_failure(self.clock())
            self._alert("promote_failed", id, reason=f"{type(e).__name__}: {e}")
            self._backoff(id, "promote", f"{type(e).__name__}")
        finally:
            with self.mu:
                self.promote_running.discard(id)
                self.promote_started.pop(id, None)
                self.want_off.pop(id, None)
                self.watched.pop(id, None)

    def _after(self, db, id, what, res):
        if res.ok:
            log.info("%s %s done: state=%s", what, id, res.state)
            self.fails.pop(id, None)
            if what == "promote":
                self._rebase_atime(db, id)  # promote read the whole file itself
            if what == "promote" and id in self.flagged:  # its flag ends with its promote
                self.flagged.discard(id)
                self._kv_set(db, "flagged", sorted(self.flagged))
            return
        log.warning("%s %s not done: %s (state=%s)", what, id, res.reason, res.state)
        self._backoff(id, what, res.reason)
        if res.state == "failed":
            self._note_failure(self.clock())
            self._alert(f"{what}_failed", id, reason=res.reason)
            if what == "promote" and res.reason == "chunk mismatch":
                # every copy (upload and twin) gave a bad chunk: the exact file is lost
                rd = self.redownloader()
                if rd is not None:
                    rd.mark_lost(db, id, "a", "chunk mismatch on every copy")
                else:
                    self._once("lost_file", id, reason="chunk mismatch on every copy")
                if id in self.flagged:
                    self.flagged.discard(id)
                    self._kv_set(db, "flagged", sorted(self.flagged))

    def _spawn_demote_worker(self):
        with self.mu:
            if self.demote_worker or not self.demote_todo:
                return
            self.demote_worker = True
        self._spawn(self._demote_worker)

    def _demote_worker(self):
        try:
            db = self._open()
            while not self.stop.is_set():
                with self.mu:
                    if not self.demote_todo:
                        return
                    id = self.demote_todo.popleft()
                self._yield_to_watched()
                now = self.clock()
                block = self._demote_block(now)
                if block:
                    log.info("demotes stopped: %s", block)
                    return
                self._demote_one(db, id, now)
        except Exception as e:
            log.error("demote worker raised %s: %s", type(e).__name__, e)
        finally:
            with self.mu:
                self.demote_worker = False

    def _demote_one(self, db, id, now):
        with self.mu:
            self.reading.add(id)
        try:
            self._demote_inner(db, id, now)
        finally:
            with self.mu:
                self.reading.discard(id)
                read = id in self.own_read
                self.own_read.discard(id)
            if read:
                self._rebase_atime(db, id)  # qualify read the whole file itself

    def _demote_inner(self, db, id, now):
        cfg, st = self.cfg, self.state
        n = self._tonight(now)
        row = st.get(db, id)
        if row is None or row["state"] not in ("whole", "offloading"):
            return
        if not self._may_demote(row):
            return  # not on the TIERING_ONLY_PATHS allowlist; nothing remote runs
        with self.mu:
            n.setdefault("tried", []).append(id)
            self._save_night()
        if row.get("rating_key") in self.desired_rks:
            return
        size = int(row.get("size") or 0)
        resume = row["state"] == "offloading"
        disk = self.disk.get(id)
        if disk is None:
            log.info("demote %s skipped: no single disk path", id)
            return
        if n["bytes"] + size > cfg.demote_bytes_per_night:
            log.info("demote %s skipped: would pass the night's cap", id)
            return
        head_frac = None
        if not resume:
            head_frac, why = self._head_frac(row)
            if head_frac is None:
                log.info("demote %s skipped: %s", id, why)
                return
            try:
                ls = os.lstat(disk)
            except OSError as e:
                log.warning("demote %s skipped: cannot stat L (%s)", id, type(e).__name__)
                return
            if os.path.islink(disk) or ls.st_nlink != 1:
                log.info("demote %s skipped: L is a symlink or has other hardlinks", id)
                return
            g = self.deps.gate(disk)
            if g != "free":
                log.info("demote %s skipped: glacier gate says %s", id, g)
                return
            # qualify reads the file, so it gets the disk path; the DB row keeps the share path
            with self.mu:
                self.own_read.add(id)
            q = self.deps.qualify({**row, "lib_path": disk})
            if not q.ok and any(str(r).endswith("_unsure") for r in q.reasons):
                # a partial search or a spent budget: not tonight, and nothing is recorded
                log.info("demote %s not tonight: %s", id, ",".join(q.reasons))
                return
            if not q.ok:
                log.info("demote %s skipped: qualify failed: %s", id, ",".join(q.reasons))
                st.log_event(db, id, "qualify_failed", {"reasons": list(q.reasons)})
                return
            cols = set(getattr(st, "COLUMNS", None) or FALLBACK_COLUMNS) - set(PROTECTED_COLUMNS)
            fields = {k: v for k, v in (q.fields or {}).items() if k in cols}
            fields.setdefault("last_check_ts", now)
            if not st.update_if(db, id, "whole", **fields):
                log.info("demote %s dropped: row changed during qualify", id)
                return
            row = st.get(db, id)
            block = self._demote_block(self.clock())  # qualify takes a while
            if block or row.get("rating_key") in self.desired_rks:
                log.info("demote %s dropped after qualify: %s", id, block or "wanted again")
                return
            # qualify read the whole file and takes minutes: a torrent may have started
            # to seed it, or a link been made, since the first look. Ask again, now.
            try:
                ls = os.lstat(disk)
            except OSError as e:
                log.warning("demote %s dropped: cannot stat L (%s)", id, type(e).__name__)
                return
            if os.path.islink(disk) or ls.st_nlink != 1:
                log.info("demote %s dropped: L became a symlink or got other hardlinks", id)
                return
            g = self.deps.gate(disk)
            if g != "free":
                log.info("demote %s dropped after qualify: glacier gate says %s", id, g)
                return
        log.info("demote start %s", id)
        with self.mu:
            self.demote_running.add(id)
        try:
            if head_frac is not None and _takes(self.deps.demote, "head_frac"):
                res = self.deps.demote(db, row, True, head_frac=head_frac)
            else:
                res = self.deps.demote(db, row, True)
        except Exception as e:
            log.error("demote %s raised %s: %s", id, type(e).__name__, e)
            self._note_failure(self.clock())  # no more demotes tonight
            self._alert("demote_failed", id, reason=f"{type(e).__name__}: {e}")
            return
        finally:
            with self.mu:
                self.demote_running.discard(id)
        self._after(db, id, "demote", res)
        if res.ok:
            with self.mu:
                n["bytes"] += size
                self._save_night()

    # ---- liveness

    def _heartbeat(self, now):
        try:
            tmp = self.heartbeat_path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(f"{now:.0f}\n")
            os.replace(tmp, self.heartbeat_path)
        except OSError as e:
            log.warning("cannot write heartbeat: %s", type(e).__name__)

    def health(self) -> dict:
        return {"last_pass_ts": self.last_pass, "last_ok_pass_ts": self.last_ok,
                "passes": self.passes, "errors": self.errors, "apply": self.cfg.apply,
                "counts": dict(self.counts),
                "promote_workers": self.promote_workers,
                "worker_deaths": self.worker_deaths,
                "stuck_promotes": sorted(i for i, t in dict(self.promote_started).items()
                                         if self.clock() - t > STUCK_AFTER)}

    def run_forever(self):
        if self.cfg.apply and self.cfg.events:
            threading.Thread(target=self.follow_forever, daemon=True).start()
        while not self.stop.is_set():
            try:
                self.run_pass()
            except Exception as e:
                self.errors += 1
                log.error("pass failed: %s: %s", type(e).__name__, e)
            self.sleep(self.cfg.pass_seconds)


# --------------------------------------------------------------------------
# process plumbing


def acquire_lock(db_path):
    """Exclusive flock on a file next to the DB. Returns the open file, or None
    when another instance holds it."""
    fh = open(db_path + ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def start_health_server(runner: Runner, spec: str):
    host, _, port = spec.rpartition(":")
    host = host or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("TIERING_HTTP must be a localhost address")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/healthz":
                self.send_error(404)
                return
            body = json.dumps(runner.health(), default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None, env=None, deps=None) -> int:
    once = "--once" in (sys.argv[1:] if argv is None else argv)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(Redact())
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        cfg = Config.from_env(env)
    except ValueError as e:
        log.error("config: %s", e)
        return 2
    if not cfg.apply and not cfg.gate_path_map:
        log.warning("TIERING_GATE_PATH_MAP is not set: the glacier gate would read every "
                    "file as free (APPLY refuses to start without it)")
    lock = acquire_lock(cfg.db)
    if lock is None:
        log.error("another runner holds the lock for %s", cfg.db)
        return 1
    try:
        log.info("runner starting (%s)", "APPLY" if cfg.apply else "dry run")
        try:
            deps = deps or load_deps(cfg)
        except Exception as e:  # a missing or bad setting; the messages name settings, not values
            log.error("settings: %s: %s", type(e).__name__, e)
            return 2
        runner = Runner(cfg, deps)
        runner.announce_only_paths()
        if once:
            try:
                runner.run_pass(wait=True)
            except Exception as e:
                log.error("pass failed: %s: %s", type(e).__name__, e)
                return 1
            return 0
        srv = start_health_server(runner, cfg.http)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: runner.stop.set())
        runner.sleep = lambda s: runner.stop.wait(s)
        try:
            runner.run_forever()
        finally:
            srv.shutdown()
            runner.join()
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
