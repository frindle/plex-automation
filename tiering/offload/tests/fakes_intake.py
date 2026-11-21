"""Fake servers and a fake state module for the intake tests. All local, no network."""
from __future__ import annotations

import email
import email.policy
import hashlib
import json
import shutil
import socketserver
import ssl
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

APIKEY = "k" * 32


def make_nzb(ids, name="x") -> bytes:
    segs = "".join(f'<segment bytes="100" number="{i + 1}">{m}</segment>' for i, m in enumerate(ids))
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file subject="{name}">'
            f"<segments>{segs}</segments></file></nzb>").encode()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass


# ------------------------------------------------------------------ WebDAV

class _DavHandler(_Quiet):
    def _auth(self):
        o = self.server.owner
        if o.auth and self.headers.get("Authorization") != o.auth:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_HEAD(self):
        if not self._auth():
            return
        d = self.server.owner.files.get(urllib.parse.unquote(self.path))
        if d is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()

    def do_GET(self):
        if not self._auth():
            return
        o = self.server.owner
        d = o.files.get(urllib.parse.unquote(self.path))
        if d is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        o.range_requests += 1 if rng else 0
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            a, b = int(a), min(int(b), len(d) - 1)
            if a >= len(d):
                self.send_response(416)
                self.end_headers()
                return
            part = d[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(d)}")
        else:
            part = d
            self.send_response(200)
        self.send_header("Content-Length", str(len(part)))
        self.end_headers()
        self.wfile.write(part)

    def do_PROPFIND(self):
        if not self._auth():
            return
        o = self.server.owner
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        base = urllib.parse.unquote(self.path)
        if not base.endswith("/"):
            base += "/"
        kids = {}
        for p, d in o.files.items():
            if p.startswith(base):
                rest = p[len(base):]
                head = rest.split("/", 1)[0]
                kids[head] = None if "/" in rest else len(d)
        if not kids:
            self.send_response(404)
            self.end_headers()
            return
        out = ['<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">',
               f'<D:response><D:href>{escape(urllib.parse.quote(base))}</D:href><D:propstat><D:prop>'
               "<D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>"]
        for name, size in kids.items():
            href = urllib.parse.quote(base + name + ("/" if size is None else ""))
            if size is None:
                prop = "<D:resourcetype><D:collection/></D:resourcetype>"
            else:
                prop = f"<D:getcontentlength>{size}</D:getcontentlength><D:resourcetype/>"
            out.append(f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{prop}"
                       "</D:prop></D:propstat></D:response>")
        out.append("</D:multistatus>")
        body = "".join(out).encode()
        self.send_response(207)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeDav(_Server):
    def __init__(self, files=None, user="u", password="p"):
        import base64
        self.files = dict(files or {})
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.range_requests = 0
        super().__init__(_DavHandler)


# ------------------------------------------------------------------ AltMount

class _AmHandler(_Quiet):
    def _json(self, obj, status=200):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _history(self, o, q, query):
        # like upstream, nzo_ids is read from the query string only
        if q.get("apikey", [""])[0] != o.key:
            return self._json({"status": False, "error": "Invalid API key"})
        jid = query.get("nzo_ids", [""])[0]
        job = o.jobs.get(jid)
        slots = []
        if job is not None:
            job["polls"] += 1
            if job["polls"] > o.pending_polls:
                if o.fail:
                    slots = [{"nzo_id": jid, "status": "Failed", "storage": ""}]
                else:
                    slots = [{"nzo_id": jid, "status": "Completed", "storage": o.storage}]
        self._json({"history": {"slots": slots, "noofslots": len(slots)}})

    def do_POST(self):
        o = self.server.owner
        o.urls.append(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        ctype = self.headers["Content-Type"]
        if ctype.startswith("application/x-www-form-urlencoded"):
            q = urllib.parse.parse_qs(body.decode())
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if (query.get("mode") or q.get("mode") or [""])[0] == "history":  # upstream qf()
                return self._history(o, q, query)
            return self._json({"status": False, "error": "bad request"})
        msg = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
        form, upload = {}, None
        for part in msg.iter_parts():
            nm = part.get_param("name", header="content-disposition")
            if part.get_filename():
                upload = (part.get_filename(), part.get_payload(decode=True))
            else:
                form[nm] = part.get_content()
        if form.get("apikey") != o.key:
            return self._json({"status": False, "error": "Invalid API key"})
        if form.get("mode") != "addfile" or upload is None:
            return self._json({"status": False, "error": "bad request"})
        o.uploads.append((upload[0], upload[1]))
        o.jobs[form["nzbname"]] = {"polls": 0}
        self._json({"status": True, "nzo_ids": [form["nzbname"]]})

    def do_GET(self):  # the key is never accepted in a URL
        self.server.owner.urls.append(self.path)
        self._json({"status": False, "error": "use POST"}, 405)


class FakeAltMount(_Server):
    def __init__(self, storage, key=APIKEY, pending_polls=1, fail=False):
        self.key, self.storage = key, storage
        self.pending_polls, self.fail = pending_polls, fail
        self.uploads, self.jobs, self.urls = [], {}, []
        super().__init__(_AmHandler)


# ------------------------------------------------------------------ Newznab

class _NzHandler(_Quiet):
    def do_GET(self):
        o = self.server.owner
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if q.get("apikey", [""])[0] != o.key:
            self.send_response(200)
            body = b'<error code="100" description="bad key"/>'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path.startswith("/get/"):
            rel = o.releases.get(u.path[5:])
            if rel is None:
                self.send_response(404)
                self.end_headers()
                return
            o.gets.append(u.path[5:])
            b = rel["nzb"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        o.queries.append({k: v[0] for k, v in q.items() if k != "apikey"})
        if o.fail:
            self.send_response(o.fail_code)
            if o.retry_after is not None:
                self.send_header("Retry-After", str(o.retry_after))
            self.end_headers()
            return
        items = []
        for gid, rel in o.releases.items():
            if rel.get("hidden"):
                continue
            items.append(
                f"<item><title>{escape(rel['title'])}</title><guid>{gid}</guid>"
                f'<enclosure url="{o.url}/get/{gid}?apikey={o.key}" length="{len(rel["nzb"])}"/>'
                f'<newznab:attr name="size" value="{rel.get("size", 1000)}"/></item>')
        body = ('<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
                f"<channel>{o.extra_items}{''.join(items)}</channel></rss>").encode()
        self.send_response(200)
        # truncate: promise more bytes than are sent, so the client sees an IncompleteRead
        self.send_header("Content-Length", str(len(body) + (50 if o.truncate else 0)))
        self.end_headers()
        self.wfile.write(body)


class FakeNewznab(_Server):
    def __init__(self, releases=None, key="nzkey"):
        self.key = key
        self.releases = dict(releases or {})  # guid -> {title, nzb, size?}
        self.queries, self.gets = [], []
        self.fail = False
        self.fail_code = 500
        self.retry_after = None
        self.truncate = False
        self.extra_items = ""  # raw XML put in front of the items
        super().__init__(_NzHandler)


# ------------------------------------------------------------------ NNTP

class _NntpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        o = self.server.owner
        o.connections += 1
        w = self.wfile
        if o.greeting_code != "200":
            w.write(f"{o.greeting_code} no\r\n".encode())
            return
        w.write(b"200 ready\r\n")
        authed = not o.user
        while True:
            line = self.rfile.readline(4096)
            if not line:
                return
            cmd = line.decode().strip()
            if cmd.startswith("AUTHINFO USER"):
                w.write(b"381 pass\r\n")
            elif cmd.startswith("AUTHINFO PASS"):
                if o.refuse_auth or cmd.split(" ", 2)[2] != o.password:
                    w.write(b"481 no\r\n")
                else:
                    authed = True
                    w.write(b"281 ok\r\n")
            elif cmd.startswith("STAT"):
                o.stats += 1
                if not authed:
                    w.write(b"480 auth\r\n")
                elif o.quota:
                    w.write(b"400 over quota\r\n")
                else:
                    mid = cmd[5:].strip().strip("<>")
                    w.write(f"223 0 <{mid}>\r\n".encode() if mid in o.ids else b"430 no\r\n")
            elif cmd == "QUIT":
                w.write(b"205 bye\r\n")
                return
            else:
                w.write(b"500 what\r\n")


class FakeNntp:
    """STAT server. ``cert`` is a (certfile, keyfile) pair to serve TLS."""

    def __init__(self, ids, user="nu", password="np", cert=None):
        self.ids, self.user, self.password = set(ids), user, password
        self.refuse_auth = False
        self.quota = False
        self.greeting_code = "200"
        self.connections = self.stats = 0

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

            def get_request(s):
                sock, addr = s.socket.accept()
                if cert:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                    ctx.load_cert_chain(*cert)
                    sock = ctx.wrap_socket(sock, server_side=True)
                return sock, addr

        self.srv = S(("127.0.0.1", 0), _NntpHandler)
        self.srv.owner = self
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def cfg(self, **kw):
        from tiering.offload.nntp_stat import ProviderCfg
        base = dict(host="127.0.0.1", port=self.port, user=self.user,
                    password=self.password, conns=3, tls=False, batch=7, timeout=5)
        base.update(kw)
        return ProviderCfg(**base)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def self_signed_cert(tmp_path):
    """Make a localhost cert with the openssl CLI. Returns (cert, key) or None."""
    if not shutil.which("openssl"):
        return None
    c, k = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", k, "-out", c,
         "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True)
    return (c, k) if r.returncode == 0 else None


# ------------------------------------------------------------------ state fake

COLUMNS = (
    "id", "lib_path", "stub_path", "remote_path", "size", "mtime_ns", "sha256", "head_len",
    "tail_len", "head_b3", "tail_b3", "nzb_path", "nzb_path2", "title", "year", "imdb_id",
    "tvdb_id", "season", "episode", "release_name", "rating_key", "kind", "is4k", "state",
    "step", "restore_score", "restore_note", "last_check_ts", "last_stat_ok_ts", "last_ok_ts",
    "created_ts", "updated_ts")


class FakeState:
    """The state.py contract (tiering/OFFLOAD_DESIGN.md), in memory. ``db`` is ignored.

    Matches the contract signatures exactly. Unknown columns raise ValueError, as the
    real module does. ``set_state`` is deliberately absent: the intake code must not
    call it.
    """

    def __init__(self, rows=None):
        self.rows = {r["id"]: dict(r) for r in (rows or [])}
        self.events = []
        self.calls = []

    @staticmethod
    def _check(fields):
        bad = [k for k in fields if k not in COLUMNS]
        if bad:
            raise ValueError(f"unknown column(s): {', '.join(sorted(bad))}")

    def list_files(self, db, state=None):
        self.calls.append("list_files")
        return [dict(r) for r in self.rows.values() if state is None or r.get("state") == state]

    def get(self, db, id):
        r = self.rows.get(id)
        return dict(r) if r else None

    def by_path(self, db, lib_path):
        self.calls.append("by_path")
        for r in self.rows.values():
            if r.get("lib_path") == lib_path:
                return dict(r)
        return None

    def upsert(self, db, row):
        self.calls.append("upsert")
        if not row.get("id"):
            raise ValueError("row needs an id")
        self._check(row)
        cur = self.rows.setdefault(row["id"], {"state": "whole", "is4k": 0, "step": 0})
        cur.update({k: v for k, v in row.items() if k not in ("state", "is4k", "step") or v is not None})

    def update_if(self, db, id, expect_state, **fields):
        self.calls.append("update_if")
        self._check(fields)
        r = self.rows.get(id)
        if r is None or r.get("state") != expect_state:
            return False
        r.update(fields)
        r["updated_ts"] = 1
        return True

    def log_event(self, db, id, kind, detail):
        self.calls.append("log_event")
        self.events.append((id, kind, detail))


# ------------------------------------------------------------------ Radarr / Sonarr

class _ArrHandler(_Quiet):
    def do_GET(self):
        o = self.server.owner
        u = urllib.parse.urlsplit(self.path)
        o.requests.append((u.path, u.query, self.headers.get("X-Api-Key")))
        if self.headers.get("X-Api-Key") != o.key or "apikey" in u.query.lower():
            self.send_response(401)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(u.query)
        if o.fail:
            self.send_response(o.fail_code)
            if o.retry_after is not None:
                self.send_header("Retry-After", str(o.retry_after))
            self.end_headers()
            return
        if u.path == "/api/v3/movie":
            data = o.movies
        elif u.path == "/api/v3/series":
            data = o.series
        elif u.path == "/api/v3/episode":
            data = o.episodes.get(int(q["seriesId"][0]), [])
        else:
            self.send_response(404)
            self.end_headers()
            return
        b = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class FakeArr(_Server):
    """A Radarr (``movies``) or Sonarr (``series`` and ``episodes``) API on a real socket."""

    def __init__(self, key="arrkey", movies=None, series=None, episodes=None):
        self.key = key
        self.movies, self.series = movies or [], series or []
        self.episodes = episodes or {}  # series id -> list of episode dicts
        self.requests = []
        self.fail = False
        super().__init__(_ArrHandler)
