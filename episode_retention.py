"""Episode torrent retention: keep seeding season packs, drop single-episode
Sonarr or exactly `library-seed` torrents once they have seeded SEED_DAYS.
Single-episode torrents labeled `library-seed` under /data/Media keep their
files (entry removed only). Movies are never touched; anything ambiguous
counts as a season pack.

Data is deleted only when every file is outside /data/Media AND the library
already holds the episode (library_has_file); otherwise the torrent entry is
removed and the files stay. Paced: EPISODE_RETENTION_PER_RUN (default 5, hard
max 25) per run, one at a time, and nothing at all while any torrent is in
Error. Dry-run unless EPISODE_RETENTION_APPLY=1.
"""
import os
import posixpath
import re

SEED_DAYS = 21
DEFAULT_PER_RUN = 5
HARD_MAX_PER_RUN = 25
MEDIA_ROOT = '/data/Media'
VIDEO_EXT = ('.mkv', '.mp4', '.avi', '.m4v', '.ts', '.wmv')
EPISODE_RE = re.compile(r'(?i)\bS\d{1,2}E\d{1,3}\b')
FIELDS = ['name', 'label', 'state', 'progress', 'seeding_time', 'save_path']  # "files" is fetched per candidate, see episode_retention()


def classify(t):
    label = (t.get('label') or '').lower()
    if label.startswith('radarr'):
        return 'movie'
    tracked = label.startswith('sonarr') or label == 'library-seed'
    if not tracked:
        return 'other'
    videos = [f.get('path') or '' for f in (t.get('files') or [])
              if (f.get('path') or '').lower().endswith(VIDEO_EXT)]
    if len(videos) == 1 and EPISODE_RE.search(t.get('name') or '') and EPISODE_RE.search(videos[0]):
        return 'episode'
    return 'season-pack'


def under_media(path):
    p = posixpath.normpath(path).lower()
    root = MEDIA_ROOT.lower()
    return p == root or p.startswith(root + '/')


def file_paths(t):
    save_path = t.get('save_path') or ''
    return [posixpath.join(save_path, f.get('path') or '') for f in (t.get('files') or [])]


def remove_data_for(t, library_has_file):
    paths = file_paths(t)
    if not paths or any(under_media(p) for p in paths):
        return False
    try:
        return library_has_file(t) is True
    except Exception:
        return False


def per_run_limit(env):
    try:
        n = int(env.get('EPISODE_RETENTION_PER_RUN', DEFAULT_PER_RUN))
    except ValueError:
        n = DEFAULT_PER_RUN
    return max(0, min(n, HARD_MAX_PER_RUN))


def classify_light(t):
    """classify() without the (heavy) "files" field: a Sonarr or library-seed
    torrent whose name has an episode marker is only a *candidate* 'episode'."""
    label = (t.get('label') or '').lower()
    if label.startswith('radarr'):
        return 'movie'
    tracked = label.startswith('sonarr') or label == 'library-seed'
    if not tracked:
        return 'other'
    return 'episode' if EPISODE_RE.search(t.get('name') or '') else 'season-pack'


def episode_retention(torrents, remove, library_has_file, env=None, fetch_files=None):
    """fetch_files(hash) -> files list, used when torrents were pulled without
    "files": it is called only for candidates that passed the age/seed checks."""
    env = os.environ if env is None else env
    apply = env.get('EPISODE_RETENTION_APPLY', '0') == '1'
    limit = per_run_limit(env)
    result = {'dry_run': not apply, 'limit': limit, 'paused': False, 'capped': False,
              'removed': [], 'planned': [], 'errors': [],
              'kept': {'season-pack': 0, 'movie': 0, 'other': 0, 'incomplete': 0, 'too-young': 0}}
    if any((t or {}).get('state') == 'Error' for t in torrents.values()):
        result['paused'] = True
        return result
    for h in sorted(torrents):
        t = torrents[h] or {}
        lazy = fetch_files is not None and 'files' not in t
        kind = classify_light(t) if lazy else classify(t)
        if kind != 'episode':
            result['kept'][kind] += 1
            continue
        if (t.get('progress') or 0) < 100:
            result['kept']['incomplete'] += 1
            continue
        if not t.get('seeding_time') or t['seeding_time'] < SEED_DAYS * 86400:
            result['kept']['too-young'] += 1
            continue
        if lazy:
            try:
                t = dict(t, files=fetch_files(h) or [])
            except Exception:
                t = dict(t, files=[])  # unknown -> ambiguous -> kept as a season pack
            if classify(t) != 'episode':
                result['kept']['season-pack'] += 1
                continue
        if len(result['removed']) + len(result['planned']) >= limit:
            result['capped'] = True
            break
        entry = {'hash': h, 'name': t.get('name', ''), 'remove_data': remove_data_for(t, library_has_file)}
        if not apply:
            result['planned'].append(entry)
            continue
        try:
            remove(h, entry['remove_data'])
        except Exception as e:
            result['errors'].append({'hash': h, 'error': str(e)})
            break
        result['removed'].append(entry)
    return result
