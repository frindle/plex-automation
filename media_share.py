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
from flask import Blueprint, redirect, render_template_string, request

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


INDEX_HTML = '''
<h2>Media share</h2>
<p>Signed in as {{ email }}</p>
<ul>
{% for lib in libraries %}
  <li><a href="/share/browse/{{ lib }}">{{ lib }}</a></li>
{% endfor %}
</ul>
<p><a href="/share/usage">My usage</a></p>
'''

BROWSE_HTML = '''
<h2>{{ library }} / {{ rel_path }}</h2>
{% if parent is not none %}<p><a href="/share/browse/{{ library }}?path={{ parent }}">.. up</a></p>{% endif %}
<ul>
{% for e in entries %}
  <li>
    {% if e.is_dir %}
      <a href="/share/browse/{{ library }}?path={{ e.rel }}">{{ e.name }}/</a>
    {% else %}
      {{ e.name }}
    {% endif %}
    <form style="display:inline" method="post" action="/share/upload">
      <input type="hidden" name="library" value="{{ library }}">
      <input type="hidden" name="rel_path" value="{{ e.rel }}">
      <button type="submit">Upload</button>
    </form>
  </li>
{% endfor %}
</ul>
'''

STATUS_HTML = '''
<h2>Upload {{ row.id }}</h2>
<p>Status: {{ row.status }}</p>
<p>Sent: {{ "%.1f"|format(row.bytes_done / 1024 / 1024) }} MB</p>
{% if row.error %}<p>Error: {{ row.error }}</p>{% endif %}
{% if row.status in ("pending", "running") %}
<meta http-equiv="refresh" content="3">
{% endif %}
'''

USAGE_HTML = '''
<h2>Usage for {{ email }}</h2>
<ul>
{% for label, total in usage.items() %}
  <li>{{ label }}: {{ "%.2f"|format(total / 1024 / 1024 / 1024) }} GB</li>
{% endfor %}
</ul>
'''


@share_bp.route('/')
def share_index():
    email = _current_email()
    if not email:
        return 'Access denied: no Cloudflare Access identity found', 403
    return render_template_string(INDEX_HTML, libraries=LIBRARIES.keys(), email=email)


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
        BROWSE_HTML, library=library, entries=entries, rel_path=rel_path, parent=parent
    )


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

    db = sqlite3.connect(DB_PATH)
    cur = db.execute(
        'INSERT INTO uploads (friend_email, library, rel_path, status, created_at) VALUES (?, ?, ?, ?, ?)',
        (email, library, rel_path, 'pending', datetime.utcnow().isoformat())
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
