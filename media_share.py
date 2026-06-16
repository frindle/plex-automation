"""
Friend-facing media upload portal.

Lets friends authenticated via Cloudflare Access browse the Movies/TV/Music
libraries and push a file or folder to their own SFTP server. Identity comes
from the Cf-Access-Authenticated-User-Email header set by Cloudflare Access;
this only provides real auth if the app is unreachable except through the
Cloudflare Tunnel (no other ingress to this port).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import paramiko
from flask import Blueprint, redirect, render_template_string, request, send_file

log = logging.getLogger(__name__)

share_bp = Blueprint('share', __name__, url_prefix='/share')

LIBRARIES = {
    'movies': '/media/movies',
    'tv': '/media/tv',
    'music': '/media/music',
}

DB_PATH = os.environ.get('SHARE_DB_PATH', '/data/share_uploads.db')
UPLOAD_RATE_LIMIT_MBIT = float(os.environ.get('UPLOAD_RATE_LIMIT_MBIT', '5'))
RATE_LIMIT_BYTES_PER_SEC = UPLOAD_RATE_LIMIT_MBIT * 1_000_000 / 8

USAGE_WINDOWS = {
    '7 days': 7,
    '30 days': 30,
    '60 days': 60,
    '90 days': 90,
    '6 months': 182,
    '1 year': 365,
}


def init_db():
    db = sqlite3.connect(DB_PATH)
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
    db.commit()
    db.close()


def get_friend_config(email):
    friends = json.loads(os.environ.get('FRIENDS_CONFIG', '{}'))
    return friends.get(email)


def safe_join(root, rel_path):
    rel_path = rel_path or ''
    full = os.path.normpath(os.path.join(root, rel_path))
    root_normalized = os.path.normpath(root)
    if full != root_normalized and not full.startswith(root_normalized + os.sep):
        raise ValueError('Path traversal attempt')
    return full


class RateLimiter:
    """Sleeps inside the SFTP progress callback to cap throughput."""

    def __init__(self, rate_bytes_per_sec):
        self.rate = rate_bytes_per_sec
        self.start_time = time.monotonic()
        self.bytes_sent = 0

    def throttle(self, delta_bytes):
        if self.rate <= 0:
            return
        self.bytes_sent += delta_bytes
        elapsed = time.monotonic() - self.start_time
        expected_time = self.bytes_sent / self.rate
        sleep_for = expected_time - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)


def _mkdirs(sftp, remote_dir):
    parts = remote_dir.strip('/').split('/')
    path = ''
    for part in parts:
        if not part:
            continue
        path += '/' + part
        try:
            sftp.mkdir(path)
        except IOError:
            pass  # already exists


def perform_upload(upload_id, friend_cfg, local_root, rel_path):
    db = sqlite3.connect(DB_PATH)
    try:
        full_path = safe_join(local_root, rel_path)
        if os.path.isdir(full_path):
            files = []
            for dirpath, _, filenames in os.walk(full_path):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    files.append((fp, os.path.relpath(fp, full_path)))
        else:
            files = [(full_path, os.path.basename(full_path))]

        db.execute('UPDATE uploads SET status=? WHERE id=?', ('running', upload_id))
        db.commit()

        transport = paramiko.Transport((friend_cfg['host'], int(friend_cfg.get('port', 22))))
        transport.connect(username=friend_cfg['user'], password=friend_cfg['password'])
        sftp = paramiko.SFTPClient.from_transport(transport)

        base_remote = friend_cfg.get('remote_dir', '/').rstrip('/')
        top_name = os.path.basename(full_path.rstrip('/'))
        limiter = RateLimiter(RATE_LIMIT_BYTES_PER_SEC)
        bytes_done_total = 0

        for local_file, rel in files:
            remote_path = f'{base_remote}/{top_name}/{rel}'.replace('\\', '/')
            _mkdirs(sftp, os.path.dirname(remote_path))

            last_sent = {'val': 0}

            def progress(sent, _total, _last_sent=last_sent, _base=bytes_done_total):
                delta = sent - _last_sent['val']
                _last_sent['val'] = sent
                limiter.throttle(delta)
                db.execute('UPDATE uploads SET bytes_done=? WHERE id=?', (_base + sent, upload_id))
                db.commit()

            sftp.put(local_file, remote_path, callback=progress)
            bytes_done_total += os.path.getsize(local_file)

        sftp.close()
        transport.close()
        db.execute(
            'UPDATE uploads SET status=?, bytes_done=?, finished_at=? WHERE id=?',
            ('done', bytes_done_total, datetime.utcnow().isoformat(), upload_id)
        )
        db.commit()
        log.info(f'Upload {upload_id} complete: {bytes_done_total/1024/1024:.1f}MB to {friend_cfg["host"]}')
    except Exception as e:
        db.execute(
            'UPDATE uploads SET status=?, error=?, finished_at=? WHERE id=?',
            ('failed', str(e), datetime.utcnow().isoformat(), upload_id)
        )
        db.commit()
        log.error(f'Upload {upload_id} failed: {e}')
    finally:
        db.close()


def _current_email():
    return request.headers.get('Cf-Access-Authenticated-User-Email', '')


_BASE_CSS = '''
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f0f0f; color: #e8e8e8; min-height: 100vh; }
  header { background: #1a1a1a; border-bottom: 1px solid #2a2a2a;
           padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 1.1rem; font-weight: 600; letter-spacing: 0.02em; color: #fff; }
  header .meta { font-size: 0.8rem; color: #666; }
  header nav a { color: #888; text-decoration: none; font-size: 0.85rem; margin-left: 16px; }
  header nav a:hover { color: #fff; }
  main { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
  h2 { font-size: 1.3rem; font-weight: 600; margin-bottom: 24px; color: #fff; }
  a { color: #6ea8fe; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; }
  .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
          padding: 24px 20px; text-align: center; cursor: pointer;
          transition: border-color 0.15s, background 0.15s; text-decoration: none; color: inherit; }
  .card:hover { border-color: #555; background: #222; text-decoration: none; }
  .card .icon { font-size: 2.4rem; margin-bottom: 12px; }
  .card .label { font-size: 0.95rem; font-weight: 500; color: #e8e8e8; text-transform: capitalize; }
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
  .btn-primary:hover { background: #1d4ed8; text-decoration: none; }
  .btn-ghost { background: #2a2a2a; color: #bbb; border: 1px solid #333; }
  .btn-ghost:hover { background: #333; color: #fff; text-decoration: none; }
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
'''

_LIB_ICONS = {'movies': '🎬', 'tv': '📺', 'music': '🎵'}

INDEX_HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media Share</title><style>''' + _BASE_CSS + '''</style></head>
<body>
<header>
  <h1>Media Share</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <nav><a href="/share/usage">Usage</a></nav>
    <span class="meta">{{ email }}</span>
  </div>
</header>
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
</main>
</body></html>
'''

BROWSE_HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ library }} — Media Share</title><style>''' + _BASE_CSS + '''</style></head>
<body>
<header>
  <h1>Media Share</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <nav><a href="/share">Libraries</a><a href="/share/usage">Usage</a></nav>
    <span class="meta">{{ email }}</span>
  </div>
</header>
<main>
  <div class="breadcrumb">
    <a href="/share">home</a> /
    <a href="/share/browse/{{ library }}">{{ library }}</a>
    {% if rel_path %}
      {% for part in rel_path.split("/") if part %}
        / {{ part }}
      {% endfor %}
    {% endif %}
  </div>
  <div class="file-list">
    {% if parent is not none %}
    <div class="file-row">
      <span class="file-icon">⬆</span>
      <span class="file-name"><a href="/share/browse/{{ library }}?path={{ parent }}">.. up</a></span>
    </div>
    {% endif %}
    {% for e in entries %}
    <div class="file-row">
      <span class="file-icon">{% if e.is_dir %}📁{% else %}🎬{% endif %}</span>
      <span class="file-name">
        {% if e.is_dir %}
          <a href="/share/browse/{{ library }}?path={{ e.rel }}">{{ e.name }}</a>
        {% else %}
          {{ e.name }}
        {% endif %}
      </span>
      <div class="actions">
        {% if not e.is_dir %}
        <a class="btn btn-ghost" href="/share/download/{{ library }}?path={{ e.rel }}">Download</a>
        {% endif %}
        <form method="post" action="/share/upload">
          <input type="hidden" name="library" value="{{ library }}">
          <input type="hidden" name="rel_path" value="{{ e.rel }}">
          <button class="btn btn-primary" type="submit">Upload to me</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
</main>
</body></html>
'''

STATUS_HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload Status — Media Share</title>
{% if row.status in ("pending", "running") %}<meta http-equiv="refresh" content="3">{% endif %}
<style>''' + _BASE_CSS + '''</style></head>
<body>
<header>
  <h1>Media Share</h1>
  <nav><a href="/share">Libraries</a><a href="/share/usage">Usage</a></nav>
</header>
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
    {% if row.error %}
    <div class="error-box">{{ row.error }}</div>
    {% endif %}
  </div>
</main>
</body></html>
'''

USAGE_HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Usage — Media Share</title><style>''' + _BASE_CSS + '''</style></head>
<body>
<header>
  <h1>Media Share</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <nav><a href="/share">Libraries</a></nav>
    <span class="meta">{{ email }}</span>
  </div>
</header>
<main>
  <h2>Your Usage</h2>
  <table class="stat-table">
    <tr><th>Period</th><th>Data Sent</th></tr>
    {% for label, total in usage.items() %}
    <tr>
      <td>{{ label }}</td>
      <td class="size">{{ "%.2f"|format(total / 1024 / 1024 / 1024) }} GB</td>
    </tr>
    {% endfor %}
  </table>
</main>
</body></html>
'''


@share_bp.route('/')
def share_index():
    email = _current_email()
    if not email:
        return 'Access denied: no Cloudflare Access identity found', 403
    return render_template_string(INDEX_HTML, libraries=LIBRARIES.keys(), email=email, icons=_LIB_ICONS)


@share_bp.route('/browse/<library>')
def share_browse(library):
    email = _current_email()
    if not email:
        return 'Access denied', 403
    if library not in LIBRARIES:
        return 'Unknown library', 404
    rel_path = request.args.get('path', '')
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.isdir(full):
        return 'Not a directory', 400
    entries = []
    for name in sorted(os.listdir(full)):
        p = os.path.join(full, name)
        entries.append({
            'name': name,
            'is_dir': os.path.isdir(p),
            'rel': os.path.join(rel_path, name) if rel_path else name,
        })
    parent = os.path.dirname(rel_path) if rel_path else None
    return render_template_string(
        BROWSE_HTML, library=library, entries=entries, rel_path=rel_path, parent=parent, email=email
    )


@share_bp.route('/download/<library>')
def share_download(library):
    email = _current_email()
    if not email:
        return 'Access denied', 403
    if library not in LIBRARIES:
        return 'Unknown library', 404
    rel_path = request.args.get('path', '')
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.isfile(full):
        return 'Not a file', 400
    return send_file(full, as_attachment=True, conditional=True)


@share_bp.route('/upload', methods=['POST'])
def share_upload():
    email = _current_email()
    if not email:
        return 'Access denied', 403
    friend_cfg = get_friend_config(email)
    if not friend_cfg:
        return f'No upload destination configured for {email}', 403
    library = request.form.get('library')
    rel_path = request.form.get('rel_path', '')
    if library not in LIBRARIES:
        return 'Unknown library', 400
    try:
        full = safe_join(LIBRARIES[library], rel_path)
    except ValueError:
        return 'Invalid path', 400
    if not os.path.exists(full):
        return 'Not found', 404

    full = safe_join(LIBRARIES[library], rel_path)
    if os.path.isdir(full):
        bytes_total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(full) for f in fs)
    else:
        bytes_total = os.path.getsize(full)

    db = sqlite3.connect(DB_PATH)
    cur = db.execute(
        'INSERT INTO uploads (friend_email, library, rel_path, status, bytes_total, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (email, library, rel_path, 'pending', bytes_total, datetime.utcnow().isoformat())
    )
    upload_id = cur.lastrowid
    db.commit()
    db.close()

    threading.Thread(
        target=perform_upload,
        args=(upload_id, friend_cfg, LIBRARIES[library], rel_path),
        daemon=True
    ).start()
    return redirect(f'/share/status/{upload_id}')


@share_bp.route('/status/<int:upload_id>')
def share_status(upload_id):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
    db.close()
    if not row:
        return 'Not found', 404
    return render_template_string(STATUS_HTML, row=row)


@share_bp.route('/usage')
def share_usage():
    email = _current_email()
    if not email:
        return 'Access denied', 403
    db = sqlite3.connect(DB_PATH)
    usage = {}
    for label, days in USAGE_WINDOWS.items():
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        total = db.execute(
            "SELECT COALESCE(SUM(bytes_done), 0) FROM uploads "
            "WHERE friend_email=? AND status='done' AND created_at >= ?",
            (email, cutoff)
        ).fetchone()[0]
        usage[label] = total
    db.close()
    return render_template_string(USAGE_HTML, email=email, usage=usage)
