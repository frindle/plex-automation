"""Switch a player that is watching an offloaded file onto the restore-path release, mid-stream.

``python -m offload.runner --switch-live <id>`` (a dry run unless TIERING_APPLY=1) and
``--switch-live <id> --check`` (read-only: lists the sessions on the row and whether each player
accepts remote control).

Steps (``switch_live``):
 1. Plex ``/status/sessions``: the sessions whose part file is the row's library file. Per session
    the player's machineIdentifier, the viewOffset and the item's ratingKey. None: nothing to do.
 2. Each player is probed for remote control (``player_control``). A player that cannot be
    controlled is left on the old file; when none can be, nothing is restored.
 3. The restore (``deps.restore``, ops.restore_from_path) unless the row is already restored. When
    it returns the new file is whole on disk and verified, so it is pre-buffered by construction:
    the switch never fetches anything.
 4. Wait until Plex's metadata for the item shows a part of the new file's size (the partial scan
    ran), with a timeout.
 5. Per session: re-read viewOffset, map it onto the new release (watch_hold.map_offset, the
    sound around the point), and send ``playMedia`` at the mapped offset through the server's
    player proxy. A failed mapping or command leaves that player on the old file (its open
    file keeps working until the stub is cleaned up) and is reported.

The Plex remote-control shape follows python-plexapi (``PlexClient.playMedia`` / ``sendCommand``
with ``proxyThroughServer``): ``GET <server>/player/playback/playMedia`` with the header
``X-Plex-Target-Client-Identifier: <player machineIdentifier>`` and the query ``key``,
``offset``, ``machineIdentifier``, ``protocol``, ``address``, ``port``, ``type``, ``token``,
``containerKey`` (a play queue made with ``POST /playQueues``), ``providerIdentifier``,
``commandID``. The player reopens the file: a few seconds of rebuffer is normal.

Secrets: the Plex token goes in the ``X-Plex-Token`` header only. The one token in a query is the
short-lived delegation token the player is handed, as plexapi does. No URL is ever logged or put
in an error; messages carry the path without its query and the status code.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ops
from . import state as st

log = logging.getLogger(__name__)

TIMEOUT = 15
SCAN_TIMEOUT_S = 120.0
SCAN_POLL_S = 3.0
CONFIRM_TIMEOUT_S = 25.0
PROVIDER = "com.plexapp.plugins.library"


class PlexRCError(RuntimeError):
    """A Plex call failed. The message never holds a URL or a token."""


class Plex:
    """Minimal Plex HTTP client. `http` may be replaced (tests): http(method, path, params,
    headers) -> (status, body bytes); it raises PlexRCError on a transport failure."""

    def __init__(self, base=None, token=None, http=None):
        self.base = (base if base is not None else os.environ.get("PLEX_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("PLEX_TOKEN", "")
        if not self.base or not self.token:
            raise PlexRCError("PLEX_URL and PLEX_TOKEN must be set")
        self._http = http or self._urllib
        self.command_id = 0

    def _urllib(self, method, path, params, headers):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, r.read(4 << 20)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PlexRCError(f"{method} {path} failed: {type(e).__name__}") from None

    def call(self, method, path, params=None, target=None):
        """(status, parsed JSON or None). The token is added as a header here, never in the URL."""
        headers = {"Accept": "application/json", "X-Plex-Token": self.token}
        if target:
            headers["X-Plex-Target-Client-Identifier"] = target
        status, body = self._http(method, path, params or {}, headers)
        try:
            doc = json.loads(body) if body else None
        except ValueError:
            doc = None
        return status, doc

    def get(self, path, params=None):
        status, doc = self.call("GET", path, params)
        if status != 200:
            raise PlexRCError(f"GET {path} failed: HTTP {status}")
        return (doc or {}).get("MediaContainer", {})

    def next_command_id(self):
        self.command_id += 1
        return self.command_id


def _norm(p):
    return os.path.normpath(str(p)) if p else ""


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def find_sessions(plex, lib_path):
    """The live sessions whose part file is `lib_path` (as Plex reports it)."""
    want = _norm(lib_path)
    out = []
    for m in plex.get("/status/sessions").get("Metadata") or []:
        part = next((p for md in (m.get("Media") or []) for p in (md.get("Part") or [])
                     if _norm(p.get("file")) == want), None)
        if part is None:
            continue
        pl = m.get("Player") or {}
        if not pl.get("machineIdentifier"):
            continue
        media = next((md for md in m.get("Media") or [] if part in (md.get("Part") or [])), {})
        out.append({
            "ratingKey": str(m.get("ratingKey")),
            "machine": pl["machineIdentifier"],
            "state": pl.get("state") or "",
            "product": pl.get("product") or "",
            "view_offset_ms": _int(m.get("viewOffset")),
            "dur_ms": _int(part.get("duration") or media.get("duration") or m.get("duration")),
            "part_size": _int(part.get("size")),
            "type": m.get("type") or "movie",
        })
    return out


def session_of(plex, machine, rating_key):
    """The player's session on this item now (whichever file it reports), or None."""
    for m in plex.get("/status/sessions").get("Metadata") or []:
        if (m.get("Player") or {}).get("machineIdentifier") == machine \
                and str(m.get("ratingKey")) == str(rating_key):
            parts = [p for md in (m.get("Media") or []) for p in (md.get("Part") or [])]
            return {"state": (m.get("Player") or {}).get("state") or "",
                    "view_offset_ms": _int(m.get("viewOffset")),
                    "part_sizes": [_int(p.get("size")) for p in parts]}
    return None


def player_control(plex, machine):
    """(ok, why): can this player take a proxied playMedia? It must be listed by the server
    (`/clients`) with the `playback` capability; failing that, a proxied timeline poll that
    the player answers counts. Read-only."""
    try:
        for c in plex.get("/clients").get("Server") or []:
            if c.get("machineIdentifier") == machine:
                caps = str(c.get("protocolCapabilities") or "")
                if "playback" in caps.split(","):
                    return True, "advertises playback"
                return False, "listed but does not advertise playback"
        status, _ = plex.call("GET", "/player/timeline/poll",
                              {"wait": 0, "commandID": plex.next_command_id()}, target=machine)
    except PlexRCError as e:
        return False, str(e)
    if status == 200:
        return True, "not listed, but answers a proxied timeline poll"
    return False, f"not listed, proxied poll gave HTTP {status}"


def new_part(plex, rating_key, size):
    """(part dict, media duration ms) for the part of `size` bytes in the item's metadata, else None."""
    mc = plex.get(f"/library/metadata/{urllib.parse.quote(str(rating_key), safe='')}")
    for m in mc.get("Metadata") or []:
        for md in m.get("Media") or []:
            for p in md.get("Part") or []:
                if _int(p.get("size")) == int(size):
                    return p, _int(p.get("duration") or md.get("duration") or m.get("duration"))
    return None


def wait_for_part(plex, rating_key, size, timeout=SCAN_TIMEOUT_S, poll=SCAN_POLL_S,
                  sleep=time.sleep, clock=time.monotonic):
    end = clock() + timeout
    while True:
        try:
            hit = new_part(plex, rating_key, size)
        except PlexRCError as e:
            log.warning("switch: metadata read failed: %s", e)
            hit = None
        if hit:
            return hit
        if clock() >= end:
            return None
        sleep(poll)


def play_media(plex, session, offset_ms, server_url):
    """Send playMedia to the player through the server. Raises PlexRCError on failure."""
    mid = plex.get("/").get("machineIdentifier")
    if not mid:
        raise PlexRCError("server machineIdentifier unknown")
    rk = session["ratingKey"]
    key = f"/library/metadata/{rk}"
    u = urllib.parse.urlparse(server_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    params = {
        "providerIdentifier": PROVIDER, "machineIdentifier": mid,
        "protocol": u.scheme, "address": u.hostname or "", "port": port,
        "offset": int(offset_ms), "key": key, "type": "video",
        "commandID": plex.next_command_id(),
    }
    status, doc = plex.call("POST", "/playQueues", {
        "type": "video", "uri": f"server://{mid}/{PROVIDER}{key}", "shuffle": 0,
        "repeat": 0, "continuous": 0, "own": 1})
    qid = ((doc or {}).get("MediaContainer") or {}).get("playQueueID") if status == 200 else None
    if qid:
        params["containerKey"] = f"/playQueues/{qid}?window=100&own=1"
    else:
        log.warning("switch: no play queue (HTTP %s); sending without containerKey", status)
    status, doc = plex.call("GET", "/security/token", {"type": "delegation", "scope": "all"})
    tok = ((doc or {}).get("MediaContainer") or {}).get("token") if status == 200 else None
    if tok:
        params["token"] = tok
    else:
        log.warning("switch: no delegation token (HTTP %s); sending without one", status)
    status, _ = plex.call("GET", "/player/playback/playMedia", params, target=session["machine"])
    if status != 200:
        raise PlexRCError(f"playMedia failed: HTTP {status}")


def ffmpeg_reader(path, binary=None):
    """read(start_s, dur_s) -> mono s16le PCM, for watch_hold.map_offset. ffmpeg runs under the
    name tierfs treats as a background reader (TIERFS_BACKGROUND_COMMS), so it never promotes."""
    import subprocess
    import watch_hold
    binary = binary or os.environ.get("SWITCH_FFMPEG_BIN", "watch-align")

    def read(start_s, dur_s):
        r = subprocess.run([binary, "-nostdin", "-v", "error", "-ss", f"{max(start_s, 0):.3f}",
                            "-t", f"{dur_s:.3f}", "-i", path, "-map", "0:a:0", "-vn", "-sn",
                            "-dn", "-ac", "1", "-ar", str(watch_hold.RATE), "-f", "s16le", "-"],
                           capture_output=True, timeout=180)
        if r.returncode:
            raise RuntimeError("ffmpeg failed")
        return r.stdout
    return read


def default_map(t_s, old_path, new_path, old_dur_s, new_dur_s):
    import watch_hold
    return watch_hold.map_offset(t_s, ffmpeg_reader(old_path), ffmpeg_reader(new_path),
                                 old_dur_s, new_dur_s)


def _restored_info(db, id):
    """(name, size) of the latest restore_done event, or None."""
    done = [e for e in st.events(db, id) if e["kind"] == "restore_done"]
    if not done:
        return None
    d = done[-1]["detail"]
    return d.get("name"), _int(d.get("size"))


def check(row, plex, out=print):
    """Read-only: list the sessions on the row and whether each player accepts remote control."""
    sess = find_sessions(plex, row.get("lib_path"))
    out(f"{len(sess)} session(s) playing this row")
    for s in sess:
        ok, why = player_control(plex, s["machine"])
        out(f"  player {s['machine']} ({s['product'] or 'unknown app'}, {s['state'] or '?'}, "
            f"at {s['view_offset_ms'] // 1000}s): remote control "
            f"{'OK' if ok else 'NOT available'} ({why})")
    return sess


def switch_live(db, row, plex, *, apply, restore, path_map=None, tierfs_mount=None, server_url=None,
                mapper=default_map, sleep=time.sleep, clock=time.monotonic,
                scan_timeout=SCAN_TIMEOUT_S, confirm_timeout=CONFIRM_TIMEOUT_S):
    """Returns {'switched': [machine...], 'left': {machine: reason}, 'note': str}. Never raises
    for a Plex or player problem; a player that is not switched stays on the old file."""
    id = row["id"]
    res = {"switched": [], "left": {}, "note": ""}
    sess = find_sessions(plex, row.get("lib_path"))
    if not sess:
        res["note"] = "no session is playing this row; nothing to switch"
        log.info("switch %s: %s", id, res["note"])
        return res
    ctl = []
    for s in sess:
        ok, why = player_control(plex, s["machine"])
        if ok:
            ctl.append(s)
        else:
            res["left"][s["machine"]] = f"no remote control ({why})"
            log.warning("switch %s: player %s stays on the old file: no remote control (%s)",
                        id, s["machine"], why)
    if not ctl:
        res["note"] = "no player accepts remote control; nothing restored"
        log.warning("switch %s: %s", id, res["note"])
        return res
    server_url = server_url or plex.base
    already = row.get("state") == "failed" and row.get("restore_note") == ops.RESTORED_NOTE
    if not apply:
        for s in ctl:
            log.info("dry run switch %s: would %s then send playMedia (offset mapped from %ds, "
                     "%s) to player %s", id, "use the existing restore" if already else "restore",
                     s["view_offset_ms"] // 1000, s["state"] or "?", s["machine"])
        res["note"] = "dry run: nothing restored or sent"
        return res

    # the old file is read through tierfs: its entry is <mount>/<stub name>; without the mount
    # setting, L's symlink (taken before the swap) names it
    old_path = None
    if tierfs_mount and row.get("stub_path"):
        old_path = os.path.join(tierfs_mount, os.path.basename(row["stub_path"]))
    elif not already:
        lib, why = ops._resolve_lib(row["lib_path"], path_map)
        if not why and os.path.islink(lib):
            old_path = os.readlink(lib)
    if not already:
        r = restore(db, row, True, True)
        if not r.ok:
            res["note"] = f"restore failed: {r.reason}"
            for s in ctl:
                res["left"][s["machine"]] = "restore failed"
            log.error("switch %s: %s; players stay on the old file", id, res["note"])
            return res
    info = _restored_info(db, id)
    if not info or not info[1]:
        res["note"] = "restored, but the new file's size is unknown; not switching"
        for s in ctl:
            res["left"][s["machine"]] = "new file unknown"
        log.error("switch %s: %s", id, res["note"])
        return res
    name, size = info
    new_path, why = ops._resolve_lib(
        os.path.join(os.path.dirname(row["lib_path"]), name), path_map)
    if why or old_path is None:
        res["note"] = ("cannot find the old or new file on disk to map the offset "
                       f"({why or 'TIERFS_MOUNT is not set and the row was restored earlier'})")
        for s in ctl:
            res["left"][s["machine"]] = "files unavailable for offset mapping"
        log.error("switch %s: %s; not switching", id, res["note"])
        return res

    rk = ctl[0]["ratingKey"]
    hit = wait_for_part(plex, rk, size, scan_timeout, sleep=sleep, clock=clock)
    if not hit:
        res["note"] = "Plex did not show the new file in time"
        for s in ctl:
            res["left"][s["machine"]] = "scan timeout"
        log.error("switch %s: %s (%ds); players stay on the old file", id, res["note"],
                  int(scan_timeout))
        return res
    new_dur_s = hit[1] / 1000.0 or None

    for s in ctl:
        m = s["machine"]
        try:
            fresh = session_of(plex, m, rk)
        except PlexRCError:
            fresh = None
        if fresh is None:
            res["left"][m] = "session ended before the switch"
            log.info("switch %s: player %s stopped before the switch", id, m)
            continue
        t_s = fresh["view_offset_ms"] / 1000.0
        try:
            mp = mapper(t_s, old_path, new_path, (s["dur_ms"] / 1000.0) or None, new_dur_s)
        except Exception as e:
            mp = {"ok": False, "why": f"error {type(e).__name__}"}
        if not mp.get("ok") or mp.get("new_s") is None:
            res["left"][m] = f"offset mapping failed ({mp.get('why')})"
            log.warning("switch %s: player %s stays on the old file: offset mapping failed (%s)",
                        id, m, mp.get("why"))
            continue
        off_ms = max(0, int(mp["new_s"] * 1000))
        try:
            play_media(plex, {"machine": m, "ratingKey": rk}, off_ms, server_url)
        except PlexRCError as e:
            res["left"][m] = f"playMedia failed ({e})"
            log.warning("switch %s: player %s stays on the old file: %s", id, m, e)
            continue
        log.info("switch %s: sent playMedia to player %s (%s): %ds -> %ds", id, m,
                 fresh["state"] or "?", int(t_s), off_ms // 1000)
        res["switched"].append(m)
        end = clock() + confirm_timeout
        while clock() < end:
            try:
                now = session_of(plex, m, rk)
            except PlexRCError:
                now = None
            if now and size in now["part_sizes"]:
                break
            sleep(2.0)
        else:
            log.warning("switch %s: player %s was sent the command but is not seen on the new "
                        "file yet; check the player", id, m)
    return res
