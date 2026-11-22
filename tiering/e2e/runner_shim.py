#!/usr/bin/env python3
"""Starts the real runner for the e2e tests, with two test-only switches.

E2E_KILL_AT=<point>[#n]  SIGKILL this process the n-th time it reaches the point.
  Points: the names ops passes to `_point`, plus
    offload.stepN / promote.stepN   right after the step marker is in the DB
    offload.punched                 right after the hole is punched, before the checks
    promote.fillN                   after N chunks were fetched from the remote
E2E_UNPRIV=1  the open-handle scan skips processes it may not read (an
  unprivileged test user cannot read other users' /proc/<pid>/fd; CI runs as root
  and does not set this).

Run with the working directory at tiering/, like `python -m offload.runner`.
"""
import functools
import os
import signal
import sys

sys.path.insert(0, os.getcwd())

from offload import ops, runner  # noqa: E402

KILL, _, NTH = os.environ.get("E2E_KILL_AT", "").partition("#")
_left = [int(NTH or 1)]


def hit(name):
    if name and name == KILL:
        _left[0] -= 1
        if _left[0] <= 0:
            os.kill(os.getpid(), signal.SIGKILL)


def after(fn, name_of):
    @functools.wraps(fn)
    def w(*a, **kw):
        out = fn(*a, **kw)
        hit(name_of(*a, **kw))
        return out
    return w


if KILL:
    ops._point = hit
    ops._record_off = after(ops._record_off, lambda db, id, step, hook: f"offload.step{step}")
    ops._record = after(ops._record, lambda db, id, step, state, hook: f"promote.step{step}")
    ops.punch_hole = after(ops.punch_hole, lambda *a, **kw: "offload.punched")
    _n = [0]

    def _fill_name(*a, **kw):
        _n[0] += 1
        return f"promote.fill{_n[0]}"
    ops._fetch_chunk = after(ops._fetch_chunk, _fill_name)

if os.environ.get("E2E_UNPRIV") == "1":
    lenient = functools.partial(ops.scan_open_holders, ignore_unreadable=True)
    strict = ops.scan_open_holders
    for obj in vars(ops).values():
        if callable(obj) and getattr(obj, "__defaults__", None):
            obj.__defaults__ = tuple(lenient if d is strict else d for d in obj.__defaults__)
        if callable(obj) and getattr(obj, "__kwdefaults__", None):
            obj.__kwdefaults__ = {k: (lenient if d is strict else d)
                                  for k, d in obj.__kwdefaults__.items()}
    ops.scan_open_holders = lenient

sys.exit(runner.main())
