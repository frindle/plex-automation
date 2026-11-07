"""Report-only audit of /data/Downloads. Never deletes, moves or relabels.

Three findings, at most ONE Pushover summary per run, and only for findings
not already reported:

- orphan: a file under Downloads that no Deluge torrent owns. Left behind by
  a removal that kept data, or by a manual move.
- single-link: a video file of a finished sonarr*/radarr*/library-seed torrent
  in Downloads that has only one link. Either it was never imported, or the
  import made a full copy instead of a hardlink (on Unraid, a download and a
  library folder on different disks can't be hardlinked).
- unlabeled: a torrent with no label. No cleanup rule ever picks it up.

Pure: callers pass torrents (with 'files'), the local Downloads root, a
Deluge->local path mapper and the stat/walk functions; nothing here talks to
Deluge or the arrs.
"""
import json
import os
import posixpath
import unicodedata

DOWNLOADS_ROOT = '/data/Downloads'
FIELDS = ['name', 'label', 'save_path', 'progress', 'completed_time', 'time_added', 'files']
VIDEO_EXT = ('.mkv', '.mp4', '.m4v', '.avi', '.ts', '.m2ts', '.wmv', '.mov')
LINK_LABEL_PREFIXES = ('sonarr', 'radarr', 'library-seed')
GRACE_SECONDS = 86400
SUMMARY_NAMES = 8


def _norm(p):
    return unicodedata.normalize('NFC', posixpath.normpath(p)) if p else ''


def under(path, root):
    if not path:
        return False
    p = posixpath.normpath(path).lower()
    r = posixpath.normpath(root).lower()
    return p == r or p.startswith(r + '/')


def torrent_paths(t):
    save_path = (t or {}).get('save_path') or ''
    return [posixpath.join(save_path, f.get('path') or '') for f in ((t or {}).get('files') or [])
            if f.get('path')]


def _group(local_path, local_root):
    rel = posixpath.relpath(local_path, local_root).split('/')
    return '/'.join(rel[:2]) if len(rel) > 2 else '/'.join(rel)


def find_orphans(torrents, local_root, to_local, now, walk=os.walk, lstat=os.lstat):
    owned = {_norm(to_local(p)) for t in torrents.values() for p in torrent_paths(t)}
    out = []
    for dirpath, dirnames, filenames in walk(local_root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for name in sorted(filenames):
            if name.startswith('.'):
                continue  # .DS_Store, ._AppleDouble, libtorrent .parts
            p = posixpath.join(dirpath, name)
            if _norm(p) in owned:
                continue
            try:
                st = lstat(p)
            except OSError:
                continue
            if now - st.st_mtime < GRACE_SECONDS:
                continue  # an import or move may still be in flight
            out.append({'path': p, 'size': st.st_size, 'group': _group(p, local_root)})
    return out


def _finished_long_enough(t, now):
    if (t.get('progress') or 0) < 100:
        return False
    done = t.get('completed_time') or t.get('time_added') or 0
    return done > 0 and now - done >= GRACE_SECONDS


def find_single_link(torrents, to_local, now, stat=os.stat):
    out = []
    for h in sorted(torrents):
        t = torrents[h] or {}
        label = (t.get('label') or '').lower()
        if not label.startswith(LINK_LABEL_PREFIXES):
            continue
        if not under(t.get('save_path'), DOWNLOADS_ROOT) or not _finished_long_enough(t, now):
            continue
        files, size = 0, 0
        for p in torrent_paths(t):
            base = posixpath.basename(p).lower()
            if not base.endswith(VIDEO_EXT) or 'sample' in base:
                continue
            try:
                st = stat(to_local(p))
            except OSError:
                continue  # missing data is the error audit's job
            if st.st_nlink == 1:
                files += 1
                size += st.st_size
        if files:
            out.append({'hash': h, 'name': t.get('name', ''), 'label': t.get('label', ''),
                        'files': files, 'size': size})
    return out


def find_unlabeled(torrents):
    return [{'hash': h, 'name': (torrents[h] or {}).get('name', ''),
             'save_path': (torrents[h] or {}).get('save_path', '')}
            for h in sorted(torrents) if not ((torrents[h] or {}).get('label') or '').strip()]


def _gb(n):
    return f'{n / 1e9:.1f} GB'


def summarize(orphans, single, unlabeled, limit=SUMMARY_NAMES):
    groups = {}
    for o in orphans:
        groups[o['group']] = groups.get(o['group'], 0) + o['size']
    lines = [f'orphan files {len(orphans)} ({_gb(sum(o["size"] for o in orphans))}), '
             f'single-link {len(single)} torrents ({_gb(sum(s["size"] for s in single))}), '
             f'unlabeled {len(unlabeled)}']
    items = [f'- orphan: {g} ({_gb(s)})' for g, s in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))]
    items += [f'- single-link: {s["name"]} [{s["label"]}] ({_gb(s["size"])})' for s in single]
    items += [f'- unlabeled: {u["name"]}' for u in unlabeled]
    lines += items[:limit]
    if len(items) > limit:
        lines.append(f'... and {len(items) - limit} more')
    return '\n'.join(lines)


def audit(torrents, local_root, to_local, notify, reported, now, dry_run=False,
          walk=os.walk, lstat=os.lstat, stat=os.stat):
    """`reported` is a set of finding keys, updated in place: keys no longer
    found are dropped (so a finding that comes back is announced again), new
    ones are added once announced."""
    torrents = torrents or {}
    orphans = find_orphans(torrents, local_root, to_local, now, walk=walk, lstat=lstat)
    single = find_single_link(torrents, to_local, now, stat=stat)
    unlabeled = find_unlabeled(torrents)
    keyed = ([('o:' + o['path'], 'o', o) for o in orphans] + [('s:' + s['hash'], 's', s) for s in single]
             + [('u:' + u['hash'], 'u', u) for u in unlabeled])
    result = {
        'dry_run': dry_run,
        'orphans': orphans, 'single_link': single, 'unlabeled': unlabeled,
        'counts': {'orphan_files': len(orphans), 'orphan_bytes': sum(o['size'] for o in orphans),
                   'single_link_torrents': len(single), 'single_link_bytes': sum(s['size'] for s in single),
                   'unlabeled': len(unlabeled)},
        'notified': [],
    }
    if dry_run:
        return result
    current = {k for k, _, _ in keyed}
    new = [(k, kind, e) for k, kind, e in keyed if k not in reported]
    reported.intersection_update(current)
    if new:
        notify(f'Downloads audit: {len(new)} new finding(s)',
               summarize([e for _, kind, e in new if kind == 'o'], [e for _, kind, e in new if kind == 's'],
                         [e for _, kind, e in new if kind == 'u']))
        reported.update(k for k, _, _ in new)
        result['notified'] = [k for k, _, _ in new]
    return result


def load_reported(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return {k for k in data if isinstance(k, str)} if isinstance(data, list) else set()
    except Exception:
        return set()


def save_reported(path, reported):
    """Atomic write; never raises."""
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(sorted(reported), f)
        os.replace(tmp, path)
    except Exception:
        pass
