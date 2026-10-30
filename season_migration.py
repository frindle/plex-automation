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
    raise NotImplementedError('not implemented: candidates')


def same_inodes(src, dst):
    raise NotImplementedError('not implemented: same_inodes')


def migrate_op(rpc, torrent_hash, t, seeding_dir=SEEDING_DIR, run=subprocess.run,
               sleep=time.sleep, poll_seconds=10):
    raise NotImplementedError('not implemented: migrate_op')


def run_once(rpc, state, fetch=None, env=None, run=subprocess.run, sleep=time.sleep):
    raise NotImplementedError('not implemented: run_once')


def start_worker(rpc, state, fetch=None, env=None, interval=DEFAULT_INTERVAL, stop=None,
                 tick=None):
    raise NotImplementedError('not implemented: start_worker')
