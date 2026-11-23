"""Keep the glacier disk (disk1) asleep: queue the work that would wake it until it is awake anyway.

Off unless HOLD_DISK1_UPGRADES is on (see arr-webhook.py for the wiring).

Why a hold and not a split: in Sonarr's UpgradeMediaFileService.UpgradeEpisodeFile and Radarr's
UpgradeMovieFile the old file is recycled (RecycleBinProvider.DeleteFile = a move into the recycle
folder, or a permanent delete when none is set) and its database row deleted, in the same call and
BEFORE the new file is moved in. There is no setting or hook that imports the new copy and defers the
delete, so the whole import has to wait.

The hold. The *arr imports a finished download when its download client reports the item Completed
under the *arr's own category (Deluge client: GetTorrentsByLabel(TvCategory/MovieCategory); Completed =
torrent.IsFinished and state != Checking). When a finished download is an upgrade of a file that lives
on disk1, its Deluge label is moved to a hold label the *arr does not watch. The *arr's
StopTrackingMissing then only drops the item from its in-memory download cache; the Grabbed history row
(keyed by downloadId = infohash) stays. On release the label goes back and a RefreshMonitoredDownloads
command is queued; the *arr re-tracks the item by downloadId, finds the Grabbed row and imports it as
the upgrade it is. Nothing is paused, moved or deleted, so seeding carries on while held.

Hold at grab time. The *arr's OnGrab webhook reaches us before the download finishes. If the grabbed
item already has a file (the *arr API gives its path from its own database, no disk access) that is on
disk1, the torrent is relabelled right then, so the ~1 minute completion poll can never import it. The
completion-time check in hold_pass stays as a fallback.

The stopgap copy. Once a held download is finished, the better copy is hardlinked into a separate hold
root (GLACIER_HOLD_ROOT, default `data/Glacier Hold`), outside the *arr root folders, so no rescan can
find and import it and no folder in the library is touched:
`<root>/TV Shows/<show folder>/<season folder>/<name> - glacier-tmp.<ext>` or
`<root>/Movies/<movie folder>/<name> - glacier-tmp.<ext>`. The hold root's two folders are extra folders
of the Plex TV and Movie libraries (plex_hold_folders.py adds them), so Plex merges by title and lists
the file as a second version; a partial scan of the hold folder is requested. A hardlink shares the
download's inode, so it lives on the download's disk, never disk1. On release the link and the empty hold
folders are removed FIRST, the hold folder is rescanned, then the label is restored and the import
released. Crash safety: the state file is written before every step that has an effect (the link path is
journalled before the link is made), holds survive a restart, a link is never kept without a hold entry,
and startup lists and clears leftovers.

"Is the old file on disk1" is answered from an index file, never from the disks (a lookup can spin the
disk up). The nightly mover writes it atomically at glacier-index.txt beside the spin-*.tsv files
(GLACIER_INDEX). Format: UTF-8, one unit per line, sorted and unique, each a path relative to /mnt/diskN
(no leading slash, '/' separators). Units: TV = the show folder (`data/Media/TV Shows/<Show>`); movies =
the movie folder; anything else = its first three path parts. A file is on disk1 when its share path
(container /data/... maps to data/..., setting GLACIER_PATH_MAP) starts with a unit followed by "/". Blank
and '#' lines are ignored. The file's mtime is the freshness signal: missing, empty or older than
GLACIER_INDEX_MAX_AGE_DAYS (default 3) means the hold is inactive.

"Disk1 is up" = the newest disk1 row in the spin logger's spin-*.tsv says up (rows are
`epoch<TAB>disk<TAB>up|down`), or local time is inside the nightly window (GLACIER_WINDOW, 22:00-06:00).

Logs from this module carry counts and ids (infohashes) only.
"""
import glob
import os
import posixpath
import re

HOLD_LABEL = 'glacier-hold'
DEFAULT_WINDOW = '22:00-06:00'
INDEX_MAX_AGE_DAYS = 3
SPIN_DISK = 'disk1'
WATCHED_STATES = ('downloading', 'importpending')   # arr states in which an import is still ahead
LINK_MARK = ' - glacier-tmp'
DEFAULT_HOLD_ROOT = 'data/Glacier Hold'   # share-relative; the container path comes from the path map
SEASON_RE = re.compile(r'^(season \d+|specials)$', re.I)
VIDEO_EXTS = ('.mkv', '.mp4', '.m4v', '.avi', '.ts', '.mov', '.wmv')


# ── disk state ────────────────────────────────────────────────────────────────

def parse_window(spec):
    """'22:00-06:00' -> (start_min, end_min) minutes since midnight. None if unusable."""
    try:
        a, b = str(spec).strip().split('-')
        (ah, am), (bh, bm) = [[int(x) for x in p.strip().split(':')] for p in (a, b)]
        if not (0 <= ah < 24 and 0 <= bh < 24 and 0 <= am < 60 and 0 <= bm < 60):
            return None
        return ah * 60 + am, bh * 60 + bm
    except (ValueError, TypeError):
        return None


def in_window(now_dt, spec=DEFAULT_WINDOW):
    """True when now_dt (a datetime in the wanted zone) is inside the window; wraps midnight.
    An unparseable spec falls back to the default window. start == end is an empty window."""
    w = parse_window(spec) or parse_window(DEFAULT_WINDOW)
    start, end = w
    cur = now_dt.hour * 60 + now_dt.minute
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def newest_disk1_state(spin_dir, disk=SPIN_DISK, max_files=3):
    """(epoch, is_up) of the newest `disk` row in spin-*.tsv under spin_dir, or None when there is none.
    Only the newest few files (by name) are read. Malformed rows are skipped."""
    if not spin_dir:
        return None
    try:
        files = sorted(glob.glob(os.path.join(spin_dir, 'spin-*.tsv')))[-max_files:]
    except OSError:
        return None
    best = None
    for path in files:
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    parts = line.rstrip('\n').split('\t')
                    if len(parts) < 3 or parts[1].strip() != disk:
                        continue
                    try:
                        ts = float(parts[0])
                    except ValueError:
                        continue
                    state = parts[2].strip().lower()
                    if state not in ('up', 'down'):
                        continue
                    if best is None or ts >= best[0]:
                        best = (ts, state == 'up')
        except OSError:
            continue
    return best


def disk1_up(spin_dir, window_spec, now_dt):
    """(up, why) with why one of 'spin', 'window', 'down'."""
    row = newest_disk1_state(spin_dir)
    if row is not None and row[1]:
        return True, 'spin'
    if in_window(now_dt, window_spec):
        return True, 'window'
    return False, 'down'


# ── the index ─────────────────────────────────────────────────────────────────

def _norm(p):
    p = (p or '').replace('\\', '/').strip()
    p = posixpath.normpath('/' + p.lstrip('/')).lstrip('/')
    return '' if p == '.' else p


def load_index(path, now, max_age_days=INDEX_MAX_AGE_DAYS):
    """Frozenset of normalized prefixes, or None when the index is missing, unreadable, empty or stale
    (then the hold is inactive)."""
    if not path:
        return None
    try:
        st = os.stat(path)
        if now - st.st_mtime > max_age_days * 86400:
            return None
        with open(path, encoding='utf-8', errors='replace') as fh:
            prefixes = {_norm(line) for line in fh if line.strip() and not line.lstrip().startswith('#')}
    except OSError:
        return None
    prefixes.discard('')
    return frozenset(prefixes) if prefixes else None


DEFAULT_PATH_MAP = ('/data', 'data')   # arr container path prefix -> share path prefix


def to_share(path, path_map=DEFAULT_PATH_MAP):
    """Container path (/data/...) to the path relative to the share (data/...). Unmapped paths pass through."""
    p = (path or '').replace('\\', '/')
    src = (path_map[0] if path_map else '').rstrip('/')
    if src and (p == src or p.startswith(src + '/')):
        p = path_map[1].rstrip('/') + p[len(src):]
    return _norm(p)


def on_disk1(path, prefixes, path_map=DEFAULT_PATH_MAP):
    """True when the file path starts with a listed unit followed by '/' (whole components; the unit
    itself is a folder, so a path equal to a unit is not a file in it)."""
    p = to_share(path, path_map)
    if not p or not prefixes:
        return False
    parts = p.split('/')
    return any('/'.join(parts[:i]) in prefixes for i in range(1, len(parts)))


# ── the stopgap link ──────────────────────────────────────────────────────────

def from_share(path, path_map=DEFAULT_PATH_MAP):
    """Share-relative path (data/...) to the container path (/data/...)."""
    p = _norm(path)
    dst = (path_map[1] if path_map else '').rstrip('/')
    if dst and (p == dst or p.startswith(dst + '/')):
        return (path_map[0].rstrip('/') + p[len(dst):]) or '/'
    return '/' + p


def link_name(src_path):
    """Marked temporary name for a hardlink of the download file src_path."""
    base = posixpath.basename(src_path)
    stem, ext = posixpath.splitext(base)
    return stem + LINK_MARK + ext


def is_link_name(name):
    return posixpath.splitext(name)[0].endswith(LINK_MARK)


def plan_link(src, old_paths, prefixes, who='Sonarr', hold_root=DEFAULT_HOLD_ROOT, path_map=DEFAULT_PATH_MAP):
    """(dst, reason). dst is the hardlink path (container path, under the hold root) or None with the reason
    it is skipped. One old file only (a season pack has several), a source that is not itself on disk1, a
    video extension. The hold folder is named after the item's folder, so Plex merges it by title."""
    if len(old_paths) != 1:
        return None, 'link_skipped_multi'
    if not src or posixpath.splitext(src)[1].lower() not in VIDEO_EXTS:
        return None, 'link_skipped_nosource'
    if on_disk1(src, prefixes, path_map):
        return None, 'link_skipped_src_on_disk1'
    root = from_share(hold_root, path_map)
    d = posixpath.dirname(old_paths[0])
    name = link_name(src)
    if who == 'Radarr':
        dst = posixpath.join(root, 'Movies', posixpath.basename(d), name)
    elif SEASON_RE.match(posixpath.basename(d)):
        dst = posixpath.join(root, 'TV Shows', posixpath.basename(posixpath.dirname(d)), posixpath.basename(d), name)
    else:
        dst = posixpath.join(root, 'TV Shows', posixpath.basename(d), name)
    if on_disk1(dst, prefixes, path_map):
        return None, 'link_skipped_dst_on_disk1'
    return dst, None


def hold_folders(dst, hold_root=DEFAULT_HOLD_ROOT, path_map=DEFAULT_PATH_MAP):
    """(item_folder, scan_root) for a link path: <root>/<TV Shows|Movies>/<title> and <root>/<TV Shows|Movies>."""
    root = from_share(hold_root, path_map)
    parts = dst[len(root):].strip('/').split('/')
    return posixpath.join(root, parts[0], parts[1]), posixpath.join(root, parts[0])


# ── hold / release ────────────────────────────────────────────────────────────

def watched_hashes_for_hold(torrents, labels, held):
    """Finished torrents on a label the *arr watches that are not held yet (hash -> info)."""
    out = {}
    for h, t in (torrents or {}).items():
        if h in held or (t.get('label') or '').lower() not in labels:
            continue
        if t.get('state') in ('Checking', 'Allocating', 'Error'):
            continue
        done = t.get('is_finished') or (t.get('progress') or 0) >= 100
        if done:
            out[h] = t
    return out


class Counts(dict):
    def bump(self, key, n=1):
        self[key] = self.get(key, 0) + n


def _hold_one(io, state, h, label, who, old_paths, hold_label):
    """Record the way back, then relabel. True when held."""
    state[h] = {'label': label or who.lower(), 'who': who, 'since': io.now(), 'link': None, 'folder': None,
                'scan_root': None, 'old': old_paths[0] if len(old_paths) == 1 else None,
                'multi': len(old_paths) > 1, 'link_done': False}
    io.save(state)                   # record first: a crash after the label change must not lose the way back
    try:
        io.set_label(h, hold_label)
    except Exception:  # noqa: BLE001
        state.pop(h, None)
        io.save(state)
        return False
    return True


def _download_path(info):
    sp, name = info.get('save_path'), info.get('name')
    return posixpath.join(sp, name) if sp and name else None


def _link_one(io, state, h, info, prefixes, c, hold_root, path_map):
    """Give a held, finished download its stopgap link in the hold root and ask Plex to scan it. Tried once."""
    entry = state[h]
    try:
        src = io.pick_source(_download_path(info))
    except Exception:  # noqa: BLE001
        c.bump('lookup_failed')
        return
    entry['link_done'] = True
    old = [entry['old']] if entry.get('old') else []      # empty (a season pack, several files) is skipped as multi
    dst, why = plan_link(src, old, prefixes, entry['who'], hold_root, path_map)
    if why:
        io.save(state)
        c.bump(why)
        return
    entry['link'] = dst              # journalled before the link exists: a crash leaves a record, never an orphan
    entry['folder'], entry['scan_root'] = hold_folders(dst, hold_root, path_map)
    io.save(state)
    try:
        io.link(src, dst)
    except Exception:  # noqa: BLE001
        entry['link'] = None
        io.save(state)
        c.bump('link_failed')
        return
    c.bump('linked')
    try:
        io.plex_scan(entry['folder'])
        c.bump('scanned')
    except Exception:  # noqa: BLE001
        c.bump('scan_failed')


def link_pass(io, state, prefixes, hold_root=DEFAULT_HOLD_ROOT, path_map=DEFAULT_PATH_MAP):
    """Link every held download that has finished and has not been linked yet (holds made at grab time)."""
    c = Counts()
    pending = [h for h, e in state.items() if not e.get('link') and not e.get('link_done')]
    if not pending:
        return c
    torrents = io.torrents()
    for h in pending:
        info = torrents.get(h)
        if info and (info.get('is_finished') or (info.get('progress') or 0) >= 100):
            _link_one(io, state, h, info, prefixes, c, hold_root, path_map)
    return c


def grab_hold(io, state, prefixes, who, h, ids, hold_label=HOLD_LABEL, path_map=DEFAULT_PATH_MAP):
    """Hold a download at grab time. `ids` are the episode ids (Sonarr) or movie ids (Radarr) it is for.
    Returns a status; 'not_in_deluge', 'no_grab_history' and 'lookup_failed' are worth a retry."""
    if h in state:
        return 'already'
    info = io.torrents().get(h)
    if info is None:
        return 'not_in_deluge'
    recs = [({'episodeId': i} if who == 'Sonarr' else {'movieId': i}) for i in ids if i is not None]
    if not recs:
        return 'not_upgrade'
    try:
        paths = io.old_paths(who, recs)
        if not paths:
            return 'not_upgrade'
        if not any(on_disk1(p, prefixes, path_map) for p in paths):
            return 'old_file_elsewhere'
        if not io.has_grab(who, h):
            return 'no_grab_history'
    except Exception:  # noqa: BLE001
        return 'lookup_failed'
    return 'held' if _hold_one(io, state, h, info.get('label'), who, paths, hold_label) else 'hold_failed'


def hold_pass(io, state, prefixes, labels, checked, hold_label=HOLD_LABEL, path_map=DEFAULT_PATH_MAP,
              hold_root=DEFAULT_HOLD_ROOT):
    """Completion-time fallback: hold finished upgrade downloads whose current file is on disk1 and link them.
    `state` is {hash: entry} and is mutated; io.save(state) persists it and is called BEFORE each step with
    an effect. `checked` is a set of hashes already judged, so each download is looked at once. Returns Counts."""
    c = Counts()
    cands = watched_hashes_for_hold(io.torrents(), labels, state)
    cands = {h: t for h, t in cands.items() if h not in checked}
    if not cands:
        return c
    queues = {}
    for who in io.services():
        try:
            queues[who] = {}
            for r in io.queue(who):
                h = (r.get('downloadId') or '').lower()
                if h:
                    queues[who].setdefault(h, []).append(r)
        except Exception:  # noqa: BLE001
            c.bump('queue_unreadable')
            queues.pop(who, None)
    for h, info in cands.items():
        who, recs = next(((w, q[h]) for w, q in queues.items() if h in q), (None, None))
        if not recs:
            if len(queues) == len(io.services()):
                checked.add(h)       # in no queue: not an arr download, or already imported
                c.bump('not_in_queue')
            continue
        if not any(str(r.get('trackedDownloadState') or '').lower() in WATCHED_STATES for r in recs):
            checked.add(h)
            c.bump('past_import_stage')
            continue
        try:
            paths = io.old_paths(who, recs)
            if not paths:
                checked.add(h)
                c.bump('not_upgrade')
                continue
            if not any(on_disk1(p, prefixes, path_map) for p in paths):
                checked.add(h)
                c.bump('old_file_elsewhere')
                continue
            if not io.has_grab(who, h):
                checked.add(h)       # without a Grabbed row a later import could not match: never hold
                c.bump('no_grab_history')
                continue
        except Exception:  # noqa: BLE001
            c.bump('lookup_failed')  # not marked checked: tried again next pass
            continue
        if not _hold_one(io, state, h, info.get('label'), who, paths, hold_label):
            c.bump('hold_failed')
            continue
        checked.add(h)
        c.bump('held')
        _link_one(io, state, h, info, prefixes, c, hold_root, path_map)
    return c


def _drop_link(io, entry):
    """Remove the entry's stopgap link and the hold folders it leaves empty. True when the link is gone."""
    if not entry.get('link'):
        return True
    try:
        io.unlink(entry['link'])
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        return False
    try:
        if entry.get('folder') and entry.get('scan_root'):
            io.rmdir_empty(entry['folder'], entry['scan_root'])
    except Exception:  # noqa: BLE001
        pass                         # an empty folder left behind is harmless
    entry['link'] = None
    return True


def release_pass(io, state, hold_label=HOLD_LABEL, verify_tries=6, verify_poll_s=5.0, sleep=None, keep=None):
    """Release every held download: remove its stopgap link (and empty hold folders), rescan the hold folder,
    THEN put its original label back, then nudge the *arr. A link that cannot be removed keeps the hold
    (retried next pass). A download gone from Deluge or relabelled by someone else is dropped from state, its
    link removed. `keep(h, entry)` true keeps a live hold as it is, link and all, until a later pass. Returns
    Counts: kept, released, gone, relabelled_elsewhere, failed, unlink_failed, rematched, unmatched."""
    c = Counts()
    if not state:
        return c
    torrents = io.torrents()
    released = []
    for h in list(state):
        entry = state[h]
        t = torrents.get(h)
        mine = t is not None and (t.get('label') or '') == hold_label
        if mine and keep is not None and keep(h, entry):
            c.bump('kept')           # someone else still wants it held (watch_hold: a viewer); next pass
            continue
        had_link, scan_root = bool(entry.get('link')), entry.get('scan_root')
        if not _drop_link(io, entry):
            c.bump('unlink_failed')
            continue
        io.save(state)
        if had_link and scan_root:
            try:
                io.plex_scan(scan_root)
                c.bump('scanned_on_release')
            except Exception:  # noqa: BLE001
                c.bump('scan_failed')
        if t is None or not mine:
            state.pop(h)
            io.save(state)
            c.bump('gone' if t is None else 'relabelled_elsewhere')
            continue
        try:
            io.set_label(h, entry.get('label') or '')
        except Exception:  # noqa: BLE001
            c.bump('failed')         # stays in state (without its link), tried again next pass
            continue
        state.pop(h)
        io.save(state)
        released.append((h, entry.get('who')))
        c.bump('released')
    for who in sorted({w for _, w in released if w}):
        try:
            io.refresh(who)
        except Exception:  # noqa: BLE001
            c.bump('refresh_failed')
    if released:
        _verify(io, released, c, verify_tries, verify_poll_s, sleep)
    return c


def reconcile(io, state, hold_label=HOLD_LABEL):
    """Startup: list leftover stopgap links and clear every one that has no live hold behind it. A link
    belongs to a live hold when its torrent still wears the hold label. Returns Counts: entries, links,
    orphan_links_removed, orphan_entries_dropped, kept."""
    c = Counts()
    if not state:
        return c
    torrents = io.torrents()
    c['entries'] = len(state)
    c['links'] = sum(1 for e in state.values() if e.get('link'))
    for h in list(state):
        entry = state[h]
        live = (torrents.get(h) or {}).get('label') == hold_label
        if live:
            c.bump('kept')
            continue
        had_link = bool(entry.get('link'))
        if not _drop_link(io, entry):
            c.bump('unlink_failed')
            continue
        if had_link:
            c.bump('orphan_links_removed')
        state.pop(h)
        c.bump('orphan_entries_dropped')
        io.save(state)
    return c


def _verify(io, released, c, tries, poll_s, sleep):
    """Count how many released downloads the *arr tracks again (queue row with the same downloadId)."""
    pending = {h: w for h, w in released if w}
    for i in range(max(tries, 1)):
        for who in sorted(set(pending.values())):
            try:
                seen = {(r.get('downloadId') or '').lower() for r in io.queue(who)}
            except Exception:  # noqa: BLE001
                continue
            for h in [h for h, w in pending.items() if w == who and h in seen]:
                pending.pop(h)
                c.bump('rematched')
        if not pending:
            break
        if sleep and i + 1 < tries:
            sleep(poll_s)
    c['unmatched'] = len(pending)
