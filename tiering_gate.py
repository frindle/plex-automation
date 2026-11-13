"""Torrent gate for the Unraid glacier mover (tiering/glacier-mover.sh).

Given file paths the mover wants to push to the glacier disk (written in
Deluge's namespace, e.g. /data/Media/TV Shows/Show/S01/ep.mkv), decide per
path (no side effects):
  free   no Deluge torrent owns the file
  pause  every owning torrent is complete (progress 100) with a known, healthy
         state: the mover pauses them around the move and resumes them after.
         The /mnt/user path is unchanged by a disk-to-disk move, so a resumed
         torrent keeps seeding with no recheck. Nothing is ever removed.
  wait   an owning torrent is incomplete, erroring, checking/moving, or its
         state is unknown: leave the file, try again another night.
         With keep_seeding (the fill phase), a complete owner that is still
         seeding or queued also waits: seeding data stays off the glacier disk
         so it can spin down. Only owners that are already paused may move.

active_roots() lists the content roots of torrents that are not paused, for
the evict phase (it moves those roots off the glacier disk).

Pure logic here; arr-webhook.py supplies the Deluge data and does the pausing.
"""
import posixpath

FREE, PAUSE, WAIT = 'free', 'pause', 'wait'
FIELDS = ['name', 'save_path', 'progress', 'state']
OK_STATES = {'seeding', 'paused', 'stopped', 'queued'}
PAUSED_STATES = {'paused', 'stopped'}


def content_roots(torrents):
    """{content root path: [hash, ...]}. A torrent's data lives at save_path/name
    (a folder for multi-file torrents, the file itself for single-file ones)."""
    roots = {}
    for h, t in (torrents or {}).items():
        t = t or {}
        sp, name = t.get('save_path'), t.get('name')
        if not isinstance(sp, str) or not sp or not isinstance(name, str) or not name:
            continue
        roots.setdefault(posixpath.normpath(posixpath.join(sp, name)), []).append(h)
    return roots


def owners(path, roots):
    """Hashes of every torrent whose content root is `path` or one of its parents."""
    p = posixpath.normpath(path)
    found = []
    while True:
        found += roots.get(p, [])
        if p in ('/', '', '.'):
            return found
        p = posixpath.dirname(p) or '/'


def torrent_ok(t):
    """(ok, reason). ok only when complete and in a known, healthy state."""
    t = t or {}
    prog = t.get('progress')
    if not isinstance(prog, (int, float)) or prog < 100:
        return False, 'torrent not complete'
    st = t.get('state')
    if not isinstance(st, str) or st.lower() not in OK_STATES:
        return False, f'torrent state {st if isinstance(st, str) and st else "unknown"}'
    return True, 'complete'


def is_paused(t):
    """True when the torrent is already paused/stopped (by someone else)."""
    st = (t or {}).get('state')
    return isinstance(st, str) and st.lower() in PAUSED_STATES


def classify_paths(paths, torrents, keep_seeding=False):
    """-> [{'path', 'status', 'reason', 'hashes'}] in input order. keep_seeding:
    a complete owner that is not paused (seeding, queued) makes the path wait."""
    roots = content_roots(torrents)
    out = []
    for path in paths:
        hs = owners(path, roots)
        if not hs:
            out.append({'path': path, 'status': FREE, 'reason': 'no torrent', 'hashes': []})
            continue
        states = [(h,) + torrent_ok(torrents[h]) for h in hs]
        blockers = [f'{h[:8]} {why}' for h, ok, why in states if not ok]
        if not blockers and keep_seeding:
            blockers = [f'{h[:8]} seeding (kept off glacier)' for h in hs if not is_paused(torrents[h])]
        if blockers:
            out.append({'path': path, 'status': WAIT, 'reason': '; '.join(blockers), 'hashes': hs})
        else:
            out.append({'path': path, 'status': PAUSE, 'reason': 'complete, pause around move', 'hashes': hs})
    return out


def active_roots(torrents):
    """-> sorted [(state, root)] for every torrent that is not paused/stopped or
    erroring (seeding, queued, downloading, checking, unknown state...). A root
    owned by several torrents is listed once per torrent."""
    out = []
    for h, t in (torrents or {}).items():
        t = t or {}
        sp, name, st = t.get('save_path'), t.get('name'), t.get('state')
        if not isinstance(sp, str) or not sp or not isinstance(name, str) or not name:
            continue
        st = st.lower() if isinstance(st, str) and st else 'unknown'
        if st in PAUSED_STATES or st == 'error':
            continue
        out.append((st, posixpath.normpath(posixpath.join(sp, name))))
    return sorted(out, key=lambda r: (r[1], r[0]))


def pause_plan(rows, torrents):
    """-> (to_pause, already_paused, blockers). Hashes owning the rows, split into
    ones we may pause (running) and ones someone else already paused (left alone).
    blockers lists every row that is 'wait' (the caller refuses to pause then)."""
    to_pause, already, blockers = [], [], []
    for r in rows:
        if r['status'] == WAIT:
            blockers.append(r)
        for h in r['hashes']:
            if h in to_pause or h in already:
                continue
            (already if is_paused(torrents.get(h)) else to_pause).append(h)
    return to_pause, already, blockers


def to_tsv(rows):
    """status<TAB>reason<TAB>path per line (path last, so tabs in it survive)."""
    return ''.join(f"{r['status']}\t{r['reason']}\t{r['path']}\n" for r in rows)
