"""Report-only weekly audit of files nothing owns. It only reads: nothing is changed on disk or in Deluge.

Compares what is on disk (Downloads plus the library roots) with what Deluge,
Plex and the arrs say they own, and lists what nobody claims:

- downloads_orphan: a file under Downloads that no torrent owns (the rule and
  the grace period of downloads_audit).
- library_untracked: a video under a library root unknown to Plex, the arrs
  and Deluge.
- library_no_video_dir: a library folder with no video anywhere below it
  (leftover subtitles, nfo, artwork).
- torrent_not_in_library: a finished sonarr*/radarr* torrent, seeded long
  enough, none of whose videos Plex or the arrs know, every video of it a
  single link (so deleting it frees the space).
- plex_missing_file: a Plex part whose file is not on disk (listed only).

Pure: callers pass in everything (walk rows, Deluge torrents, Plex parts, arr
files). A source passed as None means it could not be read; the buckets that
need it are then "incomplete" and list nothing, so a file is never called
abandoned because a source failed.
"""
import os
import posixpath
import time
import unicodedata

import downloads_audit

SKIP_DIR_PREFIXES = ('.Trash',)
SKIP_DIR_NAMES = ('@eaDir', '.recycle', 'lost+found')
MAX_ROWS = 500
SEED_DAYS = 21
BUCKETS = ('downloads_orphan', 'library_untracked', 'library_no_video_dir',
           'torrent_not_in_library', 'plex_missing_file')
NEEDS = {
    'downloads_orphan': ('deluge',),
    'library_untracked': ('deluge', 'plex', 'arr'),
    'library_no_video_dir': (),
    'torrent_not_in_library': ('deluge', 'plex', 'arr'),
    'plex_missing_file': ('plex',),
}
TORRENT_LABELS = ('sonarr', 'radarr')


def _norm(p):
    return unicodedata.normalize('NFC', posixpath.normpath(p)) if p else ''


def _is_video(path):
    return posixpath.basename(path).lower().endswith(downloads_audit.VIDEO_EXT)


def _skip_dir(name):
    return name.startswith(SKIP_DIR_PREFIXES) or name in SKIP_DIR_NAMES


def _file_row(path, st):
    return {'path': path, 'size': st.st_size, 'dev': st.st_dev, 'ino': st.st_ino, 'nlink': st.st_nlink,
            'mtime': st.st_mtime, 'is_dir_empty_of_video': False}


def walk(roots):
    """Read-only scan. Returns (rows, errors). File rows describe every regular file; a directory with no video
    anywhere below it (and at least one file) gets one row too: is_dir=True, size = its total, mtime = newest file
    in it, files = [(path, size, dev, ino, nlink)]. Only the topmost such folder is reported, never a root itself.
    Symlinks and trash/recycle/@eaDir/lost+found folders are skipped; a directory that cannot be read is counted
    in `errors` and the scan goes on."""
    rows, errors = [], [0]

    def scan(d):
        has_video, size, newest, files, kids = False, 0, 0.0, [], []
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            errors[0] += 1
            return True, 0, 0.0, [], []   # unreadable: never call it video-free
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if _skip_dir(e.name):
                        continue
                    kids.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    st = os.lstat(e.path)
                    rows.append(_file_row(e.path, st))
                    files.append((e.path, st.st_size, st.st_dev, st.st_ino, st.st_nlink))
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
                    has_video = has_video or _is_video(e.path)
            except OSError:
                errors[0] += 1
        dir_rows = []
        for k in kids:
            k_video, k_size, k_new, k_files, _ = scan(k)
            if k_video:
                has_video = True
            else:
                size += k_size
                newest = max(newest, k_new)
                files.extend(k_files)
                if k_files:
                    dir_rows.append({'path': k, 'size': k_size, 'mtime': k_new, 'is_dir': True,
                                     'is_dir_empty_of_video': True, 'files': k_files})
        if has_video:
            rows.extend(dir_rows)
            return True, size, newest, files, []
        # no video here: the parent decides whether this folder is the topmost one to report
        return False, size, newest, files, []

    for root in roots:
        if not os.path.isdir(root):
            errors[0] += 1
            continue
        scan(root)
    return rows, errors[0]


def _known_sets(*sources):
    paths, sizes = set(), set()
    for src in sources:
        for p, s in src:
            paths.add(_norm(p))
            sizes.add((unicodedata.normalize('NFC', posixpath.basename(p)), s))
    return paths, sizes


def _is_known(path, size, known):
    paths, sizes = known
    return (_norm(path) in paths
            or (unicodedata.normalize('NFC', posixpath.basename(path)), size) in sizes)


def _torrent_files(torrents):
    for t in torrents.values():
        for f in (t or {}).get('files') or []:
            if f.get('path'):
                yield posixpath.join((t or {}).get('save_path') or '', f['path']), f.get('size') or 0


def _under_any(path, roots):
    return any(downloads_audit.under(path, r) for r in roots)


def _hidden(path, root):
    rel = posixpath.relpath(path, root)
    return any(part.startswith('.') for part in rel.split('/'))


def _torrent_row(t, units, now, seed_days):
    save = t.get('save_path') or ''
    firsts = {posixpath.relpath(u[0], save).split('/')[0] for u in units}
    path = posixpath.join(save, firsts.pop()) if len(firsts) == 1 else save
    mtime = max(u[5] for u in units)
    done = t.get('completed_time') or 0
    reason = (f"finished {int((now - done) // 86400)} days ago, label {t.get('label')}, "
              f'no video known to Plex or the arrs, every video a single link')
    return path, sum(u[1] for u in units), mtime, reason


def audit(walk_rows, torrents, plex_parts, arr_files, now, *, downloads_root, library_roots, seed_days=SEED_DAYS,
          grace_s=86400, walk_errors=0, arr_status=None, exists=os.path.exists, clock=time.time):
    """Returns the report dict (see the module docstring). Report only."""
    library_roots = list(library_roots)
    present = {'deluge': torrents is not None, 'plex': plex_parts is not None, 'arr': arr_files is not None}
    sources = {'deluge': 'ok' if present['deluge'] else 'failed',
               'plex': 'ok' if present['plex'] else 'failed',
               'radarr': 'ok' if present['arr'] else 'failed',
               'sonarr': 'ok' if present['arr'] else 'failed'}
    sources.update(arr_status or {})
    torrents = torrents or {}
    plex_parts = plex_parts or []
    arr_files = arr_files or []

    lib_known = _known_sets(((p['path'], p.get('size')) for p in plex_parts),
                            ((p['path'], p.get('size')) for p in arr_files))
    any_known = _known_sets(_torrent_files(torrents), ((p['path'], p.get('size')) for p in plex_parts),
                            ((p['path'], p.get('size')) for p in arr_files))
    owned = {_norm(p) for t in torrents.values() for p in downloads_audit.torrent_paths(t)}

    def old(mtime):
        return now - mtime >= grace_s

    # entries: bucket -> list of (path, size, mtime, reason, units)
    entries = {b: [] for b in BUCKETS}
    file_rows = [r for r in walk_rows if not r.get('is_dir')]
    by_path = {_norm(r['path']): r for r in file_rows}

    for r in file_rows:
        path, mtime = r['path'], r['mtime']
        name = posixpath.basename(path)
        if name.startswith('.') or not old(mtime):
            continue
        if downloads_audit.under(path, downloads_root):
            if _hidden(path, downloads_root) or _norm(path) in owned:
                continue
            entries['downloads_orphan'].append(
                (path, r['size'], mtime, 'no Deluge torrent owns this file',
                 [(path, r['size'], r['dev'], r['ino'], r['nlink'], mtime)]))
        elif _under_any(path, library_roots) and _is_video(path):
            if _hidden(path, next(x for x in library_roots if downloads_audit.under(path, x))):
                continue
            if _is_known(path, r['size'], any_known):
                continue
            entries['library_untracked'].append(
                (path, r['size'], mtime, 'video unknown to Plex, the arrs and Deluge',
                 [(path, r['size'], r['dev'], r['ino'], r['nlink'], mtime)]))

    for r in walk_rows:
        if r.get('is_dir') and _under_any(r['path'], library_roots) and old(r['mtime']):
            units = [(p, s, d, i, n, r['mtime']) for p, s, d, i, n in r['files']]
            entries['library_no_video_dir'].append(
                (r['path'], r['size'], r['mtime'], 'folder holds no video (subtitles, nfo, artwork only)', units))

    cutoff = seed_days * 86400
    for h in sorted(torrents):
        t = torrents[h] or {}
        if not (t.get('label') or '').lower().startswith(TORRENT_LABELS):
            continue
        if (t.get('progress') or 0) < 100:
            continue
        done = t.get('completed_time') or 0
        if done <= 0 or now - done < cutoff:
            continue
        units, videos, ok = [], 0, True
        for p in downloads_audit.torrent_paths(t):
            row = by_path.get(_norm(p))
            if row is None:
                continue   # not on disk (or outside the scan): nothing to free, nothing to check
            if not old(row['mtime']):
                ok = False
                break
            units.append((row['path'], row['size'], row['dev'], row['ino'], row['nlink'], row['mtime']))
            if _is_video(p):
                videos += 1
                if row['nlink'] != 1 or _is_known(p, row['size'], lib_known):
                    ok = False
                    break
        if ok and videos:
            path, size, mtime, reason = _torrent_row(t, units, now, seed_days)
            entries['torrent_not_in_library'].append((path, size, mtime, reason, units))

    scanned = [r for r in library_roots] + [downloads_root]
    for p in plex_parts:
        if _under_any(p['path'], scanned) and not exists(p['path']):
            entries['plex_missing_file'].append((p['path'], 0, 0, 'Plex lists a file that is not on disk', []))

    # incomplete buckets get no rows
    status = {}
    for b in BUCKETS:
        missing = [n for n in NEEDS[b] if not present[n]]
        status[b] = ('incomplete', missing) if missing else ('ok', [])
        if missing:
            entries[b] = []

    # reclaim: a hardlinked inode frees space only if every link of it was found and is flagged; count it once
    flagged = {_norm(u[0]) for b in BUCKETS if b != 'plex_missing_file' for e in entries[b] for u in e[4]}
    links = {}
    for r in file_rows:
        links.setdefault((r['dev'], r['ino']), set()).add(_norm(r['path']))
    seen = set()
    out_buckets = {}
    total_count = total_reclaim = 0
    for b in BUCKETS:
        rows, reclaim_sum, bytes_sum = [], 0, 0
        for path, size, mtime, reason, units in sorted(entries[b], key=lambda e: e[0]):
            rec = 0
            for p, s, dev, ino, nlink, _m in units:
                if nlink == 1:
                    rec += s
                elif (dev, ino) not in seen and len(links.get((dev, ino), ())) == nlink \
                        and links[(dev, ino)] <= flagged:
                    seen.add((dev, ino))
                    rec += s
            rows.append({'path': path, 'size': size, 'reclaim_bytes': rec, 'mtime': mtime, 'reason': reason})
            reclaim_sum += rec
            bytes_sum += size
        rows.sort(key=lambda x: (-x['reclaim_bytes'], -x['size'], x['path']))
        out_buckets[b] = {'status': status[b][0], 'count': len(rows), 'bytes': bytes_sum,
                          'reclaim_bytes': reclaim_sum, 'rows': rows[:MAX_ROWS]}
        if status[b][0] == 'incomplete':
            out_buckets[b]['note'] = 'needs ' + ', '.join(status[b][1]) + ' (source failed)'
        if b != 'plex_missing_file':
            total_count += len(rows)
            total_reclaim += reclaim_sum
    return {'started_at': now, 'finished_at': clock(), 'sources': sources, 'buckets': out_buckets,
            'totals': {'count': total_count, 'reclaim_bytes': total_reclaim}, 'walk_errors': walk_errors}


def summary_line(report):
    """Counts and GB only: no paths or titles."""
    t = report['totals']
    parts = ', '.join(f"{b} {v['count']}" for b, v in report['buckets'].items())
    return f"Abandoned audit: {t['count']} files, {t['reclaim_bytes'] / 1e9:.1f} GB reclaimable ({parts})"
