"""AltMount clients: NZB import and a WebDAV reader.

Import goes through AltMount's SABnzbd-compatible endpoint (``/sabnzbd/api``),
which authenticates with the API key and takes the NZB as an upload. The
``/import/file`` endpoint is not used: it needs the NZB on AltMount's own disk,
and its ``target_path`` option deletes whatever is at that path. We never use
``target_path`` in any form.

AltMount zero-fills missing segments and still answers 206, so a successful
read proves nothing. ``WebDavReader.sha256_full`` exists so callers can compare
a full-read hash with the local one.

Nothing here logs keys, passwords or URLs. Error messages carry only the HTTP
status and a short reason.

Environment:
  ALTMOUNT_URL         base URL of the AltMount API
  ALTMOUNT_API_KEY     API key (32 characters when set through key_override)
  ALTMOUNT_MOUNT_PATH  optional: AltMount's mount path, stripped from the path
                       it reports so the result is relative to the WebDAV root
  ALTMOUNT_CATEGORY    optional import category
  TIERFS_REMOTE_BASE / TIERFS_REMOTE_USER / TIERFS_REMOTE_PASS  WebDAV
  TIERING_ALTMOUNT_ALLOW_HTTP=1  allow plain http to a host that is not loopback
                       (a trusted LAN only; the key and password are sent in clear).
                       Without it, https is required except to localhost.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

MEDIA_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".wmv")
CHUNK = 1 << 20
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class AltMountError(Exception):
    pass


class AltMountAuthError(AltMountError):
    pass


class ImportFailed(AltMountError):
    pass


class ImportTimeout(AltMountError):
    pass


class _Http:
    """Tiny http.client wrapper. Keeps the base URL private."""

    def __init__(self, base: str, timeout: float = 60.0):
        u = urllib.parse.urlsplit(base)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise AltMountError("bad base url")
        if (u.scheme == "http" and u.hostname not in _LOCAL_HOSTS
                and os.environ.get("TIERING_ALTMOUNT_ALLOW_HTTP", "") not in ("1", "true", "yes")):
            raise AltMountError("plain http is only allowed to localhost")
        self._https = u.scheme == "https"
        self._host = u.hostname
        self._port = u.port or (443 if self._https else 80)
        self._prefix = u.path.rstrip("/")
        self._timeout = timeout
        self._ctx = None
        if self._https:
            self._ctx = ssl.create_default_context()
            self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def open(self, method, path, headers=None, body=None):
        if self._https:
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout,
                                               context=self._ctx)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, self._prefix + path, body=body, headers=headers or {})
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise AltMountError(f"request failed: {type(e).__name__}") from None


def _quote_path(p: str) -> str:
    if not p.startswith("/"):
        p = "/" + p
    return urllib.parse.quote(p, safe="/")


# ---------------------------------------------------------------- WebDAV

class WebDavReader:
    """RemoteReader over WebDAV. ``remote_path`` is relative to the WebDAV base."""

    def __init__(self, base: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: float = 60.0):
        base = base if base is not None else os.environ.get("TIERFS_REMOTE_BASE", "")
        user = user if user is not None else os.environ.get("TIERFS_REMOTE_USER", "")
        password = password if password is not None else os.environ.get("TIERFS_REMOTE_PASS", "")
        if not base:
            raise AltMountError("TIERFS_REMOTE_BASE is not set")
        self._http = _Http(base, timeout)
        self._auth = None
        if user or password:
            tok = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._auth = "Basic " + tok

    def _headers(self, extra=None):
        h = dict(extra or {})
        if self._auth:
            h["Authorization"] = self._auth
        return h

    def _check(self, resp, ok=(200, 206, 207)):
        if resp.status in (401, 403):
            raise AltMountAuthError(f"webdav auth refused ({resp.status})")
        if resp.status not in ok:
            raise AltMountError(f"webdav status {resp.status}")

    def read_range(self, remote_path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        rng = f"bytes={offset}-{offset + length - 1}"
        conn, resp = self._http.open("GET", _quote_path(remote_path),
                                     self._headers({"Range": rng}))
        try:
            if resp.status == 416:
                return b""
            self._check(resp, ok=(206,))
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise AltMountError(f"read failed: {type(e).__name__}") from None
        finally:
            conn.close()
        if len(data) > length:
            raise AltMountError("server returned more than requested")
        return data

    def size(self, remote_path: str) -> int:
        conn, resp = self._http.open("HEAD", _quote_path(remote_path), self._headers())
        try:
            self._check(resp, ok=(200,))
            n = resp.getheader("Content-Length")
            if n is None:
                raise AltMountError("no content length")
            try:
                return int(n)
            except ValueError:
                raise AltMountError("bad content length") from None
        finally:
            conn.close()

    def sha256_full(self, remote_path: str) -> str:
        """Stream the whole file and return its SHA-256 hex digest."""
        conn, resp = self._http.open("GET", _quote_path(remote_path), self._headers())
        h = hashlib.sha256()
        got = 0
        try:
            self._check(resp, ok=(200,))
            want = resp.getheader("Content-Length")
            while True:
                b = resp.read(CHUNK)
                if not b:
                    break
                h.update(b)
                got += len(b)
        except (OSError, http.client.HTTPException) as e:
            raise AltMountError(f"read failed: {type(e).__name__}") from None
        finally:
            conn.close()
        if want is not None and got != int(want):
            raise AltMountError("short read")
        return h.hexdigest()

    def listdir(self, remote_dir: str) -> list[dict]:
        """PROPFIND depth 1. Returns [{name, path, size, is_dir}]."""
        body = (b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop>'
                b"<getcontentlength/><resourcetype/></prop></propfind>")
        d = remote_dir if remote_dir.endswith("/") else remote_dir + "/"
        conn, resp = self._http.open(
            "PROPFIND", _quote_path(d),
            self._headers({"Depth": "1", "Content-Type": "application/xml"}), body)
        try:
            self._check(resp, ok=(207,))
            raw = resp.read()
        finally:
            conn.close()
        ns = {"d": "DAV:"}
        out = []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise AltMountError("bad webdav listing") from None
        for r in root.findall("d:response", ns):
            href = urllib.parse.unquote(r.findtext("d:href", "", ns))
            href = urllib.parse.urlsplit(href).path
            is_dir = r.find(".//d:resourcetype/d:collection", ns) is not None
            ln = r.findtext(".//d:getcontentlength", "0", ns) or "0"
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if is_dir and href.rstrip("/").endswith(d.rstrip("/")):
                continue  # the directory itself
            try:
                size = int(ln)
            except ValueError:
                raise AltMountError("bad content length in listing") from None
            out.append({"name": name, "path": d + name, "size": size, "is_dir": is_dir})
        return out

    def find_media(self, remote_path: str) -> str:
        """If ``remote_path`` is a directory, return its largest media file."""
        if remote_path.lower().endswith(MEDIA_EXTS):
            return remote_path
        files = [e for e in self.listdir(remote_path)
                 if not e["is_dir"] and e["name"].lower().endswith(MEDIA_EXTS)]
        if not files:
            raise ImportFailed("no media file in imported folder")
        return max(files, key=lambda e: e["size"])["path"]


# ---------------------------------------------------------------- import

def _safe_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name).strip(" .") or "release"
    return n[:150]


class ImportClient:
    """Import an NZB through AltMount and wait for the WebDAV path."""

    def __init__(self, url: str | None = None, apikey: str | None = None, *,
                 reader: WebDavReader | None = None, mount_path: str | None = None,
                 category: str | None = None, poll_interval: float = 2.0,
                 timeout: float = 1800.0, sleep=time.sleep, clock=time.monotonic,
                 http_timeout: float = 60.0):
        url = url if url is not None else os.environ.get("ALTMOUNT_URL", "")
        apikey = apikey if apikey is not None else os.environ.get("ALTMOUNT_API_KEY", "")
        if not url or not apikey:
            raise AltMountError("ALTMOUNT_URL and ALTMOUNT_API_KEY must be set")
        self._http = _Http(url, http_timeout)
        self._key = apikey
        self._reader = reader
        self._mount = (mount_path if mount_path is not None
                       else os.environ.get("ALTMOUNT_MOUNT_PATH", "")).rstrip("/")
        self._cat = category if category is not None else os.environ.get("ALTMOUNT_CATEGORY", "")
        self._poll = poll_interval
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock

    def _json(self, resp, conn):
        try:
            if resp.status in (401, 403):
                raise AltMountAuthError(f"altmount auth refused ({resp.status})")
            raw = resp.read()
            if resp.status != 200:
                raise AltMountError(f"altmount status {resp.status}")
            return json.loads(raw)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise AltMountError(f"bad response: {type(e).__name__}") from None
        finally:
            conn.close()

    def _add(self, nzb_bytes: bytes, release_name: str, job_id: str) -> None:
        boundary = uuid.uuid4().hex
        fname = _safe_name(release_name) + ".nzb"
        fields = {"apikey": self._key, "mode": "addfile", "nzbname": job_id}
        if self._cat:
            fields["cat"] = self._cat
        parts = []
        for k, v in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="nzbfile"; '
                      f'filename="{fname}"\r\nContent-Type: application/x-nzb\r\n\r\n').encode())
        parts.append(nzb_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        conn, resp = self._http.open(
            "POST", "/sabnzbd/api?output=json",
            {"Content-Type": f"multipart/form-data; boundary={boundary}"}, b"".join(parts))
        data = self._json(resp, conn)
        if not data.get("status"):
            raise ImportFailed("altmount refused the nzb")
        if job_id not in (data.get("nzo_ids") or []):
            raise ImportFailed("altmount did not echo the job id")

    def _history(self, job_id: str) -> dict | None:
        # The key goes in the body, never in the URL. AltMount reads ``nzo_ids`` (and
        # ``mode``) from the query string, so those two are sent there.
        q = urllib.parse.urlencode({"mode": "history", "output": "json", "nzo_ids": job_id})
        body = urllib.parse.urlencode({"mode": "history", "output": "json",
                                       "apikey": self._key}).encode()
        conn, resp = self._http.open(
            "POST", f"/sabnzbd/api?{q}",
            {"Content-Type": "application/x-www-form-urlencoded"}, body)
        data = self._json(resp, conn)
        for s in (data.get("history") or {}).get("slots") or []:
            if s.get("nzo_id") == job_id:
                return s
        return None

    def import_nzb(self, nzb_bytes: bytes, release_name: str) -> str:
        """Import and wait. Returns the WebDAV path of the media file.

        Raises ImportFailed if AltMount marks the job failed, ImportTimeout if
        it is not done in time. The caller keeps its own archive copy of the NZB.
        """
        job_id = uuid.uuid4().hex
        self._add(nzb_bytes, release_name, job_id)
        deadline = self._clock() + self._timeout
        while True:
            slot = self._history(job_id)
            if slot:
                st = (slot.get("status") or "").lower()
                if st == "completed":
                    return self._remote_path(slot)
                if st == "failed":
                    raise ImportFailed("altmount import failed")
            if self._clock() >= deadline:
                raise ImportTimeout("altmount import not done in time")
            self._sleep(self._poll)

    def _remote_path(self, slot: dict) -> str:
        p = (slot.get("storage") or slot.get("path") or "").replace("\\", "/")
        if not p:
            raise ImportFailed("altmount reported no path")
        if self._mount and p.startswith(self._mount + "/"):
            p = p[len(self._mount):]
        elif self._mount and p == self._mount:
            p = "/"
        if not p.startswith("/"):
            p = "/" + p
        if self._reader is not None:
            p = self._reader.find_media(p)
        return p
