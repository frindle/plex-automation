"""
Friend-facing media portal + admin panel.

Friends authenticate via Cloudflare Access (Cf-Access-Authenticated-User-Email).
The admin (ADMIN_EMAIL env var) manages friend accounts at /share/admin without
any container restart — config is stored in SQLite, not in env vars.

Security: relies on Cloudflare Access being the only ingress to this port.
Never expose this container directly to the internet.
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
ALL_LIBRARIES = list(LIBRARIES.keys())

DB_PATH = os.environ.get('SHARE_DB_PATH', '/data/share_uploads.db')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')
DEFAULT_RATE_MBIT = float(os.environ.get('UPLOAD_RATE_LIMIT_MBIT', '5'))

USAGE_WINDOWS = {
    '7 days': 7,
    '30 days': 30,
    '60 days': 60,
    '90 days': 90,
    '6 months': 182,
    '1 year': 365,
}


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            sftp_host TEXT NOT NULL DEFAULT '',
            sftp_port INTEGER NOT NULL DEFAULT 22,
            sftp_user TEXT NOT NULL DEFAULT '',
            sftp_password TEXT NOT NULL DEFAULT '',
            sftp_remote_dir TEXT NOT NULL DEFAULT '/',
            libraries TEXT NOT NULL DEFAULT '["movies","tv","music"]',
            rate_limit_mbit REAL NOT NULL DEFAULT 5.0,
            created_at TEXT NOT NULL
        )
    ''')
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
    return {
        'host': row['sftp_host'],
        'port': row['sftp_port'],
        'user': row['sftp_user'],
        'password': row['sftp_password'],
        'remote_dir': row['sftp_remote_dir'],
        'rate_limit_mbit': row['rate_limit_mbit'],
    }


def get_friend_libraries(email):
    """Return the list of library keys this friend is allowed to access."""
    row = _get_friend(email)
    if not row:
        return []
    libs = json.loads(row['libraries'])
    return [k for k in libs if k in LIBRARIES]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_email():
    return request.headers.get('Cf-Access-Authenticated-User-Email', '')


def _is_admin(email):
    return bool(ADMIN_EMAIL) and email == ADMIN_EMAIL


def safe_join(root, rel_path):
    rel_path = rel_path or ''
    full = os.path.normpath(os.path.join(root, rel_path))
    root_normalized = os.path.normpath(root)
    if full != root_normalized and not full.startswith(root_normalized + os.sep):
        raise ValueError('Path traversal attempt')
    return full


# ── Upload ────────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate_bytes_per_sec):
        self.rate = rate_bytes_per_sec
        self.start_time = time.monotonic()
        self.bytes_sent = 0

    def throttle(self, delta_bytes):
        if self.rate <= 0:
            return
        self.bytes_sent += delta_bytes
        elapsed = time.monotonic() - self.start_time
        sleep_for = (self.bytes_sent / self.rate) - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)


def _mkdirs(sftp, remote_dir):
    path = ''
    for part in remote_dir.strip('/').split('/'):
        if not part:
            continue
        path += '/' + part
        try:
            sftp.mkdir(path)
        except IOError:
            pass


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

        rate = friend_cfg.get('rate_limit_mbit', DEFAULT_RATE_MBIT) * 1_000_000 / 8
        limiter = RateLimiter(rate)
        transport = paramiko.Transport((friend_cfg['host'], int(friend_cfg.get('port', 22))))
        transport.connect(username=friend_cfg['user'], password=friend_cfg['password'])
        sftp = paramiko.SFTPClient.from_transport(transport)

        base_remote = friend_cfg.get('remote_dir', '/').rstrip('/')
        top_name = os.path.basename(full_path.rstrip('/'))
        bytes_done_total = 0

        for local_file, rel in files:
            remote_path = f'{base_remote}/{top_name}/{rel}'.replace('\\', '/')
            _mkdirs(sftp, os.path.dirname(remote_path))
            last_sent = {'val': 0}

            def progress(sent, _total, _last=last_sent, _base=bytes_done_total):
                delta = sent - _last['val']
                _last['val'] = sent
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


# ── Templates ─────────────────────────────────────────────────────────────────

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
  .form-card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 28px;
               max-width: 560px; }
  .field { margin-bottom: 18px; }
  .field label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 6px;
                 text-transform: uppercase; letter-spacing: 0.04em; }
  .field input[type=text], .field input[type=password], .field input[type=email],
  .field input[type=number] {
    width: 100%; background: #111; border: 1px solid #333; border-radius: 6px;
    padding: 8px 12px; color: #e8e8e8; font-size: 0.9rem; outline: none; }
  .field input:focus { border-color: #2563eb; }
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
'''

_LIB_ICONS = {'movies': '🎬', 'tv': '📺', 'music': '🎵'}

_NAV = '''
<header>
  <h1>Media Share</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <nav>
      <a href="/share">Libraries</a>
      <a href="/share/usage">Usage</a>
      {% if is_admin %}<a href="/share/admin">Admin</a>{% endif %}
    </nav>
    <span class="meta">{{ email }}</span>
  </div>
</header>
'''

INDEX_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>Media Share</title><style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
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

BROWSE_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>{{ library }} — Media Share</title><style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
<main>
  <div class="breadcrumb">
    <a href="/share">home</a> / <a href="/share/browse/{{ library }}">{{ library }}</a>
    {% if rel_path %}{% for part in rel_path.split("/") if part %} / {{ part }}{% endfor %}{% endif %}
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
        {% if e.is_dir %}<a href="/share/browse/{{ library }}?path={{ e.rel }}">{{ e.name }}</a>
        {% else %}{{ e.name }}{% endif %}
      </span>
      <div class="actions">
        {% if not e.is_dir %}
        <a class="btn btn-ghost" href="/share/download/{{ library }}?path={{ e.rel }}">Download</a>
        {% endif %}
        {% if has_sftp %}
        <form method="post" action="/share/upload">
          <input type="hidden" name="library" value="{{ library }}">
          <input type="hidden" name="rel_path" value="{{ e.rel }}">
          <button class="btn btn-primary" type="submit">Upload to me</button>
        </form>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</main></body></html>'''

STATUS_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>Upload Status — Media Share</title>' \
    '{% if row.status in ("pending", "running") %}<meta http-equiv="refresh" content="3">{% endif %}' \
    '<style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
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
</main></body></html>'''

USAGE_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>Usage — Media Share</title><style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
<main>
  <h2>Your Usage</h2>
  <table class="stat-table">
    <tr><th>Period</th><th>Data Sent</th></tr>
    {% for label, total in usage.items() %}
    <tr><td>{{ label }}</td><td class="size">{{ "%.2f"|format(total / 1024 / 1024 / 1024) }} GB</td></tr>
    {% endfor %}
  </table>
</main></body></html>'''

ADMIN_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>Admin — Media Share</title><style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
<main>
  <h2>Friends</h2>
  {% if friends %}
  <table class="admin-table">
    <tr><th>Email</th><th>SFTP host</th><th>Libraries</th><th>Rate</th><th></th></tr>
    {% for f in friends %}
    <tr>
      <td>{{ f.email }}</td>
      <td>{{ f.sftp_host or "—" }}</td>
      <td>{% for lib in f.libraries_list %}<span class="badge">{{ lib }}</span>{% endfor %}</td>
      <td>{{ f.rate_limit_mbit }} Mbit/s</td>
      <td>
        <div class="actions">
          <a class="btn btn-ghost" href="/share/admin/friend/{{ f.id }}/edit">Edit</a>
          <form method="post" action="/share/admin/friend/{{ f.id }}/delete"
                onsubmit="return confirm('Remove {{ f.email }}?')">
            <button class="btn btn-danger" type="submit">Remove</button>
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
        <div class="field"><label>SFTP host</label>
          <input type="text" name="sftp_host" placeholder="1.2.3.4"></div>
        <div class="field"><label>SFTP port</label>
          <input type="number" name="sftp_port" value="22"></div>
        <div class="field"><label>SFTP username</label>
          <input type="text" name="sftp_user"></div>
        <div class="field"><label>SFTP password</label>
          <input type="password" name="sftp_password"></div>
        <div class="field"><label>Remote directory</label>
          <input type="text" name="sftp_remote_dir" value="/"></div>
        <div class="field"><label>Libraries they can see</label>
          <div class="checkbox-group">
            <label><input type="checkbox" name="libraries" value="movies" checked> Movies</label>
            <label><input type="checkbox" name="libraries" value="tv" checked> TV Shows</label>
            <label><input type="checkbox" name="libraries" value="music" checked> Music</label>
          </div>
        </div>
        <div class="field"><label>Upload rate limit (Mbit/s)</label>
          <input type="number" name="rate_limit_mbit" value="{{ default_rate }}" step="0.5" min="0"></div>
        <button class="btn btn-primary" type="submit">Add friend</button>
      </form>
    </div>
  </div>
</main></body></html>'''

ADMIN_EDIT_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
    '<meta name="viewport" content="width=device-width,initial-scale=1">' \
    '<title>Edit {{ f.email }} — Admin</title><style>' + _BASE_CSS + '</style></head><body>' + _NAV + '''
<main>
  <div class="breadcrumb"><a href="/share/admin">Admin</a> / Edit friend</div>
  <h2>{{ f.email }}</h2>
  <div class="form-card">
    <form method="post" action="/share/admin/friend/{{ f.id }}/edit">
      <div class="field"><label>SFTP host</label>
        <input type="text" name="sftp_host" value="{{ f.sftp_host }}"></div>
      <div class="field"><label>SFTP port</label>
        <input type="number" name="sftp_port" value="{{ f.sftp_port }}"></div>
      <div class="field"><label>SFTP username</label>
        <input type="text" name="sftp_user" value="{{ f.sftp_user }}"></div>
      <div class="field"><label>SFTP password</label>
        <input type="password" name="sftp_password" placeholder="Leave blank to keep current"></div>
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
      <div class="field"><label>Upload rate limit (Mbit/s)</label>
        <input type="number" name="rate_limit_mbit" value="{{ f.rate_limit_mbit }}" step="0.5" min="0"></div>
      <button class="btn btn-primary" type="submit">Save changes</button>
    </form>
  </div>
</main></body></html>'''


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


def _allowed_libraries(email):
    if _is_admin(email):
        return ALL_LIBRARIES
    return get_friend_libraries(email)


# ── Routes ────────────────────────────────────────────────────────────────────

@share_bp.route('/')
def share_index():
    email, err = _require_auth()
    if err:
        return err
    libs = _allowed_libraries(email)
    return _render(INDEX_HTML, email, libraries=libs)


@share_bp.route('/browse/<library>')
def share_browse(library):
    email, err = _require_auth()
    if err:
        return err
    if library not in _allowed_libraries(email):
        return 'Not found', 404
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
        entries.append({'name': name, 'is_dir': os.path.isdir(p),
                        'rel': os.path.join(rel_path, name) if rel_path else name})
    parent = os.path.dirname(rel_path) if rel_path else None
    has_sftp = bool(get_friend_config(email)) if not _is_admin(email) else False
    return _render(BROWSE_HTML, email, library=library, entries=entries,
                   rel_path=rel_path, parent=parent, has_sftp=has_sftp)


@share_bp.route('/download/<library>')
def share_download(library):
    email, err = _require_auth()
    if err:
        return err
    if library not in _allowed_libraries(email):
        return 'Not found', 404
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
    email, err = _require_auth()
    if err:
        return err
    friend_cfg = get_friend_config(email)
    if not friend_cfg:
        return 'No upload destination configured for your account', 403
    library = request.form.get('library')
    rel_path = request.form.get('rel_path', '')
    if library not in _allowed_libraries(email):
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
        (email, library, rel_path, 'pending', bytes_total, datetime.utcnow().isoformat())
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
    usage = {}
    for label, days in USAGE_WINDOWS.items():
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        total = db.execute(
            "SELECT COALESCE(SUM(bytes_done),0) FROM uploads "
            "WHERE friend_email=? AND status='done' AND created_at>=?",
            (email, cutoff)
        ).fetchone()[0]
        usage[label] = total
    db.close()
    return _render(USAGE_HTML, email, usage=usage)


# ── Admin routes ──────────────────────────────────────────────────────────────

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
    db.close()
    friends = []
    for r in rows:
        f = dict(r)
        f['libraries_list'] = json.loads(r['libraries'])
        friends.append(f)
    return friends


@share_bp.route('/admin')
def share_admin():
    email, err = _require_admin()
    if err:
        return err
    return _render(ADMIN_HTML, email, friends=_friends_list(),
                   all_libraries=ALL_LIBRARIES, default_rate=DEFAULT_RATE_MBIT)


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
            'INSERT INTO friends (email, sftp_host, sftp_port, sftp_user, sftp_password, '
            'sftp_remote_dir, libraries, rate_limit_mbit, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (f_email, request.form.get('sftp_host', ''),
             int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''), request.form.get('sftp_password', ''),
             request.form.get('sftp_remote_dir', '/'), libraries,
             float(request.form.get('rate_limit_mbit') or DEFAULT_RATE_MBIT),
             datetime.utcnow().isoformat())
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
        return _render(ADMIN_EDIT_HTML, email, f=f, all_libraries=ALL_LIBRARIES)

    new_password = request.form.get('sftp_password', '').strip()
    libraries = json.dumps(request.form.getlist('libraries') or ALL_LIBRARIES)
    db = sqlite3.connect(DB_PATH)
    if new_password:
        db.execute(
            'UPDATE friends SET sftp_host=?, sftp_port=?, sftp_user=?, sftp_password=?, '
            'sftp_remote_dir=?, libraries=?, rate_limit_mbit=? WHERE id=?',
            (request.form.get('sftp_host', ''), int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''), new_password,
             request.form.get('sftp_remote_dir', '/'), libraries,
             float(request.form.get('rate_limit_mbit') or DEFAULT_RATE_MBIT), friend_id)
        )
    else:
        db.execute(
            'UPDATE friends SET sftp_host=?, sftp_port=?, sftp_user=?, '
            'sftp_remote_dir=?, libraries=?, rate_limit_mbit=? WHERE id=?',
            (request.form.get('sftp_host', ''), int(request.form.get('sftp_port') or 22),
             request.form.get('sftp_user', ''),
             request.form.get('sftp_remote_dir', '/'), libraries,
             float(request.form.get('rate_limit_mbit') or DEFAULT_RATE_MBIT), friend_id)
        )
    db.commit()
    db.close()
    return redirect('/share/admin')


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
