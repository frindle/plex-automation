#!/usr/bin/env python3
"""Write the .env values SABnzbd needs into its sabnzbd.ini. Runs at every container start.

Standard library only (the linuxserver image has python3). Idempotent: it edits only the keys it
owns and leaves every other line alone, so a second run changes nothing. The file is written with
mode 600 through a temp file and a rename. It never prints or logs a credential: the output
carries counts and key names only.

Reads (all from the container environment):
  NNTP_PRIMARY_HOST/PORT/USER/PASS   the server entry; a missing host or user means no server is written
  SABNZBD_MAX_MBIT                   download cap in megabit/s (default 750); 0 = unlimited
  SABNZBD_CONNECTIONS                connections this app may open (required for the server entry)
  ALTMOUNT_CONNECTIONS               connections AltMount's provider entry uses (same account)
  USENET_ACCOUNT_CONNECTIONS         the account's connection limit
  SABNZBD_API_KEY                    set as SABNZBD's own API key when given
  SABNZBD_COMPLETE_DIR / SABNZBD_INCOMPLETE_DIR   container paths
  SABNZBD_CATEGORY_RADARR / _SONARR               categories (radarr, sonarr)
  PUID / PGID                        owner of sabnzbd.ini (default 99/100), so SABnzbd can read it

SABnzbd, AltMount and the tiering STAT checks (NNTP_PRIMARY_CONNS, default 8) use the same account, so
the three must stay at or under the account limit. SABNZBD_CONNECTIONS is clamped to
USENET_ACCOUNT_CONNECTIONS - ALTMOUNT_CONNECTIONS - NNTP_PRIMARY_CONNS; with no room left, or any of the
numbers unset, no server entry is written and an existing [[usenet]] entry is switched off (enable = 0):
a download app with no server is harmless; one that takes streams' connections is not.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

MBIT_TO_BYTES = 1_000_000 / 8  # megabit/s (decimal, as line speeds are quoted) to bytes/s
SERVER_SECTION = "usenet"


def bandwidth_max(mbit) -> str:
    """SABnzbd's bandwidth_max: bytes per second as a plain integer. 0 or empty = unlimited ('')."""
    try:
        m = float(mbit)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    return str(int(round(m * MBIT_TO_BYTES)))


def connections(wanted, altmount, limit, stat=0):
    """(connections for SABnzbd, note). None means no server entry. Never exceeds limit - altmount - stat."""
    try:
        w, a, lim, st = int(wanted), int(altmount), int(limit), int(stat)
    except (TypeError, ValueError):
        return None, "SABNZBD_CONNECTIONS, ALTMOUNT_CONNECTIONS and USENET_ACCOUNT_CONNECTIONS must all be numbers"
    if w < 1 or a < 0 or lim < 1 or st < 0:
        return None, "connection settings out of range"
    room = lim - a - st
    if room < 1:
        return None, "no connections left for SABnzbd under the account limit"
    if w > room:
        return room, f"SABNZBD_CONNECTIONS clamped from {w} to {room} (account limit {lim}, AltMount {a}, STAT checks {st})"
    return w, ""


def _quote(v: str) -> str:
    """A configobj value: bare when safe, else quoted with the quote the value does not contain."""
    if v == "":
        return '""'
    if not re.search(r"""[,"'#=\[\]\s]""", v) and v == v.strip():
        return v
    if '"' not in v:
        return f'"{v}"'
    if "'" not in v:
        return f"'{v}'"
    raise ValueError("a value contains both quote characters")


_HDR = re.compile(r"^\s*(\[+)\s*([^\]]*?)\s*(\]+)\s*$")


def _header(line):
    m = _HDR.match(line)
    return (len(m.group(1)), m.group(2)) if m else None


def _find(lines, names):
    """(start, end) of the body of the section path `names` (after its header, before the next header),
    or None. A parent section's body ends at its first child header."""
    stack = []
    for i, ln in enumerate(lines):
        h = _header(ln)
        if h is None:
            continue
        depth, name = h
        stack = stack[: depth - 1] + [name]
        if tuple(stack) == tuple(names) and depth == len(names):
            j = i + 1
            while j < len(lines) and _header(lines[j]) is None:
                j += 1
            return i + 1, j
    return None


def _subtree_end(lines, names):
    """Index just past the last line of section `names` and all its children."""
    start, _ = _find(lines, names)
    depth = len(names)
    for j in range(start, len(lines)):
        h = _header(lines[j])
        if h is not None and h[0] <= depth:
            return j
    return len(lines)


def _ensure(lines, names):
    for n in range(1, len(names) + 1):
        if _find(lines, names[:n]) is None:
            header = "[" * n + names[n - 1] + "]" * n
            if n == 1:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(header)
            else:
                at = _subtree_end(lines, names[: n - 1])
                while at > 0 and not lines[at - 1].strip():
                    at -= 1
                lines[at:at] = [header]
    return _find(lines, names)


def set_keys(lines, names, kv):
    """Set kv (key -> raw configobj value) in section `names`, creating it. Returns the count changed."""
    start, end = _ensure(lines, names)
    changed = 0
    for k, v in kv.items():
        want = f"{k} = {v}"
        for i in range(start, end):
            m = re.match(r"^\s*([^=#\s][^=]*?)\s*=", lines[i])
            if m and m.group(1) == k:
                if lines[i].strip() != want:
                    lines[i] = want
                    changed += 1
                break
        else:
            lines.insert(end, want)
            end += 1
            changed += 1
    return changed


def _switch_off(lines, rep):
    """No room (or no credentials): an earlier [[usenet]] entry must not keep taking connections."""
    if _find(lines, ("servers", SERVER_SECTION)) is not None:
        rep["changed"] += set_keys(lines, ("servers", SERVER_SECTION), {"enable": "0"})
        rep["notes"].append("existing server entry switched off (enable = 0)")


def render(text: str, env) -> tuple[str, dict]:
    """The new ini text for `env`, and a report with key names and counts only."""
    lines = text.splitlines()
    rep = {"changed": 0, "notes": [], "server": False}

    def put(names, kv):
        rep["changed"] += set_keys(lines, names, kv)

    misc = {}
    mbit = env.get("SABNZBD_MAX_MBIT", "750")
    bw = bandwidth_max(mbit if str(mbit).strip() != "" else "750")
    misc["bandwidth_max"] = _quote(bw) if bw else '""'
    misc["bandwidth_perc"] = "100"
    if env.get("SABNZBD_API_KEY"):
        misc["api_key"] = _quote(env["SABNZBD_API_KEY"])
    if env.get("SABNZBD_COMPLETE_DIR"):
        misc["complete_dir"] = _quote(env["SABNZBD_COMPLETE_DIR"])
    if env.get("SABNZBD_INCOMPLETE_DIR"):
        misc["download_dir"] = _quote(env["SABNZBD_INCOMPLETE_DIR"])
    # reached by container name or address: the whitelist stays at its own default, only the bind is set
    misc["host"] = "0.0.0.0"
    put(("misc",), misc)

    for cat in (env.get("SABNZBD_CATEGORY_RADARR") or "radarr", env.get("SABNZBD_CATEGORY_SONARR") or "sonarr"):
        put(("categories", cat), {"name": _quote(cat), "dir": _quote(cat), "priority": "-100"})

    host, user = env.get("NNTP_PRIMARY_HOST", ""), env.get("NNTP_PRIMARY_USER", "")
    if host and user and env.get("NNTP_PRIMARY_PASS"):
        n, note = connections(env.get("SABNZBD_CONNECTIONS"), env.get("ALTMOUNT_CONNECTIONS"),
                              env.get("USENET_ACCOUNT_CONNECTIONS"), env.get("NNTP_PRIMARY_CONNS") or 8)
        if note:
            rep["notes"].append(note)
        if n is not None:
            port = env.get("NNTP_PRIMARY_PORT") or "563"
            put(("servers", SERVER_SECTION), {
                "name": _quote(SERVER_SECTION), "displayname": _quote(SERVER_SECTION),
                "host": _quote(host), "port": _quote(port),
                "username": _quote(user), "password": _quote(env["NNTP_PRIMARY_PASS"]),
                "connections": str(n), "ssl": "0" if port == "119" else "1", "ssl_verify": "2",
                "enable": "1", "optional": "0", "required": "0", "priority": "0", "timeout": "60",
            })
            rep["server"] = True
        else:
            _switch_off(lines, rep)
    else:
        rep["notes"].append("no primary server in the environment: no server entry written")
        _switch_off(lines, rep)
    return "\n".join(lines) + "\n", rep


def write_private(path: str, text: str, uid=None, gid=None) -> bool:
    """Write `text` to `path` with mode 600, owned by uid:gid when given (this runs as root and SABnzbd runs
    as another user), through a temp file. False when nothing changed."""
    try:
        with open(path, encoding="utf-8") as f:
            st = os.stat(path)
            if f.read() == text and (st.st_mode & 0o777) == 0o600 \
                    and (os.geteuid() != 0 or ((uid is None or st.st_uid == uid) and (gid is None or st.st_gid == gid))):
                return False
    except OSError:
        pass
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sabnzbd-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        if uid is not None and gid is not None:
            try:
                os.fchown(fd, uid, gid)
            except PermissionError:
                pass  # not root (a test run): the file stays with the caller
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(argv=None, env=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    path = argv[0] if argv else os.path.join(env.get("SABNZBD_CONFIG_DIR", "/config"), "sabnzbd.ini")
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    try:
        new, rep = render(old, env)
    except ValueError as e:
        print(f"render_sabnzbd_ini: {e}")
        return 1
    try:
        uid, gid = int(env.get("PUID") or 99), int(env.get("PGID") or 100)
    except ValueError:
        uid, gid = 99, 100
    wrote = write_private(path, new, uid, gid)
    print(f"render_sabnzbd_ini: {'updated' if wrote else 'unchanged'}, {rep['changed']} keys set, "
          f"server entry {'written' if rep['server'] else 'not written'}")
    for n in rep["notes"]:
        print(f"render_sabnzbd_ini: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
