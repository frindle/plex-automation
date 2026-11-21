"""Minimal NNTP client for STAT checks over TLS.

``nntplib`` is gone in Python 3.13, so this speaks the few commands it needs
over ``socket`` and ``ssl``: AUTHINFO USER/PASS, then STAT <message-id>.

STATs are pipelined: a whole batch is written, then the replies are read in
order. Each connection handles one batch at a time and the pool runs
``conns`` connections in parallel, so a week-long run over tens of millions of
segments needs only a few dozen STATs a second.

Provider settings come from the environment, never from the repo:
  NNTP_PRIMARY_HOST / _PORT / _USER / _PASS / _CONNS   (and the same for SECONDARY)
  NNTP_<NAME>_TLS=0 turns TLS off (tests only). It is refused unless the host is
  127.0.0.1 or localhost. TLS is 1.2 or newer.

Errors that mean "could not ask" (login refused, quota, dropped link) raise
``NntpUnavailable`` with a ``kind``. They are never reported as missing
segments.
"""
from __future__ import annotations

import collections
import gzip
import os
import queue
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

BACKOFF_S = 1.0  # first retry waits this long, then double
_sleep = time.sleep


class NntpUnavailable(Exception):
    """The provider could not be asked. kind: auth, quota, network or protocol."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


@dataclass(frozen=True)
class ProviderCfg:
    host: str
    port: int = 563
    user: str = ""
    password: str = ""
    conns: int = 8
    tls: bool = True
    cafile: str | None = None
    timeout: float = 30.0
    batch: int = 100

    def __post_init__(self):
        # Without TLS the login goes out in cleartext: only the local machine may do that.
        if not self.tls and self.host not in ("127.0.0.1", "localhost"):
            raise ValueError("NNTP TLS can only be turned off for 127.0.0.1 or localhost")

    def __repr__(self) -> str:  # never show host or credentials
        return f"ProviderCfg(conns={self.conns}, tls={self.tls})"

    @classmethod
    def from_env(cls, name: str, env=None) -> "ProviderCfg | None":
        e = os.environ if env is None else env
        p = f"NNTP_{name.upper()}_"
        host = e.get(p + "HOST", "")
        if not host:
            return None
        return cls(host=host, port=int(e.get(p + "PORT", "563") or 563),
                   user=e.get(p + "USER", ""), password=e.get(p + "PASS", ""),
                   conns=int(e.get(p + "CONNS", "8") or 8),
                   tls=e.get(p + "TLS", "1") not in ("0", "false", "no"),
                   cafile=e.get(p + "CAFILE") or None)


_AUTH_CODES = {"480", "481", "482", "502"}
_QUOTA_CODES = {"400", "403"}


def _fail_for(code: str, what: str) -> NntpUnavailable:
    if code in _AUTH_CODES:
        return NntpUnavailable("auth", f"{what} {code}")
    if code in _QUOTA_CODES:
        return NntpUnavailable("quota", f"{what} {code}")
    return NntpUnavailable("protocol", f"{what} {code}")


class _Conn:
    def __init__(self, cfg: ProviderCfg):
        self.cfg = cfg
        self.sock = None
        self.f = None

    def open(self):
        cfg = self.cfg
        try:
            s = socket.create_connection((cfg.host, cfg.port), cfg.timeout)
            if cfg.tls:
                ctx = ssl.create_default_context(cafile=cfg.cafile)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                s = ctx.wrap_socket(s, server_hostname=cfg.host)
            s.settimeout(cfg.timeout)
            self.sock = s
            self.f = s.makefile("rb")
            code = self._line()[:3]
            if code not in ("200", "201"):
                raise _fail_for(code, "greeting")
            if cfg.user:
                self._send(f"AUTHINFO USER {cfg.user}")
                code = self._line()[:3]
                if code == "381":
                    self._send(f"AUTHINFO PASS {cfg.password}")
                    code = self._line()[:3]
                if code != "281":
                    raise NntpUnavailable("auth", f"login {code}")
        except NntpUnavailable:
            self.close()
            raise
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise NntpUnavailable("network", type(e).__name__) from None

    def _send(self, line: str):
        self.sock.sendall(line.encode("ascii", "replace") + b"\r\n")

    def _line(self) -> str:
        b = self.f.readline(4096)
        if not b:
            raise ConnectionError("closed")
        return b.decode("ascii", "replace").strip()

    def stat_batch(self, ids: list[str]) -> list[bool]:
        """Return True for each id the server has."""
        out: list[bool | None] = [None] * len(ids)
        cmds = []
        for i, mid in enumerate(ids):
            mid = mid.strip().strip("<>")
            if not mid or any(c in mid for c in " \r\n\t\0"):
                out[i] = False  # cannot be a real message-id
                continue
            cmds.append(i)
        try:
            self.sock.sendall(b"".join(
                f"STAT <{ids[i].strip().strip('<>')}>\r\n".encode("ascii", "replace")
                for i in cmds))
            for i in cmds:
                line = self._line()
                code = line[:3]
                if code == "223":
                    want = ids[i].strip().strip("<>")
                    parts = line.split()  # "223 <n> <message-id> [text]"
                    got = parts[2].strip("<>") if len(parts) > 2 else None
                    if got != want:  # a reply that is not for this id: out of step
                        raise NntpUnavailable("protocol", "stat reply for another id")
                    out[i] = True
                elif code == "430":
                    out[i] = False
                else:
                    raise _fail_for(code, "stat")
        except (OSError, ssl.SSLError, ConnectionError) as e:
            raise NntpUnavailable("network", type(e).__name__) from None
        return [bool(x) for x in out]

    def close(self):
        try:
            if self.sock:
                try:
                    self._send("QUIT")
                except OSError:
                    pass
                self.sock.close()
        except OSError:
            pass
        self.sock = self.f = None


class NntpPool:
    """Lazy pool of authenticated connections to one provider."""

    def __init__(self, cfg: ProviderCfg):
        self.cfg = cfg
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._sem = threading.Semaphore(max(1, cfg.conns))
        self._all: list[_Conn] = []
        self._lock = threading.Lock()

    def stat_batch(self, ids: list[str], retries: int = 2) -> list[bool]:
        last: NntpUnavailable | None = None
        for attempt in range(retries + 1):
            if attempt:  # back off before a retry, outside the connection limit
                _sleep(BACKOFF_S * 2 ** (attempt - 1))
            with self._sem:
                try:
                    conn = self._idle.get_nowait()
                except queue.Empty:
                    conn = _Conn(self.cfg)
                    try:
                        conn.open()
                    except NntpUnavailable as e:
                        if e.kind != "network":
                            raise
                        last = e
                        continue
                    with self._lock:
                        self._all.append(conn)
                try:
                    res = conn.stat_batch(ids)
                except NntpUnavailable as e:
                    conn.close()
                    with self._lock:
                        if conn in self._all:
                            self._all.remove(conn)
                    if e.kind != "network":
                        raise
                    last = e
                    continue
                self._idle.put(conn)
                return res
        raise last or NntpUnavailable("network")

    def close(self):
        with self._lock:
            conns, self._all = self._all, []
        for c in conns:
            c.close()
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def iter_nzb_ids(path: str):
    """Yield the message-id of every segment in an NZB (plain or gzipped)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag.rsplit("}", 1)[-1] == "segment":
                t = (el.text or "").strip()
                if t:
                    yield t
                el.clear()


def check_ids(ids, pool: NntpPool) -> dict:
    """STAT every id. Returns {total, missing, missing_ids[:100]}."""
    cfg = pool.cfg
    total = missing = 0
    missing_ids: list[str] = []
    window = max(2, cfg.conns * 2)
    pending: collections.deque = collections.deque()

    def drain(block_all=False):
        nonlocal total, missing
        while pending and (block_all or len(pending) >= window):
            fut, batch = pending.popleft()
            res = fut.result()  # raises NntpUnavailable
            total += len(batch)
            for mid, ok in zip(batch, res):
                if not ok:
                    missing += 1
                    if len(missing_ids) < 100:
                        missing_ids.append(mid)

    ex = ThreadPoolExecutor(max_workers=max(1, cfg.conns))
    try:
        batch: list[str] = []
        for mid in ids:
            batch.append(mid)
            if len(batch) >= cfg.batch:
                pending.append((ex.submit(pool.stat_batch, batch), batch))
                batch = []
                drain()
        if batch:
            pending.append((ex.submit(pool.stat_batch, batch), batch))
        drain(block_all=True)
    except BaseException:
        for fut, _ in pending:
            fut.cancel()
        raise
    finally:
        ex.shutdown(wait=True)
    return {"total": total, "missing": missing, "missing_ids": missing_ids}


def check_nzb(nzb_path: str, provider_cfg: ProviderCfg, pool: NntpPool | None = None) -> dict:
    """STAT every segment of an NZB on one provider."""
    own = pool is None
    pool = pool or NntpPool(provider_cfg)
    try:
        return check_ids(iter_nzb_ids(nzb_path), pool)
    finally:
        if own:
            pool.close()


class Checker:
    """Callable ``checker(nzb_path) -> dict`` that keeps its connections open."""

    def __init__(self, cfg: ProviderCfg):
        self.pool = NntpPool(cfg)
        self.cfg = cfg

    def __call__(self, nzb_path: str) -> dict:
        return check_nzb(nzb_path, self.cfg, self.pool)

    def close(self):
        self.pool.close()
