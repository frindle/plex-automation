"""Torrent gate for the Unraid glacier mover (tiering/glacier-mover.sh).

Given file paths the mover wants to push to the glacier disk (written in
Deluge's namespace, e.g. /data/Media/TV Shows/Show/S01/ep.mkv), decide per
path:
  free     no Deluge torrent owns the file
  release  every owning torrent has met the seeding rule: remove the torrent
           from Deluge, keep its data, then the file may move
  wait     an owning torrent is still inside the seeding rule (or unfinished,
           or its state is unknown): leave the file, try again another night

The seeding rule is the one remove_torrent() enforces (the same floor the RAR
sets and episode retention rely on): SEED_DAYS of seeding, torrent complete.
Pure logic here; arr-webhook.py supplies the Deluge data and does the removal.
"""
import posixpath

FREE, RELEASE, WAIT = 'free', 'release', 'wait'
FIELDS = ['name', 'save_path', 'seeding_time', 'progress', 'state']


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


def seed_state(t, seed_days):
    """(ok, reason). ok only when complete and seeded >= seed_days; unknown -> not ok."""
    t = t or {}
    st = t.get('seeding_time')
    if not isinstance(st, (int, float)):
        return False, 'seed time unknown'
    prog = t.get('progress')
    if not isinstance(prog, (int, float)) or prog < 100:
        return False, 'torrent not complete'
    if st < seed_days * 86400:
        return False, f'seeded {round(st / 86400, 1)}d of {seed_days}d'
    return True, 'seed rule met'


def classify_paths(paths, torrents, seed_days):
    """-> [{'path', 'status', 'reason', 'hashes'}] in input order."""
    roots = content_roots(torrents)
    out = []
    for path in paths:
        hs = owners(path, roots)
        if not hs:
            out.append({'path': path, 'status': FREE, 'reason': 'no torrent', 'hashes': []})
            continue
        states = [(h,) + seed_state(torrents[h], seed_days) for h in hs]
        blockers = [f'{h[:8]} {why}' for h, ok, why in states if not ok]
        if blockers:
            out.append({'path': path, 'status': WAIT, 'reason': '; '.join(blockers), 'hashes': hs})
        else:
            out.append({'path': path, 'status': RELEASE, 'reason': 'seed rule met', 'hashes': hs})
    return out


def apply_releases(rows, torrents, remove, deferred):
    """Remove each released torrent once, keeping data. `remove(hash, info)` is
    remove_torrent(h, False, info), which re-checks the seed floor itself and
    returns `deferred` when it refuses. Any failure turns every path that
    torrent owns into 'wait'. Mutates and returns rows."""
    done, failed = set(), {}
    for row in rows:
        if row['status'] != RELEASE:
            continue
        for h in row['hashes']:
            if h in done or h in failed:
                continue
            try:
                ret = remove(h, torrents.get(h) or {})
            except Exception as e:  # one bad torrent must not stop the batch
                failed[h] = f'removal error: {e}'
                continue
            if ret == deferred or ret is False:
                failed[h] = 'removal deferred by seed floor'
            else:
                done.add(h)
    for row in rows:
        bad = [h for h in row['hashes'] if h in failed]
        if row['status'] == RELEASE and bad:
            row['status'] = WAIT
            row['reason'] = f'{bad[0][:8]} {failed[bad[0]]}'
    return rows


def to_tsv(rows):
    """status<TAB>reason<TAB>path per line (path last, so tabs in it survive)."""
    return ''.join(f"{r['status']}\t{r['reason']}\t{r['path']}\n" for r in rows)
