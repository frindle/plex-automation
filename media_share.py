"""
Friend-facing media portal + admin panel.

Friends authenticate via Cloudflare Access (Cf-Access-Authenticated-User-Email).
Admin manages friends at /share/admin; friends update their own upload destination
at /share/settings. Config stored in SQLite — no container restart needed.

Security: relies on Cloudflare Access being the only ingress. Never expose the
container port directly to the internet.
"""
import ftplib
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import secrets
import tempfile
import zipfile
import paramiko
import requests as _requests
import ssl
from flask import Blueprint, after_this_request, redirect, render_template_string, request, send_file

log = logging.getLogger(__name__)

share_bp = Blueprint('share', __name__, url_prefix='/share')


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Implicit FTPS: wraps the socket with TLS immediately on connect (port 990)."""
    def connect(self, host='', port=990, timeout=-999, source_address=None):
        self.host = host
        self.port = port
        self.timeout = self.timeout if timeout == -999 else timeout
        self.source_address = source_address
        sock = socket.create_connection((host, port), self.timeout, source_address)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(sock, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

LIBRARIES = {'movies': '/media/movies', 'tv': '/media/tv', 'music': '/media/music'}
ALL_LIBRARIES = list(LIBRARIES.keys())

DB_PATH = os.environ.get('SHARE_DB_PATH', '/data/share_uploads.db')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')
RATE_LIMIT_BYTES_PER_SEC = float(os.environ.get('UPLOAD_RATE_LIMIT_MBIT', '5')) * 1_000_000 / 8

RADARR_URL = os.environ.get('RADARR_URL', 'http://10.0.0.7:7878')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY', '')
SONARR_URL = os.environ.get('SONARR_URL', 'http://10.0.0.8:8989')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY', '')

USAGE_WINDOWS = {
    '7 days': 7, '30 days': 30, '60 days': 60,
    '90 days': 90, '6 months': 182, '1 year': 365,
}

# ── Global rate limiter ───────────────────────────────────────────────────────

_rate_bytes_sent = 0
_rate_start = time.monotonic()
_rate_lock = threading.Lock()

# ── Zip jobs (in-memory, keyed by share token) ────────────────────────────────

_zip_jobs: dict = {}
_zip_jobs_lock = threading.Lock()


def _zip_sweep():
    """Periodically clean up zip jobs whose tokens have expired or been revoked."""
    while True:
        time.sleep(900)  # run every 15 minutes
        with _zip_jobs_lock:
            stale = [t for t in list(_zip_jobs) if _token_expired(t)]
            for t in stale:
                _expire_zip_job(t)
                log.debug('zip sweep: cleaned up stale job for token %s…', t[:8])


threading.Thread(target=_zip_sweep, daemon=True).start()


def _zip_worker(token, full_path, label):
    tmp_path = None
    try:
        all_files = []
        bytes_total = 0
        for dirpath, _, filenames in os.walk(full_path):
            for fn in sorted(filenames):
                abs_path = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    continue
                arc_name = os.path.relpath(abs_path, os.path.dirname(full_path))
                all_files.append((abs_path, arc_name, size))
                bytes_total += size

        with _zip_jobs_lock:
            if token not in _zip_jobs:
                return
            _zip_jobs[token]['bytes_total'] = bytes_total

        tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
        tmp_path = tmp.name
        tmp.close()

        bytes_done = 0
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc_name, size in all_files:
                zf.write(abs_path, arc_name)
                bytes_done += size
                with _zip_jobs_lock:
                    if token not in _zip_jobs:
                        return
                    _zip_jobs[token]['bytes_done'] = bytes_done

        with _zip_jobs_lock:
            if token not in _zip_jobs:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return
            _zip_jobs[token].update({'status': 'ready', 'tmp_path': tmp_path, 'bytes_done': bytes_total})

    except Exception as e:
        log.error('zip worker error for token %s: %s', token, e)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        with _zip_jobs_lock:
            if token in _zip_jobs:
                _zip_jobs[token].update({'status': 'error', 'error': str(e)})


def _global_throttle(delta_bytes):
    global _rate_bytes_sent, _rate_start
    if RATE_LIMIT_BYTES_PER_SEC <= 0:
        return
    with _rate_lock:
        _rate_bytes_sent += delta_bytes
        elapsed = time.monotonic() - _rate_start
        sleep_for = (_rate_bytes_sent / RATE_LIMIT_BYTES_PER_SEC) - elapsed
    if sleep_for > 0:
        time.sleep(sleep_for)


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'sftp',
            sftp_host TEXT NOT NULL DEFAULT '',
            sftp_port INTEGER NOT NULL DEFAULT 22,
            sftp_user TEXT NOT NULL DEFAULT '',
            sftp_password TEXT NOT NULL DEFAULT '',
            sftp_remote_dir TEXT NOT NULL DEFAULT '/',
            libraries TEXT NOT NULL DEFAULT '["movies","tv","music"]',
            allowed_titles TEXT,
            rate_limit_mbit REAL NOT NULL DEFAULT 5.0,
            created_at TEXT NOT NULL
        )
    ''')
    for col, defn in [
        ('protocol', "TEXT NOT NULL DEFAULT 'sftp'"),
        ('allowed_titles', 'TEXT'),
    ]:
        try:
            db.execute(f'ALTER TABLE friends ADD COLUMN {col} {defn}')
        except sqlite3.OperationalError:
            pass
    db.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            friend_email TEXT NOT NULL,
            library TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            status TEXT NOT NULL,
            bytes_total INTEGER NOT NULL DEFAULT 0,
            bytes_done INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS share_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            library TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            label TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_downloads INTEGER NOT NULL DEFAULT 0,
            download_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    db.commit()
    db.close()


def _get_friend(email):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM friends WHERE email=?', (email,)).fetchone()
    db.close()
    return row


def get_friend_config(email):
    row = _get_friend(email)
    if not row:
        return None
    keys = row.keys()
    return {
        'protocol': row['protocol'] if 'protocol' in keys else 'sftp',
        'host': row['sftp_host'],
        'port': int(row['sftp_port']),
        'user': row['sftp_user'],
        'password': row['sftp_password'],
        'remote_dir': row['sftp_remote_dir'],
    }


def get_friend_libraries(email):
    row = _get_friend(email)
    if not row:
        return []
    return [k for k in json.loads(row['libraries']) if k in LIBRARIES]


def _get_allowed_titles(email):
    """Returns dict {library: [folder_names]} or {} meaning no restriction."""
    row = _get_friend(email)
    if not row:
        return {}
    raw = row['allowed_titles'] if 'allowed_titles' in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_email():
    return request.headers.get('Cf-Access-Authenticated-User-Email', '')


def _is_admin(email):
    return bool(ADMIN_EMAIL) and email == ADMIN_EMAIL


def safe_join(root, rel_path):
    rel_path = rel_path or ''
    full = os.path.normpath(os.path.join(root, rel_path))
    root_n = os.path.normpath(root)
    if full != root_n and not full.startswith(root_n + os.sep):
        raise ValueError('Path traversal attempt')
    return full


def _allowed_libraries(email):
    if _is_admin(email):
        return ALL_LIBRARIES
    return get_friend_libraries(email)


def _fmt_bytes(n):
    n = int(n or 0)
    if n >= 1 << 30:
        return f'{n / (1 << 30):.2f} GB'
    if n >= 1 << 20:
        return f'{n / (1 << 20):.1f} MB'
    if n >= 1 << 10:
        return f'{n / (1 << 10):.0f} KB'
    return f'{n} B'


# ── Arr API helpers ───────────────────────────────────────────────────────────

def _arr_get(base_url, api_key, endpoint):
    try:
        r = _requests.get(
            f'{base_url.rstrip("/")}/api/v3/{endpoint}',
            headers={'X-Api-Key': api_key},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning('arr API %s: %s', endpoint, e)
        return []


def _poster_url(images):
    for img in (images or []):
        if img.get('coverType') == 'poster':
            return img.get('remoteUrl', '')
    return ''


def _check_title_access(email, library, rel_path):
    """Return True if the user may access rel_path inside library.

    allowed_titles formats:
      movies: {"movies": ["folder1", "folder2"]}
      tv:     {"tv": {"show_folder": null (all seasons) | ["Season 01", ...]}}
      legacy tv (pre-season support): {"tv": ["show_folder1", ...]}
    """
    if _is_admin(email):
        return True
    titles = _get_allowed_titles(email)
    if library not in titles:
        return True  # no restriction for this library

    parts = [p for p in rel_path.replace('\\', '/').split('/') if p]
    if not parts:
        return True  # library root — poster grid handles filtering

    top = parts[0]

    if library == 'movies':
        allowed_list = titles['movies']
        if not isinstance(allowed_list, list):
            return True
        return top.lower() in {f.lower() for f in allowed_list}

    if library == 'tv':
        tv_map = titles['tv']
        # Legacy: plain list means whole-show access, all seasons allowed
        if isinstance(tv_map, list):
            return top.lower() in {f.lower() for f in tv_map}
        if not isinstance(tv_map, dict):
            return True
        # Find this show in the dict (case-insensitive)
        show_seasons = _MISSING = object()
        for k, v in tv_map.items():
            if k.lower() == top.lower():
                show_seasons = v
                break
        if show_seasons is _MISSING:
            return False  # show not in allowed dict
        if show_seasons is None:
            return True  # all seasons allowed for this show
        if len(parts) < 2:
            return True  # browsing the show root — show itself is accessible
        season = parts[1]
        return season.lower() in {s.lower() for s in show_seasons}

    return True


def _movies_for_user(email):
    movies = [m for m in _arr_get(RADARR_URL, RADARR_API_KEY, 'movie') if m.get('hasFile')]
    movies.sort(key=lambda m: m.get('sortTitle', m.get('title', '')).lower())
    if _is_admin(email):
        return movies
    titles = _get_allowed_titles(email)
    if 'movies' not in titles:
        return movies
    allowed_list = titles['movies']
    if not isinstance(allowed_list, list):
        return movies
    allowed_set = {f.lower() for f in allowed_list}
    return [m for m in movies if os.path.basename(m.get('path', '')).lower() in allowed_set]


def _series_for_user(email):
    series = [s for s in _arr_get(SONARR_URL, SONARR_API_KEY, 'series')
              if s.get('statistics', {}).get('episodeFileCount', 0) > 0]
    series.sort(key=lambda s: s.get('sortTitle', s.get('title', '')).lower())
    if _is_admin(email):
        return series
    titles = _get_allowed_titles(email)
    if 'tv' not in titles:
        return series
    tv_map = titles['tv']
    if isinstance(tv_map, list):
        allowed_set = {f.lower() for f in tv_map}
    elif isinstance(tv_map, dict):
        allowed_set = {k.lower() for k in tv_map}
    else:
        return series
    return [s for s in series if os.path.basename(s.get('path', '')).lower() in allowed_set]


# ── Upload ────────────────────────────────────────────────────────────────────

def _sftp_mkdirs(sftp, remote_dir):
    path = ''
    for part in remote_dir.strip('/').split('/'):
        if not part:
            continue
        path += '/' + part
        try:
            sftp.mkdir(path)
        except IOError:
            pass


def _ftp_mkdirs(ftp, path):
    parts = [p for p in path.split('/') if p]
    current = ''
    for part in parts:
        current += '/' + part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass


def _upload_sftp(friend_cfg, files, top_name, upload_id, db):
    transport = paramiko.Transport((friend_cfg['host'], friend_cfg['port'] or 22))
    transport.connect(username=friend_cfg['user'], password=friend_cfg['password'])
    sftp = paramiko.SFTPClient.from_transport(transport)
    base_remote = friend_cfg.get('remote_dir', '/').rstrip('/')
    bytes_done_total = 0

    try:
        for local_file, rel in files:
            remote_path = f'{base_remote}/{top_name}/{rel}'.replace('\\', '/')
            _sftp_mkdirs(sftp, os.path.dirname(remote_path))
            last_sent = {'val': 0}
            base = bytes_done_total

            def progress(sent, _total, _last=last_sent, _base=base):
                delta = sent - _last['val']
                _last['val'] = sent
                _global_throttle(delta)
                db.execute('UPDATE uploads SET bytes_done=? WHERE id=?', (_base + sent, upload_id))
                db.commit()

            sftp.put(local_file, remote_path, callback=progress)
            bytes_done_total += os.path.getsize(local_file)
    finally:
        sftp.close()
        transport.close()

    return bytes_done_total


def _ftp_connect(host, port, user, password, timeout=30):
    """Try implicit FTPS → explicit FTPS → plain FTP. Returns (ftp, mode_label)."""
    last_exc = None
    # 1. Implicit FTPS (port 990 style — TLS from connection start)
    try:
        ftp = _ImplicitFTP_TLS()
        ftp.connect(host, port, timeout=timeout)
        ftp.login(user, password)
        ftp.prot_p()
        return ftp, 'implicit-ftps'
    except Exception as e:
        last_exc = e
        try:
            ftp.close()
        except Exception:
            pass
    # 2. Explicit FTPS (AUTH TLS)
    try:
        ftp = ftplib.FTP_TLS()
        ftp.connect(host, port, timeout=timeout)
        ftp.auth()
        ftp.login(user, password)
        ftp.prot_p()
        return ftp, 'explicit-ftps'
    except Exception as e:
        last_exc = e
        try:
            ftp.close()
        except Exception:
            pass
    # 3. Plain FTP
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=timeout)
        ftp.login(user, password)
        return ftp, 'plain-ftp'
    except Exception as e:
        last_exc = e
    raise last_exc


def _upload_ftps(friend_cfg, files, top_name, upload_id, db):
    host = friend_cfg['host']
    port = friend_cfg['port'] or 21
    ftp, _ = _ftp_connect(host, port, friend_cfg['user'], friend_cfg['password'])
    ftp.set_pasv(True)
    base_remote = friend_cfg.get('remote_dir', '/').rstrip('/')
    bytes_done_total = 0

    for local_file, rel in files:
        remote_path = f'{base_remote}/{top_name}/{rel}'.replace('\\', '/')
        remote_dir = '/'.join(remote_path.split('/')[:-1])
        filename = remote_path.split('/')[-1]
        _ftp_mkdirs(ftp, remote_dir)
        ftp.cwd(remote_dir)
        chunk_ref = {'val': 0}
        base = bytes_done_total

        def cb(data, _ref=chunk_ref, _base=base):
            _ref['val'] += len(data)
            _global_throttle(len(data))
            db.execute('UPDATE uploads SET bytes_done=? WHERE id=?', (_base + _ref['val'], upload_id))
            db.commit()

        with open(local_file, 'rb') as f:
            ftp.storbinary(f'STOR {filename}', f, blocksize=8192, callback=cb)
        bytes_done_total += os.path.getsize(local_file)

    try:
        ftp.quit()
    except Exception:
        pass
    return bytes_done_total


def perform_upload(upload_id, friend_cfg, local_root, rel_path):
    db = sqlite3.connect(DB_PATH)
    try:
        full_path = safe_join(local_root, rel_path)
        if os.path.isdir(full_path):
            files = [
                (os.path.join(dp, fn), os.path.relpath(os.path.join(dp, fn), full_path))
                for dp, _, fns in os.walk(full_path) for fn in fns
            ]
        else:
            files = [(full_path, os.path.basename(full_path))]

        db.execute('UPDATE uploads SET status=? WHERE id=?', ('running', upload_id))
        db.commit()

        top_name = os.path.basename(full_path.rstrip('/'))
        if friend_cfg.get('protocol', 'sftp') == 'ftps':
            bytes_done = _upload_ftps(friend_cfg, files, top_name, upload_id, db)
        else:
            bytes_done = _upload_sftp(friend_cfg, files, top_name, upload_id, db)

        db.execute(
            'UPDATE uploads SET status=?, bytes_done=?, finished_at=? WHERE id=?',
            ('done', bytes_done, datetime.utcnow().isoformat(), upload_id),
        )
        db.commit()
        log.info('Upload %d complete: %.1fMB to %s', upload_id, bytes_done / 1024 / 1024, friend_cfg['host'])
    except Exception as e:
        db.execute(
            'UPDATE uploads SET status=?, error=?, finished_at=? WHERE id=?',
            ('failed', str(e), datetime.utcnow().isoformat(), upload_id),
        )
        db.commit()
        log.error('Upload %d failed: %s', upload_id, e)
    finally:
        db.close()


# ── Connection test helper ────────────────────────────────────────────────────

def _test_connection(protocol, host, port, user, password):
    if not host or not user or not password:
        return False, 'Host, username, and password are required'
    if protocol == 'ftps':
        default_port = port or 21
        try:
            ftp, mode = _ftp_connect(host, default_port, user, password, timeout=10)
            ftp.quit()
            label = {'implicit-ftps': '', 'explicit-ftps': '(explicit FTPS)', 'plain-ftp': '(plain FTP — no TLS)'}
            return True, label.get(mode, '')
        except socket.timeout:
            return False, f'Connection timed out after 10s (could not reach {host}:{default_port})'
        except ftplib.error_perm as e:
            return False, f'Authentication failed: {e}'
        except Exception as e:
            return False, str(e)
    else:
        default_port = port or 22
        try:
            sock = socket.create_connection((host, default_port), timeout=10)
            transport = paramiko.Transport(sock)
            transport.connect(username=user, password=password)
            transport.close()
            return True, ''
        except socket.timeout:
            return False, f'Connection timed out after 10s (could not reach {host}:{default_port})'
        except Exception as e:
            return False, str(e)


# ── CSS / shared snippets ─────────────────────────────────────────────────────

_BASE_CSS = '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0f0f0f; color: #e8e8e8; min-height: 100vh; }
header { background: #1a1a1a; border-bottom: 1px solid #2a2a2a;
         padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
header h1 a { font-size: 1.1rem; font-weight: 600; letter-spacing: 0.02em; color: #e2a826;
              text-decoration: none; }
header h1 a:hover { color: #f0c040; }
header .meta { font-size: 0.8rem; color: #666; }
header nav a { color: #888; text-decoration: none; font-size: 0.85rem; margin-left: 16px; }
header nav a:hover { color: #fff; }
main { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }
h2 { font-size: 1.3rem; font-weight: 600; margin-bottom: 24px; color: #fff; }
h3 { font-size: 1rem; font-weight: 600; margin-bottom: 16px; color: #ccc; }
a { color: #6ea8fe; text-decoration: none; }
a:hover { text-decoration: underline; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
        padding: 24px 20px; text-align: center; cursor: pointer;
        transition: border-color 0.15s, background 0.15s; text-decoration: none; color: inherit; }
.card:hover { border-color: #555; background: #222; text-decoration: none; }
.card .icon { font-size: 2.4rem; margin-bottom: 12px; }
.card .label { font-size: 0.95rem; font-weight: 500; color: #e8e8e8; text-transform: capitalize; }
.poster-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px; }
.poster-card { text-decoration: none; color: inherit; display: block; }
.poster-card:hover .poster-img { opacity: 0.7; transform: scale(1.03); }
.poster-card:hover .poster-ph { opacity: 0.7; }
.poster-img { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: 6px;
              transition: opacity 0.15s, transform 0.15s; display: block; }
.poster-ph { width: 100%; aspect-ratio: 2/3; background: #2a2a2a; border-radius: 6px;
             display: flex; align-items: center; justify-content: center; font-size: 2.5rem;
             transition: opacity 0.15s; }
.poster-title { font-size: 0.8rem; font-weight: 500; color: #e8e8e8; margin-top: 7px;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.poster-year { font-size: 0.72rem; color: #666; }
.breadcrumb { font-size: 0.85rem; color: #666; margin-bottom: 20px; }
.breadcrumb a { color: #6ea8fe; }
.file-list { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; overflow: hidden; }
.file-row { display: flex; align-items: center; padding: 11px 16px;
            border-bottom: 1px solid #222; gap: 10px; }
.file-row:last-child { border-bottom: none; }
.file-row:hover { background: #222; }
.file-icon { font-size: 1.1rem; width: 24px; text-align: center; flex-shrink: 0; }
.file-name { flex: 1; font-size: 0.9rem; color: #e8e8e8; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }
.file-name a { color: #e8e8e8; }
.file-name a:hover { color: #6ea8fe; text-decoration: none; }
.actions { display: flex; gap: 8px; flex-shrink: 0; }
.btn { display: inline-block; padding: 5px 12px; border-radius: 6px; font-size: 0.78rem;
       font-weight: 500; cursor: pointer; border: none; text-decoration: none; white-space: nowrap; }
.btn-primary { background: #2563eb; color: #fff; }
.btn-primary:hover { background: #1d4ed8; text-decoration: none; color: #fff; }
.btn-ghost { background: #2a2a2a; color: #bbb; border: 1px solid #333; }
.btn-ghost:hover { background: #333; color: #fff; text-decoration: none; }
.btn-danger { background: #7f1d1d; color: #fca5a5; border: 1px solid #991b1b; }
.btn-danger:hover { background: #991b1b; text-decoration: none; }
.btn-sm { padding: 3px 9px; font-size: 0.74rem; }
.stat-table { width: 100%; border-collapse: collapse; }
.stat-table th { text-align: left; font-size: 0.8rem; font-weight: 500; color: #666;
                 text-transform: uppercase; letter-spacing: 0.05em; padding: 0 0 10px; }
.stat-table td { padding: 10px 0; border-top: 1px solid #222; font-size: 0.9rem; }
.stat-table .size { font-variant-numeric: tabular-nums; color: #aaa; }
.status-card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
               padding: 28px; max-width: 480px; }
.status-label { font-size: 0.8rem; color: #666; text-transform: uppercase;
                letter-spacing: 0.05em; margin-bottom: 4px; }
.status-value { font-size: 1rem; font-weight: 500; margin-bottom: 18px; }
.status-done { color: #4ade80; }
.status-running { color: #facc15; }
.status-failed { color: #f87171; }
.progress-bar { height: 6px; background: #2a2a2a; border-radius: 3px; overflow: hidden; margin-bottom: 18px; }
.progress-fill { height: 100%; background: #2563eb; border-radius: 3px; transition: width 0.5s; }
.error-box { background: #2a1a1a; border: 1px solid #5a2a2a; border-radius: 6px;
             padding: 12px 14px; font-size: 0.85rem; color: #f87171; word-break: break-all; }
.alert-ok { background: #052e16; border: 1px solid #166534; border-radius: 6px;
            padding: 10px 14px; font-size: 0.88rem; color: #4ade80; margin-bottom: 20px; }
.test-result { display:none; margin-top:10px; padding: 8px 12px; border-radius: 6px; font-size: 0.85rem; }
.test-ok  { background:#052e16; border:1px solid #166534; color:#4ade80; }
.test-err { background:#2a1a1a; border:1px solid #5a2a2a; color:#f87171; }
.form-card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
             padding: 28px; max-width: 560px; }
.field { margin-bottom: 18px; }
.field label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 6px;
               text-transform: uppercase; letter-spacing: 0.04em; }
.field input[type=text], .field input[type=password], .field input[type=email],
.field input[type=number], .field select {
  width: 100%; background: #111; border: 1px solid #333; border-radius: 6px;
  padding: 8px 12px; color: #e8e8e8; font-size: 0.9rem; outline: none;
  appearance: none; -webkit-appearance: none; }
.field input:focus, .field select:focus { border-color: #2563eb; }
.field .hint { font-size: 0.75rem; color: #555; margin-top: 4px; }
.checkbox-group { display: flex; gap: 16px; flex-wrap: wrap; }
.checkbox-group label { display: flex; align-items: center; gap: 6px; font-size: 0.9rem;
                        color: #e8e8e8; text-transform: none; letter-spacing: 0; cursor: pointer; }
.admin-table { width: 100%; border-collapse: collapse; }
.admin-table th { text-align: left; font-size: 0.78rem; color: #666; text-transform: uppercase;
                  letter-spacing: 0.05em; padding: 0 12px 10px 0; }
.admin-table td { padding: 12px 12px 12px 0; border-top: 1px solid #222; font-size: 0.88rem;
                  vertical-align: middle; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
         background: #1e3a5f; color: #93c5fd; margin: 2px 2px 2px 0; }
.section-gap { margin-top: 40px; }
.search-input { width: 100%; background: #111; border: 1px solid #333; border-radius: 6px;
                padding: 8px 12px; color: #e8e8e8; font-size: 0.9rem; outline: none;
                margin-bottom: 12px; }
.search-input:focus { border-color: #2563eb; }
.titles-list { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px;
               max-height: 360px; overflow-y: auto; padding: 8px 0; }
.title-item { display: flex; align-items: center; gap: 10px; padding: 8px 16px; }
.title-item:hover { background: #222; }
.title-item label { font-size: 0.88rem; color: #e8e8e8; cursor: pointer; }
.usage-section { margin-bottom: 40px; }
.usage-name { font-size: 0.82rem; color: #888; font-weight: 600; text-transform: uppercase;
              letter-spacing: 0.05em; margin-bottom: 10px; }
'''

_PROTO_JS = '''
<script>
function onProtoChange(sel) {
  var p = document.querySelector('[name=sftp_port]');
  if (sel.value === 'ftps' && (p.value == '22' || p.value === '')) p.value = '21';
  if (sel.value === 'sftp' && (p.value == '21' || p.value === '')) p.value = '22';
}
function testConn(friendId) {
  var res = document.getElementById('test-result');
  res.className = 'test-result'; res.style.display = 'none';
  var data = new FormData();
  var proto = document.querySelector('[name=protocol]');
  data.append('protocol',      proto ? proto.value : 'sftp');
  data.append('sftp_host',     document.querySelector('[name=sftp_host]').value);
  data.append('sftp_port',     document.querySelector('[name=sftp_port]').value);
  data.append('sftp_user',     document.querySelector('[name=sftp_user]').value);
  data.append('sftp_password', document.querySelector('[name=sftp_password]').value);
  if (friendId) data.append('friend_id', friendId);
  var btn = document.getElementById('test-btn');
  btn.disabled = true; btn.textContent = 'Testing…';
  fetch('/share/admin/test-connection', {method:'POST', body:data})
    .then(function(r){ return r.json(); })
    .then(function(j){
      res.textContent = j.ok ? '✓ Connected successfully' : '✗ ' + j.error;
      res.className = 'test-result ' + (j.ok ? 'test-ok' : 'test-err');
      res.style.display = 'block';
    })
    .catch(function(){ res.textContent='✗ Request failed'; res.className='test-result test-err'; res.style.display='block'; })
    .finally(function(){ btn.disabled=false; btn.textContent='Test connection'; });
}
function testSelf() {
  var res = document.getElementById('test-result');
  res.className = 'test-result'; res.style.display = 'none';
  var data = new FormData();
  var proto = document.querySelector('[name=protocol]');
  data.append('protocol',      proto ? proto.value : 'sftp');
  data.append('sftp_host',     document.querySelector('[name=sftp_host]').value);
  data.append('sftp_port',     document.querySelector('[name=sftp_port]').value);
  data.append('sftp_user',     document.querySelector('[name=sftp_user]').value);
  data.append('sftp_password', document.querySelector('[name=sftp_password]').value);
  var btn = document.getElementById('test-btn');
  btn.disabled = true; btn.textContent = 'Testing…';
  fetch('/share/test-connection', {method:'POST', body:data})
    .then(function(r){ return r.json(); })
    .then(function(j){
      res.textContent = j.ok ? '✓ Connected successfully' : '✗ ' + j.error;
      res.className = 'test-result ' + (j.ok ? 'test-ok' : 'test-err');
      res.style.display = 'block';
    })
    .catch(function(){ res.textContent='✗ Request failed'; res.className='test-result test-err'; res.style.display='block'; })
    .finally(function(){ btn.disabled=false; btn.textContent='Test connection'; });
}
</script>
'''

_LIB_ICONS = {'movies': '🎬', 'tv': '📺', 'music': '🎵'}

_NAV = '''
<header>
  <h1><a href="/share">Media Share</a></h1>
  <div style="display:flex;align-items:center;gap:16px">
    <nav>
      <a href="/share">Libraries</a>
      <a href="/share/usage">Usage</a>
      {% if not is_admin %}<a href="/share/settings">Settings</a>{% endif %}
      {% if is_admin %}<a href="/share/admin">Admin</a>{% endif %}
      {% if is_admin %}<a href="/share/admin/links">Links</a>{% endif %}
    </nav>
    <span class="meta">{{ email }}</span>
  </div>
</header>
'''

# ── Templates ─────────────────────────────────────────────────────────────────

_HEAD_A = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>')
_HEAD_B = '</title><style>' + _BASE_CSS + '</style></head><body>'


def _head(title):
    return _HEAD_A + title + _HEAD_B


INDEX_HTML = _head('Media Share') + _NAV + '''
<main>
  <h2>Libraries</h2>
  <div class="card-grid">
  {% for lib in libraries %}
    <a class="card" href="/share/browse/{{ lib }}">
      <div class="icon">{{ icons.get(lib, "📁") }}</div>
      <div class="label">{{ lib }}</div>
    </a>
  {% endfor %}
  </div>
</main></body></html>'''

POSTER_GRID_HTML = _head('{{ library|capitalize }} — Media Share') + _NAV + '''
<main>
  <div class="breadcrumb"><a href="/share">home</a> / {{ library }}</div>
  {% if not items %}
  <p style="color:#666">No titles available (library may be empty or API unreachable).</p>
  {% else %}
  <div class="poster-grid">
  {% for item in items %}
    <a class="poster-card" href="/share/browse/{{ library }}?path={{ item.folder | urlencode }}">
      {% if item.poster %}
        <img class="poster-img" src="{{ item.poster }}" loading="lazy" alt="{{ item.title }}">
      {% else %}
        <div class="poster-ph">{{ icons.get(library, "📁") }}</div>
      {% endif %}
      <div class="poster-title" title="{{ item.title }}">{{ item.title }}</div>
      <div class="poster-year">{{ item.year }}</div>
    </a>
  {% endfor %}
  </div>
  {% endif %}
</main></body></html>'''

BROWSE_HTML = _head('{{ library }} — Media Share') + _NAV + '''
<main>
  <div class="breadcrumb">
    <a href="/share">home</a> / <a href="/share/browse/{{ library }}">{{ library }}</a>
    {% if rel_path %}{% for part in rel_path.split("/") if part %} / {{ part }}{% endfor %}{% endif %}
  </div>
  <div class="file-list">
    {% if parent is not none %}
    <div class="file-row">
      <span class="file-icon">⬆</span>
      <span class="file-name"><a href="/share/browse/{{ library }}?path={{ parent | urlencode }}">.. up</a></span>
    </div>
    {% endif %}
    {% for e in entries %}
    <div class="file-row">
      <span class="file-icon">{% if e.is_dir %}📁{% else %}🎬{% endif %}</span>
      <span class="file-name">
        {% if e.is_dir %}
          <a href="/share/browse/{{ library }}?path={{ e.rel | urlencode }}">{{ e.name }}</a>
        {% else %}{{ e.name }}{% endif %}
      </span>
      <div class="actions">
        {% if not e.is_dir %}
        <a class="btn btn-ghost btn-sm" href="/share/download/{{ library }}?path={{ e.rel | urlencode }}">Download</a>
        {% endif %}
        {% if can_upload %}
        <form method="post" action="/share/upload" style="display:inline">
          <input type="hidden" name="library" value="{{ library }}">
          <input type="hidden" name="rel_path" value="{{ e.rel }}">
          <button class="btn btn-primary btn-sm" type="submit">Upload to me</button>
        </form>
        {% endif %}
        {% if is_admin %}
        <a class="btn btn-ghost btn-sm" href="/share/admin/create-link?library={{ library }}&path={{ e.rel | urlencode }}">Create Link</a>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</main></body></html>'''

STATUS_HTML = (_head('Upload Status — Media Share')
               + '{% if row.status in ("pending", "running") %}<meta http-equiv="refresh" content="3">{% endif %}'
               + _NAV + '''
<main>
  <h2>Upload #{{ row.id }}</h2>
  <div class="status-card">
    <div class="status-label">File</div>
    <div class="status-value">{{ row.rel_path.split("/")[-1] }}</div>
    <div class="status-label">Status</div>
    <div class="status-value status-{{ row.status }}">{{ row.status }}</div>
    {% if row.bytes_total and row.bytes_total > 0 %}
    <div class="progress-bar">
      <div class="progress-fill" style="width:{{ [(row.bytes_done / row.bytes_total * 100), 100]|min|int }}%"></div>
    </div>
    {% endif %}
    <div class="status-label">Sent</div>
    <div class="status-value">{{ "%.1f"|format(row.bytes_done / 1024 / 1024) }} MB</div>
    {% if row.error %}<div class="error-box">{{ row.error }}</div>{% endif %}
  </div>
</main></body></html>''')

USAGE_HTML = _head('Usage — Media Share') + _NAV + '''
<main>
  <h2>{% if is_admin %}Usage by Friend{% else %}Your Usage{% endif %}</h2>
  {% if is_admin %}
  <table class="stat-table">
    <tr>
      <th>Friend</th>
      {% for label in windows %}<th>{{ label }}</th>{% endfor %}
    </tr>
    {% for row in admin_rows %}
    <tr>
      <td>{{ row.email }}</td>
      {% for v in row.cols %}<td class="size">{{ v }}</td>{% endfor %}
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <table class="stat-table">
    <tr><th>Period</th><th>Data Sent</th></tr>
    {% for label, total in usage.items() %}
    <tr><td>{{ label }}</td><td class="size">{{ total }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}
</main></body></html>'''

SETTINGS_HTML = _head('Settings — Media Share') + _NAV + '''
<main>
  <h2>Your Upload Settings</h2>
  <p style="color:#888;margin-bottom:24px;font-size:0.9rem">
    Set your FTPS or SFTP destination. The "Upload to me" button on any file will push to this server.
  </p>
  {% if saved %}<div class="alert-ok">Settings saved.</div>{% endif %}
  {% if not f %}
  <p style="color:#f87171;margin-bottom:16px">Your account has not been set up by the admin yet.</p>
  {% else %}
  <div class="form-card">
    <form method="post">
      <div class="field">
        <label>Protocol</label>
        <select name="protocol" onchange="onProtoChange(this)">
          <option value="ftps" {% if f.protocol == "ftps" %}selected{% endif %}>FTPS (FTP over TLS)</option>
          <option value="sftp" {% if f.protocol == "sftp" %}selected{% endif %}>SFTP (SSH)</option>
        </select>
      </div>
      <div class="field"><label>Host</label>
        <input type="text" name="sftp_host" value="{{ f.sftp_host }}"></div>
      <div class="field"><label>Port</label>
        <input type="number" name="sftp_port" value="{{ f.sftp_port }}"></div>
      <div class="field"><label>Username</label>
        <input type="text" name="sftp_user" value="{{ f.sftp_user }}"></div>
      <div class="field"><label>Password</label>
        <input type="password" name="sftp_password" placeholder="Leave blank to keep current"></div>
      <div class="field"><label>Remote directory</label>
        <input type="text" name="sftp_remote_dir" value="{{ f.sftp_remote_dir }}"></div>
      <div class="field">
        <button class="btn btn-ghost" type="button" id="test-btn" onclick="testSelf()">Test connection</button>
        <div class="test-result" id="test-result"></div>
      </div>
      <button class="btn btn-primary" type="submit">Save settings</button>
    </form>
  </div>
  {% endif %}
</main>''' + _PROTO_JS + '''</body></html>'''

ADMIN_HTML = _head('Admin — Media Share') + _NAV + '''
<main>
  <h2>Friends</h2>
  {% if friends %}
  <table class="admin-table">
    <tr><th>Email</th><th>Protocol</th><th>Host</th><th>Libraries</th><th>Total sent</th><th></th></tr>
    {% for f in friends %}
    <tr>
      <td>{{ f.email }}</td>
      <td><span class="badge">{{ f.protocol }}</span></td>
      <td>{{ f.sftp_host or "—" }}</td>
      <td>{% for lib in f.libraries_list %}<span class="badge">{{ lib }}</span>{% endfor %}</td>
      <td class="size">{{ f.total_sent }}</td>
      <td>
        <div class="actions">
          <a class="btn btn-ghost btn-sm" href="/share/admin/friend/{{ f.id }}/edit">Edit</a>
          <a class="btn btn-ghost btn-sm" href="/share/admin/friend/{{ f.id }}/titles">Titles</a>
          <form method="post" action="/share/admin/friend/{{ f.id }}/delete"
                onsubmit="return confirm('Remove {{ f.email }}?')" style="display:inline">
            <button class="btn btn-danger btn-sm" type="submit">Remove</button>
          </form>
        </div>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#666;margin-bottom:24px">No friends added yet.</p>
  {% endif %}

  <div class="section-gap">
    <h3>Add friend</h3>
    <div class="form-card">
      <form method="post" action="/share/admin/friend/new">
        <div class="field"><label>Cloudflare Access email</label>
          <input type="email" name="email" required placeholder="steve@example.com"></div>
        <div class="field"><label>Libraries they can see</label>
          <div class="checkbox-group">
            <label><input type="checkbox" name="libraries" value="movies" checked> Movies</label>
            <label><input type="checkbox" name="libraries" value="tv" checked> TV Shows</label>
            <label><input type="checkbox" name="libraries" value="music" checked> Music</label>
          </div>
          <div class="hint">Use the Titles button to restrict which specific titles they can see.</div>
        </div>
        <p style="font-size:0.82rem;color:#555;margin:-8px 0 18px;border-top:1px solid #222;padding-top:16px">
          Optional — upload destination (enables the "Upload to me" button)
        </p>
        <div class="field">
          <label>Protocol</label>
          <select name="protocol" onchange="onProtoChange(this)">
            <option value="ftps">FTPS (FTP over TLS)</option>
            <option value="sftp">SFTP (SSH)</option>
          </select>
        </div>
        <div class="field"><label>Host</label>
          <input type="text" name="sftp_host" placeholder="1.2.3.4"></div>
        <div class="field"><label>Port</label>
          <input type="number" name="sftp_port" value="21"></div>
        <div class="field"><label>Username</label>
          <input type="text" name="sftp_user"></div>
        <div class="field"><label>Password</label>
          <input type="password" name="sftp_password"></div>
        <div class="field">
          <button class="btn btn-ghost" type="button" id="test-btn" onclick="testConn(null)">Test connection</button>
          <div class="test-result" id="test-result"></div>
        </div>
        <div class="field"><label>Remote directory</label>
          <input type="text" name="sftp_remote_dir" value="/"></div>
        <button class="btn btn-primary" type="submit">Add friend</button>
      </form>
    </div>
  </div>
</main>''' + _PROTO_JS + '''</body></html>'''

ADMIN_EDIT_HTML = _head('Edit — Admin') + _NAV + '''
<main>
  <div class="breadcrumb"><a href="/share/admin">Admin</a> / Edit friend</div>
  <h2>{{ f.email }}</h2>
  <div class="form-card">
    <form method="post" action="/share/admin/friend/{{ f.id }}/edit">
      <div class="field">
        <label>Protocol</label>
        <select name="protocol" onchange="onProtoChange(this)">
          <option value="ftps" {% if f.protocol == "ftps" %}selected{% endif %}>FTPS (FTP over TLS)</option>
          <option value="sftp" {% if f.protocol == "sftp" %}selected{% endif %}>SFTP (SSH)</option>
        </select>
      </div>
      <div class="field"><label>Host</label>
        <input type="text" name="sftp_host" value="{{ f.sftp_host }}"></div>
      <div class="field"><label>Port</label>
        <input type="number" name="sftp_port" value="{{ f.sftp_port }}"></div>
      <div class="field"><label>Username</label>
        <input type="text" name="sftp_user" value="{{ f.sftp_user }}"></div>
      <div class="field"><label>Password</label>
        <input type="password" name="sftp_password" placeholder="Leave blank to keep current"></div>
      <div class="field">
        <button class="btn btn-ghost" type="button" id="test-btn" onclick="testConn({{ f.id }})">Test connection</button>
        <div class="test-result" id="test-result"></div>
      </div>
      <div class="field"><label>Remote directory</label>
        <input type="text" name="sftp_remote_dir" value="{{ f.sftp_remote_dir }}"></div>
      <div class="field"><label>Libraries they can see</label>
        <div class="checkbox-group">
          {% for lib in all_libraries %}
          <label><input type="checkbox" name="libraries" value="{{ lib }}"
            {% if lib in f.libraries_list %}checked{% endif %}> {{ lib|capitalize }}</label>
          {% endfor %}
        </div>
      </div>
      <button class="btn btn-primary" type="submit">Save changes</button>
    </form>
  </div>
</main>''' + _PROTO_JS + '''</body></html>'''

ADMIN_TITLES_HTML = _head('Titles — Admin') + _NAV + '''
<style>
.tv-show-list { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:8px; overflow:hidden; }
.tv-show-row { border-bottom:1px solid #222; }
.tv-show-row:last-child { border-bottom:none; }
.show-header { display:flex; align-items:center; gap:10px; padding:10px 14px; cursor:pointer; }
.show-header:hover { background:#222; }
.show-toggle { background:none; border:none; color:#666; font-size:0.8rem; cursor:pointer;
               margin-left:auto; padding:2px 6px; }
.season-grid { display:flex; flex-wrap:wrap; gap:8px; padding:8px 14px 12px 38px;
               background:#141414; border-top:1px solid #222; }
.season-label { display:flex; align-items:center; gap:6px; font-size:0.82rem; color:#ccc;
                cursor:pointer; white-space:nowrap; }
.season-label input { cursor:pointer; }
.all-seasons-row { padding:6px 14px 6px 38px; background:#141414;
                   border-top:1px solid #1e1e1e; }
.all-seasons-row label { font-size:0.78rem; color:#888; display:flex; align-items:center; gap:6px; cursor:pointer; }
</style>
<main>
  <div class="breadcrumb"><a href="/share/admin">Admin</a> / Title access</div>
  <h2>{{ f.email }} — Title Access</h2>
  <p style="color:#888;font-size:0.88rem;margin-bottom:24px">
    Checked = allowed. Leave a library unrestricted for full access.
    For TV shows you can allow all seasons of a show, or expand it and pick specific seasons.
  </p>
  {% if saved %}<div class="alert-ok">Saved.</div>{% endif %}
  <form method="post" id="titles-form">
    <input type="hidden" name="tv_config" id="tv-config" value="">

    <!-- ── Movies ── -->
    <div class="section-gap">
      <h3>Movies ({{ movie_items|length }} titles)</h3>
      <div style="margin-bottom:12px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:0.9rem;color:#e8e8e8">
          <input type="checkbox" name="restrict_movies" id="restrict-movies"
                 {% if movie_restricted %}checked{% endif %}
                 onchange="document.getElementById('picker-movies').style.display=this.checked?'':'none'">
          Restrict to selected movies only
        </label>
      </div>
      <div id="picker-movies" {% if not movie_restricted %}style="display:none"{% endif %}>
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <button type="button" class="btn btn-ghost btn-sm"
                  onclick="document.querySelectorAll('[name=titles_movies]').forEach(function(e){e.checked=true})">Select all</button>
          <button type="button" class="btn btn-ghost btn-sm"
                  onclick="document.querySelectorAll('[name=titles_movies]').forEach(function(e){e.checked=false})">Clear all</button>
        </div>
        <input class="search-input" type="text" placeholder="Filter movies…" oninput="filterMovies(this)">
        <div class="titles-list" id="list-movies">
          {% for folder, title, year in movie_items %}
          <div class="title-item" data-title="{{ title|lower }}">
            <input type="checkbox" name="titles_movies" value="{{ folder }}"
                   id="m-{{ loop.index }}"
                   {% if not movie_restricted or folder in allowed_movies %}checked{% endif %}>
            <label for="m-{{ loop.index }}">{{ title }}{% if year %} ({{ year }}){% endif %}</label>
          </div>
          {% endfor %}
        </div>
      </div>
      {% if not movie_restricted %}<p style="color:#4ade80;font-size:0.84rem">✓ Full access</p>{% endif %}
    </div>

    <!-- ── TV Shows ── -->
    <div class="section-gap">
      <h3>TV Shows ({{ tv_items|length }} shows)</h3>
      <div style="margin-bottom:12px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:0.9rem;color:#e8e8e8">
          <input type="checkbox" id="restrict-tv"
                 {% if tv_restricted %}checked{% endif %}
                 onchange="document.getElementById('picker-tv').style.display=this.checked?'':'none'">
          Restrict to selected shows/seasons only
        </label>
      </div>
      <div id="picker-tv" {% if not tv_restricted %}style="display:none"{% endif %}>
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <button type="button" class="btn btn-ghost btn-sm" onclick="tvSelectAll(true)">Select all shows</button>
          <button type="button" class="btn btn-ghost btn-sm" onclick="tvSelectAll(false)">Clear all</button>
        </div>
        <input class="search-input" type="text" placeholder="Filter shows…" oninput="filterShows(this)">
        <div class="tv-show-list">
          {% for show in tv_items %}
          {% set sa = allowed_tv.get(show.folder) %}
          <div class="tv-show-row" data-folder="{{ show.folder }}" data-title="{{ show.title|lower }}">
            <div class="show-header" onclick="toggleSeasons('{{ loop.index }}')">
              <input type="checkbox" class="show-enable" data-idx="{{ loop.index }}"
                     {% if tv_restricted and (sa is not none) %}checked{% elif not tv_restricted %}checked{% endif %}
                     onclick="event.stopPropagation(); onShowToggle({{ loop.index }})">
              <span style="flex:1;font-size:0.9rem;color:#e8e8e8">
                {{ show.title }}{% if show.year %} <span style="color:#555">({{ show.year }})</span>{% endif %}
              </span>
              {% if show.seasons %}
              <button type="button" class="show-toggle" id="tog-{{ loop.index }}">▼ {{ show.seasons|length }} seasons</button>
              {% endif %}
            </div>
            {% if show.seasons %}
            <div id="seasons-{{ loop.index }}" style="display:none">
              <div class="all-seasons-row">
                <label>
                  <input type="checkbox" class="all-seasons-cb" data-idx="{{ loop.index }}"
                         onchange="onAllSeasonsToggle({{ loop.index }}, this.checked)"
                         {% if sa is none %}checked{% endif %}>
                  All seasons
                </label>
              </div>
              <div class="season-grid" id="season-grid-{{ loop.index }}"
                   {% if sa is none %}style="display:none"{% endif %}>
                {% for season in show.seasons %}
                <label class="season-label">
                  <input type="checkbox" class="season-cb" data-idx="{{ loop.index }}" value="{{ season }}"
                         {% if sa is none or season in (sa or []) %}checked{% endif %}>
                  {{ season }}
                </label>
                {% endfor %}
              </div>
            </div>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </div>
      {% if not tv_restricted %}<p style="color:#4ade80;font-size:0.84rem">✓ Full access</p>{% endif %}
    </div>

    <div style="margin-top:28px">
      <button class="btn btn-primary" type="submit">Save title access</button>
      <a href="/share/admin" class="btn btn-ghost" style="margin-left:8px">Cancel</a>
    </div>
  </form>
</main>
<script>
function filterMovies(inp) {
  var q = inp.value.toLowerCase();
  document.querySelectorAll('#list-movies .title-item').forEach(function(el) {
    el.style.display = el.dataset.title.indexOf(q) >= 0 ? '' : 'none';
  });
}
function filterShows(inp) {
  var q = inp.value.toLowerCase();
  document.querySelectorAll('.tv-show-row').forEach(function(el) {
    el.style.display = el.dataset.title.indexOf(q) >= 0 ? '' : 'none';
  });
}
function toggleSeasons(idx) {
  var el = document.getElementById('seasons-' + idx);
  var tog = document.getElementById('tog-' + idx);
  if (!el) return;
  var open = el.style.display !== 'none';
  el.style.display = open ? 'none' : '';
  if (tog) tog.textContent = open ? tog.textContent.replace('▲','▼') : tog.textContent.replace('▼','▲');
}
function onShowToggle(idx) {
  var el = document.getElementById('seasons-' + idx);
  if (el) el.style.display = 'none'; // collapse on toggle
}
function onAllSeasonsToggle(idx, allChecked) {
  var grid = document.getElementById('season-grid-' + idx);
  if (grid) grid.style.display = allChecked ? 'none' : '';
  if (!allChecked) {
    // ensure at least the first season is checked
    var cbs = document.querySelectorAll('.season-cb[data-idx="' + idx + '"]');
    var any = false;
    cbs.forEach(function(cb){ if(cb.checked) any=true; });
    if (!any && cbs.length) cbs[0].checked = true;
  }
}
function tvSelectAll(checked) {
  document.querySelectorAll('.show-enable').forEach(function(cb){ cb.checked = checked; });
  if (checked) {
    document.querySelectorAll('.all-seasons-cb').forEach(function(cb){ cb.checked = true; });
    document.querySelectorAll('[id^=season-grid-]').forEach(function(el){ el.style.display='none'; });
  }
}
document.getElementById('titles-form').addEventListener('submit', function() {
  if (!document.getElementById('restrict-tv').checked) {
    document.getElementById('tv-config').value = '';
    return;
  }
  var config = {};
  document.querySelectorAll('.tv-show-row').forEach(function(row) {
    var folder = row.dataset.folder;
    var enableCb = row.querySelector('.show-enable');
    if (!enableCb || !enableCb.checked) return;
    var allCb = row.querySelector('.all-seasons-cb');
    if (!allCb || allCb.checked) {
      config[folder] = null; // all seasons
    } else {
      var seasons = [];
      row.querySelectorAll('.season-cb').forEach(function(cb){
        if (cb.checked) seasons.push(cb.value);
      });
      config[folder] = seasons;
    }
  });
  document.getElementById('tv-config').value = JSON.stringify(config);
});
</script>
</body></html>'''


# ── Route helpers ─────────────────────────────────────────────────────────────

def _render(template, email, **kwargs):
    return render_template_string(
        template, email=email, is_admin=_is_admin(email), icons=_LIB_ICONS, **kwargs
    )


def _require_auth():
    email = _current_email()
    if not email:
        return None, ('Access denied: no Cloudflare Access identity found', 403)
    return email, None


def _require_admin():
    email, err = _require_auth()
    if err:
        return None, err
    if not _is_admin(email):
        return None, ('Forbidden', 403)
    return email, None


def _friends_list():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute('SELECT * FROM friends ORDER BY created_at').fetchall()
    friends = []
    for r in rows:
        f = dict(r)
        f['libraries_list'] = json.loads(r['libraries'])
        f['protocol'] = r['protocol'] if 'protocol' in r.keys() else 'sftp'
        total = db.execute(
            "SELECT COALESCE(SUM(bytes_done),0) FROM uploads WHERE friend_email=? AND status='done'",
            (r['email'],),
        ).fetchone()[0]
        f['total_sent'] = _fmt_bytes(total)
        friends.append(f)
    db.close()
    return friends


# ── Routes ────────────────────────────────────────────────────────────────────

@share_bp.route('/')
def share_index():
    email, err = _require_auth()
    if err:
        return err
    return _render(INDEX_HTML, email, libraries=_allowed_libraries(email))


@share_bp.route('/browse/<library>')
def share_browse(library):
    email, err = _require_auth()
    if err:
        return err
    if library not in _allowed_libraries(email):
        return 'Not found', 404

    rel_path = request.args.get('path', '')

    # Poster grid view for movies/tv at library root
    if not rel_path and library in ('movies', 'tv'):
        if library == 'movies':
            raw = _movies_for_user(email)
            items = [{
                'title': m['title'],
                'year': m.get('year', ''),
                'poster': _poster_url(m.get('images', [])),
                'folder': os.path.basename(m['path']),
            } for m in raw if m.get('path')]
        else:
            raw = _series_for_user(email)
            items = [{
                'title': s['title'],
                'year': s.get('year', ''),
                'poster': _poster_url(s.get('images', [])),
                'folder': os.path.basename(s['path']),
            } for s in raw if s.get('path')]
        return _render(POSTER_GRID_HTML, email, library=library, items=items)

    # File browser — enforce title-level access before touching the filesystem
    if not _check_title_access(email, library, rel_path):
        return 'Not found', 404
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.isdir(full):
        return 'Not a directory', 400
    entries = []
    for name in sorted(os.listdir(full)):
        p = os.path.join(full, name)
        rel = os.path.join(rel_path, name) if rel_path else name
        entries.append({'name': name, 'is_dir': os.path.isdir(p), 'rel': rel})
    parent = os.path.dirname(rel_path) if rel_path else None
    cfg = get_friend_config(email)
    can_upload = bool(cfg and cfg.get('host')) if not _is_admin(email) else False
    return _render(BROWSE_HTML, email, library=library, entries=entries,
                   rel_path=rel_path, parent=parent, can_upload=can_upload)


@share_bp.route('/download/<library>')
def share_download(library):
    email, err = _require_auth()
    if err:
        return err
    if library not in _allowed_libraries(email):
        return 'Not found', 404
    rel_path = request.args.get('path', '')
    if not _check_title_access(email, library, rel_path):
        return 'Not found', 404
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.isfile(full):
        return 'Not a file', 400
    return send_file(full, as_attachment=True, conditional=True)


@share_bp.route('/upload', methods=['POST'])
def share_upload():
    email, err = _require_auth()
    if err:
        return err
    friend_cfg = get_friend_config(email)
    if not friend_cfg or not friend_cfg.get('host'):
        return 'No upload destination configured for your account', 403
    library = request.form.get('library')
    rel_path = request.form.get('rel_path', '')
    if library not in _allowed_libraries(email):
        return 'Not found', 404
    if not _check_title_access(email, library, rel_path):
        return 'Not found', 404
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.exists(full):
        return 'Not found', 404

    if os.path.isdir(full):
        bytes_total = sum(os.path.getsize(os.path.join(r, f))
                         for r, _, fs in os.walk(full) for f in fs)
    else:
        bytes_total = os.path.getsize(full)

    db = sqlite3.connect(DB_PATH)
    cur = db.execute(
        'INSERT INTO uploads (friend_email, library, rel_path, status, bytes_total, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (email, library, rel_path, 'pending', bytes_total, datetime.utcnow().isoformat()),
    )
    upload_id = cur.lastrowid
    db.commit()
    db.close()

    threading.Thread(target=perform_upload,
                     args=(upload_id, friend_cfg, LIBRARIES[library], rel_path),
                     daemon=True).start()
    return redirect(f'/share/status/{upload_id}')


@share_bp.route('/status/<int:upload_id>')
def share_status(upload_id):
    email, err = _require_auth()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
    db.close()
    if not row:
        return 'Not found', 404
    return _render(STATUS_HTML, email, row=row)


@share_bp.route('/usage')
def share_usage():
    email, err = _require_auth()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    if _is_admin(email):
        friends = db.execute('SELECT email FROM friends ORDER BY created_at').fetchall()
        windows = list(USAGE_WINDOWS.keys())
        admin_rows = []
        for (femail,) in friends:
            values = []
            for days in USAGE_WINDOWS.values():
                cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
                total = db.execute(
                    "SELECT COALESCE(SUM(bytes_done),0) FROM uploads "
                    "WHERE friend_email=? AND status='done' AND created_at>=?",
                    (femail, cutoff),
                ).fetchone()[0]
                values.append(_fmt_bytes(total))
            admin_rows.append({'email': femail, 'cols': values})
        db.close()
        return _render(USAGE_HTML, email, admin_rows=admin_rows, windows=windows)
    else:
        usage = {}
        for label, days in USAGE_WINDOWS.items():
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
            total = db.execute(
                "SELECT COALESCE(SUM(bytes_done),0) FROM uploads "
                "WHERE friend_email=? AND status='done' AND created_at>=?",
                (email, cutoff),
            ).fetchone()[0]
            usage[label] = _fmt_bytes(total)
        db.close()
        return _render(USAGE_HTML, email, usage=usage)


@share_bp.route('/settings', methods=['GET', 'POST'])
def share_settings():
    email, err = _require_auth()
    if err:
        return err
    row = _get_friend(email)
    saved = False

    if request.method == 'POST' and row:
        new_password = request.form.get('sftp_password', '').strip()
        db = sqlite3.connect(DB_PATH)
        if new_password:
            db.execute(
                'UPDATE friends SET protocol=?, sftp_host=?, sftp_port=?, sftp_user=?, '
                'sftp_password=?, sftp_remote_dir=? WHERE email=?',
                (request.form.get('protocol', 'sftp'),
                 request.form.get('sftp_host', ''),
                 int(request.form.get('sftp_port') or 22),
                 request.form.get('sftp_user', ''),
                 new_password,
                 request.form.get('sftp_remote_dir', '/'),
                 email),
            )
        else:
            db.execute(
                'UPDATE friends SET protocol=?, sftp_host=?, sftp_port=?, sftp_user=?, '
                'sftp_remote_dir=? WHERE email=?',
                (request.form.get('protocol', 'sftp'),
                 request.form.get('sftp_host', ''),
                 int(request.form.get('sftp_port') or 22),
                 request.form.get('sftp_user', ''),
                 request.form.get('sftp_remote_dir', '/'),
                 email),
            )
        db.commit()
        db.close()
        row = _get_friend(email)
        saved = True

    f = dict(row) if row else None
    if f and 'protocol' not in f:
        f['protocol'] = 'sftp'
    return _render(SETTINGS_HTML, email, f=f, saved=saved)


@share_bp.route('/test-connection', methods=['POST'])
def friend_test_connection():
    email, err = _require_auth()
    if err:
        return {'ok': False, 'error': 'Not authenticated'}, 403
    protocol = request.form.get('protocol', 'sftp')
    host = request.form.get('sftp_host', '').strip()
    port_raw = request.form.get('sftp_port', '')
    port = int(port_raw) if port_raw.strip().isdigit() else 0
    user = request.form.get('sftp_user', '').strip()
    password = request.form.get('sftp_password', '').strip()
    if not password:
        row = _get_friend(email)
        if row:
            password = row['sftp_password']
    ok, error = _test_connection(protocol, host, port, user, password)
    return {'ok': ok, 'error': error}


# ── Shareable link templates ─────────────────────────────────────────────────

LINKS_HTML = _head('Links — Admin') + _NAV + '''
<main>
  <h2>Shareable Links</h2>
  {% if links %}
  <table class="stat-table" style="width:100%">
    <tr>
      <th>Label</th><th>Library</th><th>Expires</th>
      <th>Downloads</th><th>URL</th><th></th>
    </tr>
    {% for lnk in links %}
    <tr>
      <td>{{ lnk.label }}</td>
      <td>{{ lnk.library }}</td>
      <td style="white-space:nowrap;color:{% if lnk.expired %}#f87171{% else %}#aaa{% endif %}">
        {{ lnk.expires_at[:16].replace("T"," ") }} UTC{% if lnk.expired %} (expired){% endif %}
      </td>
      <td>{{ lnk.download_count }}{% if lnk.max_downloads %} / {{ lnk.max_downloads }}{% endif %}</td>
      <td style="font-size:0.75rem;word-break:break-all">
        <a href="/share/dl/{{ lnk.token }}" target="_blank">/share/dl/{{ lnk.token[:12] }}…</a>
      </td>
      <td>
        <form method="post" action="/share/admin/links/{{ lnk.id }}/revoke" style="display:inline">
          <button class="btn btn-danger btn-sm" type="submit"
                  onclick="return confirm('Revoke this link?')">Revoke</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#888">No active links.</p>
  {% endif %}
</main></body></html>'''

CREATE_LINK_HTML = _head('Create Link — Admin') + _NAV + '''
<main>
  <div class="form-card" style="max-width:480px">
    <h2 style="margin-bottom:1.2rem">Create Shareable Link</h2>
    <p style="color:#888;margin-bottom:1.2rem;font-size:0.9rem">{{ label }}</p>
    <form method="post">
      <input type="hidden" name="library" value="{{ library }}">
      <input type="hidden" name="rel_path" value="{{ rel_path }}">
      <div class="field">
        <label>Expires after (hours)</label>
        <input type="number" name="expires_hours" value="72" min="1" max="8760">
      </div>
      <div class="field">
        <label>Max downloads (0 = unlimited)</label>
        <input type="number" name="max_downloads" value="0" min="0">
      </div>
      <button class="btn btn-primary" type="submit">Generate Link</button>
      <a class="btn btn-ghost" href="/share/browse/{{ library }}?path={{ rel_path | urlencode }}"
         style="margin-left:8px">Cancel</a>
    </form>
  </div>
</main></body></html>'''

LINK_CREATED_HTML = _head('Link Created — Admin') + _NAV + '''
<main>
  <div class="form-card" style="max-width:520px">
    <h2 style="margin-bottom:1rem">Link Created</h2>
    <p style="color:#888;font-size:0.9rem;margin-bottom:1rem">{{ label }}</p>
    <div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:1rem;
                word-break:break-all;font-family:monospace;font-size:0.85rem;color:#60a5fa">
      {{ url }}
    </div>
    <p style="color:#888;font-size:0.82rem;margin-top:0.75rem">
      Expires: {{ expires_at[:16].replace("T"," ") }} UTC ·
      Max downloads: {% if max_downloads %}{{ max_downloads }}{% else %}unlimited{% endif %}
    </p>
    <div style="margin-top:1.2rem;display:flex;gap:8px">
      <a class="btn btn-ghost" href="/share/admin/links">View all links</a>
      <a class="btn btn-ghost" href="/share/browse/{{ library }}?path={{ rel_path | urlencode }}">Back to folder</a>
    </div>
  </div>
</main></body></html>'''


PREPARING_HTML = '''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preparing Download — Media Share</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0f0f0f; color: #e8e8e8; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; padding: 24px; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
        padding: 40px 36px; max-width: 480px; width: 100%; }
h2 { font-size: 1.2rem; font-weight: 600; margin-bottom: 8px; color: #fff; }
.lbl { color: #888; font-size: 0.9rem; margin-bottom: 28px;
       overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.progress-bar { height: 6px; background: #2a2a2a; border-radius: 3px;
                overflow: hidden; margin-bottom: 12px; }
.progress-fill { height: 100%; background: #2563eb; border-radius: 3px; transition: width 0.4s; }
.st { font-size: 0.85rem; color: #888; }
.eta { font-size: 0.82rem; color: #555; margin-top: 6px; min-height: 1.1em; }
.err { color: #f87171; font-size: 0.88rem; margin-top: 14px; display: none; }
</style></head><body>
<div class="card">
  <h2>Preparing download…</h2>
  <div class="lbl" title="{{ label }}">{{ label }}</div>
  <div class="progress-bar"><div class="progress-fill" id="pf" style="width:0%"></div></div>
  <div class="st" id="st">Starting…</div>
  <div class="eta" id="eta"></div>
  <div class="err" id="err"></div>
</div>
<script>
(function poll() {
  fetch('/share/dl/{{ token }}/status.json')
    .then(function(r) { return r.json(); })
    .then(function(j) {
      if (j.status === 'ready') { window.location = '/share/dl/{{ token }}/file'; return; }
      if (j.status === 'error' || j.status === 'expired') {
        var e = document.getElementById('err');
        e.textContent = j.status === 'expired' ? 'This link has expired.' : 'Error: ' + (j.error || 'unknown');
        e.style.display = '';
        document.getElementById('st').textContent = j.status === 'expired' ? 'Expired.' : 'Failed.';
        return;
      }
      var pct = j.bytes_total > 0 ? Math.round(j.bytes_done / j.bytes_total * 100) : 0;
      document.getElementById('pf').style.width = pct + '%';
      document.getElementById('st').textContent = pct + '% — ' + j.done_fmt + ' / ' + j.total_fmt;
      var s = j.eta_seconds;
      document.getElementById('eta').textContent = s > 0
        ? 'Est. ' + (s < 60 ? s + 's' : Math.round(s / 60) + 'm') + ' until download starts'
        : '';
      setTimeout(poll, 1000);
    })
    .catch(function() { setTimeout(poll, 2000); });
})();
</script>
</body></html>'''


# ── Admin routes ──────────────────────────────────────────────────────────────

@share_bp.route('/admin')
def share_admin():
    email, err = _require_admin()
    if err:
        return err
    return _render(ADMIN_HTML, email, friends=_friends_list(), all_libraries=ALL_LIBRARIES)


@share_bp.route('/admin/friend/new', methods=['POST'])
def admin_friend_new():
    email, err = _require_admin()
    if err:
        return err
    f_email = request.form.get('email', '').strip().lower()
    if not f_email:
        return 'Email required', 400
    libraries = json.dumps(request.form.getlist('libraries') or ALL_LIBRARIES)
    db = sqlite3.connect(DB_PATH)
    try:
        db.execute(
            'INSERT INTO friends (email, protocol, sftp_host, sftp_port, sftp_user, sftp_password, '
            'sftp_remote_dir, libraries, rate_limit_mbit, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (f_email, request.form.get('protocol', 'sftp'),
             request.form.get('sftp_host', ''),
             int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''), request.form.get('sftp_password', ''),
             request.form.get('sftp_remote_dir', '/'), libraries,
             5.0, datetime.utcnow().isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return 'A friend with that email already exists', 400
    finally:
        db.close()
    return redirect('/share/admin')


@share_bp.route('/admin/friend/<int:friend_id>/edit', methods=['GET', 'POST'])
def admin_friend_edit(friend_id):
    email, err = _require_admin()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM friends WHERE id=?', (friend_id,)).fetchone()
    db.close()
    if not row:
        return 'Not found', 404

    if request.method == 'GET':
        f = dict(row)
        f['libraries_list'] = json.loads(row['libraries'])
        f['protocol'] = row['protocol'] if 'protocol' in row.keys() else 'sftp'
        return _render(ADMIN_EDIT_HTML, email, f=f, all_libraries=ALL_LIBRARIES)

    new_password = request.form.get('sftp_password', '').strip()
    libraries = json.dumps(request.form.getlist('libraries') or ALL_LIBRARIES)
    db = sqlite3.connect(DB_PATH)
    if new_password:
        db.execute(
            'UPDATE friends SET protocol=?, sftp_host=?, sftp_port=?, sftp_user=?, sftp_password=?, '
            'sftp_remote_dir=?, libraries=? WHERE id=?',
            (request.form.get('protocol', 'sftp'),
             request.form.get('sftp_host', ''), int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''), new_password,
             request.form.get('sftp_remote_dir', '/'), libraries, friend_id),
        )
    else:
        db.execute(
            'UPDATE friends SET protocol=?, sftp_host=?, sftp_port=?, sftp_user=?, '
            'sftp_remote_dir=?, libraries=? WHERE id=?',
            (request.form.get('protocol', 'sftp'),
             request.form.get('sftp_host', ''), int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''),
             request.form.get('sftp_remote_dir', '/'), libraries, friend_id),
        )
    db.commit()
    db.close()
    return redirect('/share/admin')


@share_bp.route('/admin/friend/<int:friend_id>/titles', methods=['GET', 'POST'])
def admin_friend_titles(friend_id):
    email, err = _require_admin()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM friends WHERE id=?', (friend_id,)).fetchone()
    db.close()
    if not row:
        return 'Not found', 404

    f = dict(row)
    saved = False

    if request.method == 'POST':
        allowed_titles = {}
        if 'restrict_movies' in request.form:
            allowed_titles['movies'] = request.form.getlist('titles_movies')
        if 'restrict-tv' in request.form or request.form.get('tv_config'):
            raw_tv = request.form.get('tv_config', '').strip()
            if raw_tv:
                try:
                    allowed_titles['tv'] = json.loads(raw_tv)
                except Exception:
                    allowed_titles['tv'] = {}
            else:
                allowed_titles['tv'] = {}
        db2 = sqlite3.connect(DB_PATH)
        db2.execute('UPDATE friends SET allowed_titles=? WHERE id=?',
                    (json.dumps(allowed_titles) if allowed_titles else None, friend_id))
        db2.commit()
        db2.close()
        f['allowed_titles'] = json.dumps(allowed_titles) if allowed_titles else None
        saved = True

    allowed = {}
    if f.get('allowed_titles'):
        try:
            allowed = json.loads(f['allowed_titles'])
        except Exception:
            allowed = {}

    movie_restricted = 'movies' in allowed
    allowed_movies = set(allowed.get('movies') or [])

    tv_restricted = 'tv' in allowed
    tv_map = allowed.get('tv', {})
    # Normalise legacy list format to dict
    if isinstance(tv_map, list):
        tv_map = {f: None for f in tv_map}
    # Build per-show season data from filesystem + Sonarr
    series_raw = _arr_get(SONARR_URL, SONARR_API_KEY, 'series')
    series_raw = [s for s in series_raw if s.get('path')]
    series_raw.sort(key=lambda s: s.get('sortTitle', s.get('title', '')).lower())
    tv_items = []
    for s in series_raw:
        folder = os.path.basename(s['path'])
        show_path = os.path.join(LIBRARIES['tv'], folder)
        seasons = []
        if os.path.isdir(show_path):
            seasons = sorted(
                d for d in os.listdir(show_path)
                if os.path.isdir(os.path.join(show_path, d))
            )
        tv_items.append({
            'folder': folder,
            'title': s['title'],
            'year': s.get('year', ''),
            'seasons': seasons,
        })

    movies_raw = _arr_get(RADARR_URL, RADARR_API_KEY, 'movie')
    movies_raw = [m for m in movies_raw if m.get('hasFile') and m.get('path')]
    movies_raw.sort(key=lambda m: m.get('sortTitle', m.get('title', '')).lower())
    movie_items = [(os.path.basename(m['path']), m['title'], m.get('year', ''))
                   for m in movies_raw]

    return _render(ADMIN_TITLES_HTML, email, f=f,
                   movie_items=movie_items, movie_restricted=movie_restricted, allowed_movies=allowed_movies,
                   tv_items=tv_items, tv_restricted=tv_restricted, allowed_tv=tv_map,
                   saved=saved)


@share_bp.route('/admin/friend/<int:friend_id>/delete', methods=['POST'])
def admin_friend_delete(friend_id):
    email, err = _require_admin()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    db.execute('DELETE FROM friends WHERE id=?', (friend_id,))
    db.commit()
    db.close()
    return redirect('/share/admin')


@share_bp.route('/admin/test-connection', methods=['POST'])
def admin_test_connection():
    email, err = _require_admin()
    if err:
        return {'ok': False, 'error': 'Forbidden'}, 403
    protocol = request.form.get('protocol', 'sftp')
    host = request.form.get('sftp_host', '').strip()
    port_raw = request.form.get('sftp_port', '')
    port = int(port_raw) if port_raw.strip().isdigit() else 0
    user = request.form.get('sftp_user', '').strip()
    password = request.form.get('sftp_password', '').strip()
    if not password:
        friend_id = request.form.get('friend_id')
        if friend_id:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
            r = db.execute('SELECT sftp_password FROM friends WHERE id=?', (friend_id,)).fetchone()
            db.close()
            if r:
                password = r['sftp_password']
    ok, error = _test_connection(protocol, host, port, user, password)
    return {'ok': ok, 'error': error}


@share_bp.route('/admin/create-link', methods=['GET', 'POST'])
def admin_create_link():
    email, err = _require_admin()
    if err:
        return err
    library = request.args.get('library') or request.form.get('library', '')
    rel_path = request.args.get('path') or request.form.get('rel_path', '')
    if library not in LIBRARIES:
        return 'Invalid library', 400
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.exists(full):
        return 'Not found', 404

    label = os.path.basename(full.rstrip('/')) or library

    if request.method == 'GET':
        return _render(CREATE_LINK_HTML, email, library=library, rel_path=rel_path, label=label)

    expires_hours = int(request.form.get('expires_hours') or 72)
    max_downloads = int(request.form.get('max_downloads') or 0)
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=expires_hours)).isoformat()
    created_at = datetime.utcnow().isoformat()

    db = sqlite3.connect(DB_PATH)
    db.execute(
        'INSERT INTO share_links (token, library, rel_path, label, expires_at, max_downloads, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (token, library, rel_path, label, expires_at, max_downloads, created_at),
    )
    db.commit()
    db.close()

    base_url = request.host_url.rstrip('/')
    url = f'{base_url}/share/dl/{token}'
    return _render(LINK_CREATED_HTML, email, library=library, rel_path=rel_path,
                   label=label, url=url, expires_at=expires_at, max_downloads=max_downloads)


@share_bp.route('/admin/links')
def admin_links():
    email, err = _require_admin()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute('SELECT * FROM share_links ORDER BY created_at DESC').fetchall()
    db.close()
    now = datetime.utcnow().isoformat()
    links = []
    for r in rows:
        lnk = dict(r)
        lnk['expired'] = r['expires_at'] < now or (r['max_downloads'] > 0 and r['download_count'] >= r['max_downloads'])
        links.append(lnk)
    return _render(LINKS_HTML, email, links=links)


@share_bp.route('/admin/links/<int:link_id>/revoke', methods=['POST'])
def admin_link_revoke(link_id):
    email, err = _require_admin()
    if err:
        return err
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT token FROM share_links WHERE id=?', (link_id,)).fetchone()
    db.execute('DELETE FROM share_links WHERE id=?', (link_id,))
    db.commit()
    db.close()
    if row:
        with _zip_jobs_lock:
            _expire_zip_job(row[0])
    return redirect('/share/admin/links')


def _validate_share_token(token):
    """Returns (row, full_path, error_response) — error_response is a tuple or None."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM share_links WHERE token=?', (token,)).fetchone()
    db.close()
    if not row:
        return None, None, ('Link not found', 404)
    now = datetime.utcnow().isoformat()
    if row['expires_at'] < now:
        return None, None, ('This link has expired', 410)
    if row['max_downloads'] > 0 and row['download_count'] >= row['max_downloads']:
        return None, None, ('Download limit reached', 410)
    try:
        full = safe_join(LIBRARIES[row['library']], row['rel_path'])
    except (ValueError, KeyError):
        return None, None, ('Invalid path', 500)
    if not os.path.exists(full):
        return None, None, ('Not found', 404)
    return row, full, None


@share_bp.route('/dl/<token>')
def share_dl(token):
    row, full, err = _validate_share_token(token)
    if err:
        return err

    if os.path.isfile(full):
        db = sqlite3.connect(DB_PATH)
        db.execute('UPDATE share_links SET download_count=download_count+1 WHERE id=?', (row['id'],))
        db.commit()
        db.close()
        return send_file(full, as_attachment=True, download_name=os.path.basename(full), conditional=True)

    if os.path.isdir(full):
        with _zip_jobs_lock:
            if token not in _zip_jobs:
                db = sqlite3.connect(DB_PATH)
                db.execute('UPDATE share_links SET download_count=download_count+1 WHERE id=?', (row['id'],))
                db.commit()
                db.close()
                _zip_jobs[token] = {
                    'status': 'zipping',
                    'label': row['label'] or os.path.basename(full.rstrip('/')),
                    'bytes_done': 0,
                    'bytes_total': 0,
                    'started_at': time.monotonic(),
                    'tmp_path': None,
                    'error': None,
                }
                threading.Thread(target=_zip_worker, args=(token, full, row['label']), daemon=True).start()
        return redirect(f'/share/dl/{token}/preparing')

    return 'Not found', 404


def _expire_zip_job(token):
    """Remove job from memory and delete its temp file. Call with lock held."""
    job = _zip_jobs.pop(token, None)
    if job and job.get('tmp_path'):
        try:
            os.unlink(job['tmp_path'])
        except Exception:
            pass


def _token_expired(token):
    """Return True if the share token no longer exists or has expired/hit its limit."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM share_links WHERE token=?', (token,)).fetchone()
    db.close()
    if not row:
        return True
    now = datetime.utcnow().isoformat()
    if row['expires_at'] < now:
        return True
    if row['max_downloads'] > 0 and row['download_count'] >= row['max_downloads']:
        return True
    return False


@share_bp.route('/dl/<token>/preparing')
def share_dl_preparing(token):
    with _zip_jobs_lock:
        job = _zip_jobs.get(token)
        if job and _token_expired(token):
            _expire_zip_job(token)
            return 'This link has expired', 410
    if not job:
        return redirect(f'/share/dl/{token}')
    if job['status'] == 'ready':
        return redirect(f'/share/dl/{token}/file')
    return render_template_string(PREPARING_HTML, token=token, label=job['label'])


@share_bp.route('/dl/<token>/status.json')
def share_dl_status(token):
    with _zip_jobs_lock:
        job = _zip_jobs.get(token)
        if job and _token_expired(token):
            _expire_zip_job(token)
            return {'status': 'expired'}, 410
    if not job:
        return {'status': 'not_found'}, 404
    eta = 0
    if job['status'] == 'zipping' and job['bytes_total'] > 0 and job['bytes_done'] > 0:
        elapsed = time.monotonic() - job['started_at']
        rate = job['bytes_done'] / elapsed
        if rate > 0:
            eta = int((job['bytes_total'] - job['bytes_done']) / rate)
    return {
        'status': job['status'],
        'bytes_done': job['bytes_done'],
        'bytes_total': job['bytes_total'],
        'done_fmt': _fmt_bytes(job['bytes_done']),
        'total_fmt': _fmt_bytes(job['bytes_total']),
        'eta_seconds': eta,
        'error': job.get('error'),
    }


@share_bp.route('/dl/<token>/file')
def share_dl_file(token):
    with _zip_jobs_lock:
        job = _zip_jobs.get(token)
        if job and _token_expired(token):
            _expire_zip_job(token)
            return 'This link has expired', 410
    if not job:
        return redirect(f'/share/dl/{token}')
    if job['status'] == 'zipping':
        return redirect(f'/share/dl/{token}/preparing')
    if job['status'] == 'error':
        return f'Zip failed: {job.get("error", "unknown error")}', 500

    tmp_path = job['tmp_path']
    label = job['label']

    @after_this_request
    def _cleanup(response, _token=token, _tmp=tmp_path):
        with _zip_jobs_lock:
            _zip_jobs.pop(_token, None)
        try:
            os.unlink(_tmp)
        except Exception:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name=f'{label}.zip', conditional=True)
