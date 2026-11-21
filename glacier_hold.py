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

The stopgap copy. While held, the better copy is hardlinked into the item's folder under a marked
temporary name (`<download file name> - glacier-tmp.<ext>`) so Plex can show it at once (a partial scan
of that folder is requested). The link is made from the download file, which lives on the download's
disk, so a hardlink (same inode, no data copied) can only exist on that disk and never on disk1; a
download whose own path is under an index prefix is never linked. On release the link is removed FIRST,
then the label restored and the import released. Crash safety: the state file is written before every
step that has an effect (the link path is journalled before the link is made), holds survive a restart,
a link is never kept without a hold entry, and startup lists and clears leftovers.

"Is the old file on disk1" is answered from an index file, never from the disks (a lookup can spin the
disk up). Index format (GLACIER_INDEX): UTF-8 text, one path prefix per line, relative to the share, no
leading slash, '/' separators, for example `data/Media/TV Shows/Example Show`. Blank lines and lines
starting with '#' are ignored. A file path is on disk1 when it equals a prefix or lies below one
(whole path components only). A missing, empty or stale (older than GLACIER_INDEX_MAX_AGE_DAYS, default
3) index means the hold is inactive.

"Disk1 is up" = the newest disk1 row in the spin logger's spin-*.tsv says up (rows are
`epoch<TAB>disk<TAB>up|down`), or local time is inside the nightly window (GLACIER_WINDOW, 22:00-06:00).

Logs from this module carry counts and ids (infohashes) only.
"""
import glob
import os
import posixpath

HOLD_LABEL = 'glacier-hold'
DEFAULT_WINDOW = '22:00-06:00'
INDEX_MAX_AGE_DAYS = 3
SPIN_DISK = 'disk1'
WATCHED_STATES = ('downloading', 'importpending')   # arr states in which an import is still ahead
LINK_MARK = ' - glacier-tmp'
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


def on_disk1(path, prefixes):
    """True when the file path (a container path such as /data/Media/...) is at or below an index prefix."""
    p = _norm(path)
    if not p or not prefixes:
        return False
    parts = p.split('/')
    return any('/'.join(parts[:i]) in prefixes for i in range(1, len(parts) + 1))


# ── the stopgap link ──────────────────────────────────────────────────────────

def link_name(src_path):
    """Marked temporary name for a hardlink of the download file src_path."""
    base = posixpath.basename(src_path)
    stem, ext = posixpath.splitext(base)
    return stem + LINK_MARK + ext


def is_link_name(name):
    return posixpath.splitext(name)[0].endswith(LINK_MARK)


def plan_link(src, old_paths, prefixes):
    """(dst, reason). dst is the hardlink path to make, or None with the reason it is skipped. One old
    file only (a season pack has several), a source that is not itself on disk1, a video extension."""
    if len(old_paths) != 1:
        return None, 'link_skipped_multi'
    if not src or posixpath.splitext(src)[1].lower() not in VIDEO_EXTS:
        return None, 'link_skipped_nosource'
    if on_disk1(src, prefixes):
        return None, 'link_skipped_src_on_disk1'
    return posixpath.join(posixpath.dirname(old_paths[0]), link_name(src)), None


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


def hold_pass(io, state, prefixes, labels, checked, hold_label=HOLD_LABEL):
    """Hold finished upgrade downloads whose current file is on disk1, and give each a stopgap link.
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
            if not any(on_disk1(p, prefixes) for p in paths):
                checked.add(h)
                c.bump('old_file_elsewhere')
                continue
            if not io.has_grab(who, h):
                checked.add(h)       # without a Grabbed row a later import could not match: never hold
                c.bump('no_grab_history')
                continue
            src = io.pick_source(recs[0].get('outputPath'))
        except Exception:  # noqa: BLE001
            c.bump('lookup_failed')  # not marked checked: tried again next pass
            continue
        entry = {'label': info.get('label') or '', 'who': who, 'since': io.now(), 'link': None, 'folder': None}
        state[h] = entry
        io.save(state)               # record first: a crash after the label change must not lose the way back
        try:
            io.set_label(h, hold_label)
        except Exception:  # noqa: BLE001
            state.pop(h, None)
            io.save(state)
            c.bump('hold_failed')
            continue
        checked.add(h)
        c.bump('held')
        dst, why = plan_link(src, paths, prefixes)
        if why:
            c.bump(why)
            continue
        entry['link'] = dst          # journalled before the link exists: a crash leaves a record, never an orphan
        entry['folder'] = posixpath.dirname(dst)
        io.save(state)
        try:
            io.link(src, dst)
        except Exception:  # noqa: BLE001
            entry['link'] = None
            io.save(state)
            c.bump('link_failed')
            continue
        c.bump('linked')
        try:
            io.plex_scan(entry['folder'])
            c.bump('scanned')
        except Exception:  # noqa: BLE001
            c.bump('scan_failed')
    return c


def _drop_link(io, entry):
    """Remove the entry's stopgap link. True when it is gone (or never existed)."""
    if not entry.get('link'):
        return True
    try:
        io.unlink(entry['link'])
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        return False
    entry['link'] = None
    return True


def release_pass(io, state, hold_label=HOLD_LABEL, verify_tries=6, verify_poll_s=5.0, sleep=None):
    """Release every held download: remove its stopgap link, THEN put its original label back, then nudge
    the *arr. A link that cannot be removed keeps the hold (retried next pass). A download gone from Deluge
    or relabelled by someone else is dropped from state, its link removed. Returns Counts: released, gone,
    relabelled_elsewhere, failed, unlink_failed, rematched, unmatched."""
    c = Counts()
    if not state:
        return c
    torrents = io.torrents()
    released = []
    for h in list(state):
        entry = state[h]
        t = torrents.get(h)
        mine = t is not None and (t.get('label') or '') == hold_label
        if not _drop_link(io, entry):
            c.bump('unlink_failed')
            continue
        io.save(state)
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
