#!/usr/bin/env python3
"""Fake WebDAV remote for the tiering e2e tests (stdlib only).

Serves files under ROOT with GET (single Range), HEAD and PROPFIND depth 0/1,
behind Basic auth. Can answer chosen byte ranges with zeros and a 206, which
imitates a missing Usenet segment. Counts requests (GET /__stats).

  fake_webdav.py --root DIR --port 8081 [--user U --password P]

Env fallbacks: FAKE_DAV_USER, FAKE_DAV_PASS.
Zero ranges are re-read on every request, from either source:
  FAKE_DAV_ZEROS="path:start-end,path:start-end"   (end exclusive, path relative to root)
  the JSON file named by FAKE_DAV_ZERO_FILE: {"rel/path": [[start, end], ...]}
"""
import argparse
import base64
import json
import os
import re
import threading
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

STATS = {"requests": 0, "by_method": {}, "by_path": {}, "bytes_served": 0, "zeroed_responses": 0}
LOCK = threading.Lock()


def zero_ranges(rel):
    out = []
    for item in filter(None, os.environ.get("FAKE_DAV_ZEROS", "").split(",")):
        p, _, rng = item.rpartition(":")
        a, _, b = rng.partition("-")
        if p == rel:
            out.append((int(a), int(b)))
    zf = os.environ.get("FAKE_DAV_ZERO_FILE")
    if zf and os.path.exists(zf):
        try:
            with open(zf) as fh:
                out += [tuple(x) for x in json.load(fh).get(rel, [])]
        except (OSError, ValueError):
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "fakedav/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _count(self):
        with LOCK:
            STATS["requests"] += 1
            m = STATS["by_method"]
            m[self.command] = m.get(self.command, 0) + 1
            p = STATS["by_path"]
            p[self.path] = p.get(self.path, 0) + 1

    def _auth(self):
        if self.server.creds is None:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if base64.b64decode(h[6:]).decode() == self.server.creds:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="fake"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _resolve(self):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).strip("/")
        full = os.path.realpath(os.path.join(self.server.root, rel))
        root = self.server.root
        if full != root and not full.startswith(root + os.sep):
            return rel, None
        return rel, full if os.path.exists(full) else None

    def _send(self, code, body=b"", ctype="text/plain", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def _serve(self):
        self._count()
        if self.path == "/__stats":
            with LOCK:
                return self._send(200, json.dumps(STATS).encode(), "application/json")
        if not self._auth():
            return
        rel, full = self._resolve()
        if full is None or os.path.isdir(full):
            return self._send(404)
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        start, end, code = 0, size, 200
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._send(416, extra=[("Content-Range", f"bytes */{size}")])
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) + 1, size) if m.group(2) else size
            else:
                start = max(size - int(m.group(2)), 0)
            if start >= size or start >= end:
                return self._send(416, extra=[("Content-Range", f"bytes */{size}")])
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", formatdate(os.path.getmtime(full), usegmt=True))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        zr = zero_ranges(rel)
        pos = start
        with open(full, "rb") as fh:
            while pos < end:
                n = min(1 << 20, end - pos)
                fh.seek(pos)
                buf = bytearray(fh.read(n))
                for a, b in zr:
                    lo, hi = max(a, pos), min(b, pos + len(buf))
                    if lo < hi:
                        buf[lo - pos:hi - pos] = bytes(hi - lo)
                        with LOCK:
                            STATS["zeroed_responses"] += 1
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                with LOCK:
                    STATS["bytes_served"] += len(buf)
                pos += len(buf)

    def do_PROPFIND(self):
        self._count()
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        rel, full = self._resolve()
        if full is None:
            return self._send(404)
        depth = self.headers.get("Depth", "1")
        entries = [(rel, full)]
        if os.path.isdir(full) and depth != "0":
            entries += [(f"{rel}/{c}".strip("/"), os.path.join(full, c)) for c in sorted(os.listdir(full))]
        parts = []
        for r, f in entries:
            st = os.stat(f)
            isdir = os.path.isdir(f)
            href = "/" + urllib.parse.quote(r) + ("/" if isdir and r else "")
            parts.append(
                f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
                f"<d:displayname>{escape(os.path.basename(f))}</d:displayname>"
                f"<d:getcontentlength>{0 if isdir else st.st_size}</d:getcontentlength>"
                f"<d:getlastmodified>{formatdate(st.st_mtime, usegmt=True)}</d:getlastmodified>"
                f"<d:resourcetype>{'<d:collection/>' if isdir else ''}</d:resourcetype>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        body = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">'
                + "".join(parts) + "</d:multistatus>").encode()
        self._send(207, body, "application/xml; charset=utf-8")


def make_server(root, port, user=None, password=None, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.root = os.path.realpath(root)
    user = user if user is not None else os.environ.get("FAKE_DAV_USER")
    password = password if password is not None else os.environ.get("FAKE_DAV_PASS", "")
    srv.creds = f"{user}:{password}" if user else None
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user")
    ap.add_argument("--password")
    a = ap.parse_args()
    make_server(a.root, a.port, a.user, a.password, a.host).serve_forever()


if __name__ == "__main__":
    main()
