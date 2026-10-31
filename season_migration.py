"""Season-pack migration: move library-seeded Sonarr season packs out of the
Plex library into Just4Seeding without touching the library files.

Per torrent (one in flight, under speed_cap's 30 KB/s cap, kept until the
torrent is verified Seeding): hardlink the data with `cp -al` into
SEEDING_DIR/<name>, verify every file shares its inode, pause, move_storage to
SEEDING_DIR, verify again (the library copy must still be there), recheck,
resume. A pilot of one torrent runs first, then batches of BATCH. Any torrent
in Error pauses the run. Dry-run unless MIGRATION_APPLY=1. Runs on its own
worker thread (start_worker), never the scheduler thread.

`rpc(method, params)` is the Deluge JSON-RPC call (returns `result`).
"""
import os
import posixpath
import subprocess
import threading
import time

import episode_retention
import speed_cap

SEEDING_DIR = '/data/Downloads/Just4Seeding'
MEDIA_ROOT = '/data/Media'
PILOT = 1
BATCH = 10
MOVE_POLLS = 120
DEFAULT_INTERVAL = 3600
FIELDS = ['name', 'label', 'state', 'progress', 'save_path']


def candidates(torrents):
    result = []
    for h, t in (torrents or {}).items():
        if t is None:
            continue
        name = t.get('name') or ''
        if not name or name in ('.', '..') or '/' in name:
            continue
        if episode_retention.classify_light(t) != 'season-pack':
            continue
        if t.get('state') != 'Seeding':
            continue
        if (t.get('progress') or 0) < 100:
            continue
        save_path = t.get('save_path') or ''
        if not save_path or not episode_retention.under_media(save_path):
            continue
        result.append(h)
    return sorted(result)


def same_inodes(src, dst):
    """Return True when src and dst both exist, neither is a symlink, and dst
    is a full hardlink mirror: for a file, dst is a file with the same
    (st_dev, st_ino) via os.lstat; for a dir, every regular file under src
    (os.walk) has a non-symlink file at the same relative path under dst
    with the same (st_dev, st_ino), any symlink (file or dir) under src
    returns False, at least one file exists, and dst holds exactly as many
    files as src."""
    try:
        src_stat = os.lstat(src)
        dst_stat = os.lstat(dst)
    except (OSError, ValueError):
        return False
    if os.path.islink(src) or os.path.islink(dst):
        return False
    if not os.path.isdir(src):
        if not os.path.isfile(dst):
            return False
        src_st = os.lstat(src)
        dst_st = os.lstat(dst)
        return (src_st.st_dev == dst_st.st_dev and
                src_st.st_ino == dst_st.st_ino)
    src_files = []
    for root, dirs, files in os.walk(src):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                return False
        for f in files:
            fpath = os.path.join(root, f)
            if os.path.islink(fpath):
                return False
            src_files.append(fpath)
    if not src_files:
        return False
    dst_files = [os.path.join(r, f) for r, _d, fs in os.walk(dst) for f in fs]
    if len(src_files) != len(dst_files):
        return False
    for fpath in src_files:
        rel = os.path.relpath(fpath, src)
        dst_path = os.path.join(dst, rel)
        if not os.path.lexists(dst_path):
            return False
        if os.path.islink(dst_path):
            return False
        if not os.path.isfile(dst_path):
            return False
        src_st = os.lstat(fpath)
        dst_st = os.lstat(dst_path)
        if src_st.st_dev != dst_st.st_dev or src_st.st_ino != dst_st.st_ino:
            return False
    return True




def migrate_op(rpc, torrent_hash, t, seeding_dir=SEEDING_DIR, run=subprocess.run,
               sleep=time.sleep, poll_seconds=10):
    src = posixpath.join(t['save_path'], t['name'])
    dst = posixpath.join(seeding_dir, t['name'])

    def op():
        # 1) If dst does not exist, cp -al src dst
        if not os.path.lexists(dst):
            result = run(['cp', '-al', src, dst], check=False)
            if result.returncode != 0:
                return False

        # 2) Same inodes check
        if not same_inodes(src, dst):
            return False

        # 3) Pause and move storage
        rpc('core.pause_torrent', [[torrent_hash]])
        rpc('core.move_storage', [[torrent_hash], seeding_dir])

        # 4) Poll for move completion
        for _ in range(MOVE_POLLS):
            st = rpc('core.get_torrent_status', [torrent_hash, ['save_path', 'state']]) or {}
            if st.get('state') == 'Error':
                return False
            save_path = st.get('save_path', '')
            if st.get('state') != 'Moving' and os.path.normpath(save_path) == os.path.normpath(seeding_dir):
                break
            sleep(poll_seconds)
        else:
            return False

        # 5) Verify source still matches destination
        if not same_inodes(src, dst):
            return False

        # 6) Force recheck and resume
        rpc('core.force_recheck', [torrent_hash])
        rpc('core.resume_torrent', [torrent_hash])
        return True

    return op




def run_once(rpc, state, fetch=None, env=None, run=subprocess.run, sleep=time.sleep):
    if env is None:
        env = os.environ
    torrents = fetch() if fetch else rpc('core.get_torrents_status', [{}, FIELDS]) or {}
    hashes = candidates(torrents)
    limit = BATCH if state.get('pilot_done') else PILOT
    items = [(h, migrate_op(rpc, h, torrents[h], seeding_dir=env.get('SEEDING_DIR', SEEDING_DIR), run=run, sleep=sleep)) for h in hashes[:limit]]
    result = speed_cap.run_migrations(rpc, items, env=dict(env, MIGRATION_PER_TICK=str(limit)), sleep=sleep)
    result['candidates'] = len(hashes)
    if result['done']:
        state['pilot_done'] = True
    state['last'] = result
    return result


def start_worker(rpc, state, fetch=None, env=None, interval=DEFAULT_INTERVAL, stop=None,
                 tick=None):
    if stop is None:
        stop = threading.Event()
    if tick is None:
        tick = run_once
    def _loop():
        while not stop.is_set():
            try:
                tick(rpc, state, fetch=fetch, env=env)
            except Exception as e:
                state['last_error'] = '%s: %s' % (type(e).__name__, e)
            stop.wait(interval)
    th = threading.Thread(target=_loop, name='season-migration', daemon=True)
    th.start()
    return th
