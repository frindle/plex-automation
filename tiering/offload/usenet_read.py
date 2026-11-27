"""Byte ranges of a posted file, read straight from a provider and checked by CRC.

qualify reads the remote copy back through AltMount. When a chunk comes back wrong on
every read, that is either a copy that is not ``L`` or AltMount serving wrong bytes for
a while (seen live: the same wrong bytes for several seconds under concurrent range
reads, on articles whose CRC-checked bytes are ``L``'s). ``UsenetFile.read`` settles
which: it fetches the articles that cover the range with BODY, decodes them, checks each
part's CRC32 and returns the bytes the upload really holds.

It only answers for an upload that posts the file itself: the NZB must hold a file
whose yEnc size is the local size, cut into parts of one size (all but the last). A
packed upload (RAR volumes) or anything unexpected gives None ("cannot tell"), and so
does a provider that cannot be asked. It never raises for those.
"""
from __future__ import annotations

import io
import threading
import zlib
from xml.etree.ElementTree import ParseError, iterparse

from .nntp_stat import NntpUnavailable, ProviderCfg, _Conn

MAX_FILES_PROBED = 3  # NZB files (largest first) whose first part is fetched to find L
_T = bytes((i - 42) & 0xFF for i in range(256))


class CannotTell(Exception):
    """The range cannot be read from the provider. Never a verdict on the copy."""


def _kv(line: bytes) -> dict:
    """``=ybegin part=1 total=9 line=128 size=123 name=a b.mkv`` -> {"part": "1", ...}.
    ``name`` is last and may hold spaces, so it is cut off first."""
    s = line.decode("latin-1")
    head, sep, name = s.partition(" name=")
    out = {}
    for tok in head.split()[1:]:
        k, eq, v = tok.partition("=")
        if eq:
            out[k] = v
    if sep:
        out["name"] = name
    return out


def ydecode(lines: list[bytes]) -> tuple[dict, bytes]:
    """Decode one yEnc part. Returns (info, data) where info holds size, part, begin and
    end (1-based, inclusive) and the part's CRC32 when the poster gave one. Raises
    CannotTell when the part is malformed or its CRC is wrong."""
    begin = part = end = None
    out = bytearray()
    for ln in lines:
        if ln.startswith(b"=ybegin "):
            begin = _kv(ln)
        elif ln.startswith(b"=ypart "):
            part = _kv(ln)
        elif ln.startswith(b"=yend"):
            end = _kv(ln)
            break
        elif begin is not None:
            i = 0
            while True:
                j = ln.find(b"=", i)
                if j < 0:
                    out += ln[i:].translate(_T)
                    break
                out += ln[i:j].translate(_T)
                if j + 1 < len(ln):
                    out.append((ln[j + 1] - 106) & 0xFF)
                i = j + 2
    if begin is None or end is None:
        raise CannotTell("not a whole yEnc part")
    try:
        size = int(begin["size"])
        b, e = (int(part["begin"]), int(part["end"])) if part else (1, size)
        if e - b + 1 != len(out) or ("size" in end and int(end["size"]) != len(out)):
            raise CannotTell("part length")
        crc = end.get("pcrc32") or (end.get("crc32") if part is None else None)
        if crc is None:
            raise CannotTell("no part crc")
        if zlib.crc32(out) != int(crc, 16):
            raise CannotTell("part crc")
    except (KeyError, ValueError, TypeError):
        raise CannotTell("bad yEnc header") from None
    return {"size": size, "begin": b, "end": e}, bytes(out)


def nzb_files(nzb_bytes: bytes) -> list[list[str]]:
    """Each NZB file's message-ids in part order. A file whose numbers are not 1..n
    is left out: its parts cannot be placed."""
    files = []
    try:
        segs: list = []
        for ev, el in iterparse(io.BytesIO(nzb_bytes), events=("start", "end")):
            tag = el.tag.rsplit("}", 1)[-1]
            if ev == "start" and tag == "file":
                segs = []
            elif ev == "end" and tag == "segment":
                t = (el.text or "").strip()
                try:
                    segs.append((int(el.get("number", "0")), int(el.get("bytes", "0")), t))
                except ValueError:
                    segs.append((0, 0, t))
            elif ev == "end" and tag == "file":
                segs.sort()
                if segs and [s[0] for s in segs] == list(range(1, len(segs) + 1)) and \
                        all(s[2] for s in segs):
                    files.append((sum(s[1] for s in segs), [s[2] for s in segs]))
                el.clear()
    except ParseError:
        return []
    files.sort(key=lambda f: -f[0])
    return [ids for _, ids in files]


class UsenetFile:
    """``read(off, n)`` -> the posted file's bytes at [off, off + n), from one provider
    connection (opened on first use, shared by threads under a lock). Raises CannotTell
    when it cannot answer."""

    def __init__(self, nzb_bytes: bytes, cfg: ProviderCfg, size: int, conn_factory=_Conn):
        self.nzb_bytes, self.cfg, self.size = nzb_bytes, cfg, size
        self.conn_factory = conn_factory
        self.mu = threading.Lock()
        self.conn = None
        self.ids: list[str] | None = None  # the file's parts, once found
        self.part = 0  # bytes per part (all but the last)
        self.failed = None  # why it cannot answer, once known

    def _body(self, mid: str) -> list[bytes]:
        for attempt in range(2):  # a dropped link gets one fresh connection
            if self.conn is None:
                c = self.conn_factory(self.cfg)
                c.open()
                self.conn = c
            try:
                lines = self.conn.body(mid)
            except NntpUnavailable as e:
                self._drop()
                if e.kind != "network" or attempt:
                    raise
                continue
            if lines is None:
                raise CannotTell("article not on the provider")
            return lines
        raise CannotTell("unreachable")

    def _drop(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _find(self):
        files = nzb_files(self.nzb_bytes)
        for ids in files[:MAX_FILES_PROBED]:
            info, data = ydecode(self._body(ids[0]))
            if info["size"] != self.size:
                continue
            if info["begin"] != 1 or not data:
                raise CannotTell("first part does not start the file")
            p = len(data)
            if (len(ids) - 1) * p >= self.size or len(ids) * p < self.size:
                raise CannotTell("parts are not one size")
            self.ids, self.part = ids, p
            return
        raise CannotTell("no posted file of this size")

    def read(self, off: int, n: int) -> bytes:
        with self.mu:
            if self.failed:
                raise CannotTell(self.failed)
            try:
                if self.ids is None:
                    self._find()
                if off < 0 or n <= 0 or off + n > self.size:
                    raise CannotTell("range outside the file")
                p = self.part
                out = bytearray()
                for k in range(off // p, (off + n - 1) // p + 1):
                    info, data = ydecode(self._body(self.ids[k]))
                    if info["begin"] != k * p + 1 or info["size"] != self.size:
                        raise CannotTell("part out of place")
                    out += data
                lo = off - (off // p) * p
                return bytes(out[lo:lo + n])
            except NntpUnavailable as e:
                self.failed = f"provider {e.kind}"  # do not keep knocking tonight
                raise CannotTell(self.failed) from None
            except CannotTell as e:
                if self.ids is None:
                    self.failed = str(e)  # no file to read: the same for every chunk
                raise

    def close(self):
        with self.mu:
            self._drop()
