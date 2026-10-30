"""Slow, capped torrent migrations.

Any operation that makes Deluge re-read or relocate a torrent's data
(move_storage, re-point, force_recheck) runs under the same 30 KB/s download
cap the upgrade labels use, and the cap stays on until the torrent is verified
Seeding. Migrations are paced: a few per tick, one at a time, and a tick pauses
the moment any torrent is in Error. Dry-run unless MIGRATION_APPLY=1.

`rpc(method, params)` is the Deluge JSON-RPC call (returns `result`).
"""
import os
import threading
import time

CAP_FIELD = 'max_download_speed'
CAP_KBPS = 30
DEFAULT_PER_TICK = 5
HARD_MAX_PER_TICK = 25

_inflight = threading.Lock()


def apply_cap(rpc, torrent_hash):
    """Set the 30 KB/s cap; return the torrent's previous limit."""
    status = rpc('core.get_torrent_status', [torrent_hash, [CAP_FIELD]]) or {}
    prior = status.get(CAP_FIELD, -1)
    rpc('core.set_torrent_options', [[torrent_hash], {CAP_FIELD: CAP_KBPS}])
    return prior


def restore_cap(rpc, torrent_hash, prior):
    rpc('core.set_torrent_options', [[torrent_hash], {CAP_FIELD: prior}])


def capped_operation(rpc, torrent_hash, op, sleep=time.sleep, poll_seconds=30, max_polls=120):
    """Cap, run op(), wait for Seeding, restore. Returns True only when the
    torrent was verified Seeding; otherwise the cap is left on."""
    prior = apply_cap(rpc, torrent_hash)
    if op() is False:
        return False
    for _ in range(max_polls):
        state = (rpc('core.get_torrent_status', [torrent_hash, ['state']]) or {}).get('state')
        if state == 'Seeding':
            restore_cap(rpc, torrent_hash, prior)
            return True
        if state == 'Error':
            return False
        sleep(poll_seconds)
    return False


def per_tick_limit(env=None):
    env = os.environ if env is None else env
    try:
        n = int(env.get('MIGRATION_PER_TICK', DEFAULT_PER_TICK))
    except ValueError:
        n = DEFAULT_PER_TICK
    return max(0, min(n, HARD_MAX_PER_TICK))


def any_error(rpc):
    torrents = rpc('core.get_torrents_status', [{}, ['state']]) or {}
    return any(t.get('state') == 'Error' for t in torrents.values())


def run_migrations(rpc, items, env=None, sleep=time.sleep):
    """items: list of (torrent_hash, op). Runs up to per_tick_limit() of them,
    sequentially, each under capped_operation."""
    env = os.environ if env is None else env
    apply = env.get('MIGRATION_APPLY', '0') == '1'
    result = {'dry_run': not apply, 'done': [], 'failed': [], 'planned': [], 'paused': False}
    if not _inflight.acquire(blocking=False):
        result['skipped'] = 'migration-in-flight'
        return result
    try:
        for torrent_hash, op in items[:per_tick_limit(env)]:
            if any_error(rpc):
                result['paused'] = True
                break
            if not apply:
                result['planned'].append(torrent_hash)
                continue
            ok = capped_operation(rpc, torrent_hash, op, sleep=sleep)
            result['done' if ok else 'failed'].append(torrent_hash)
            if not ok:
                break
    finally:
        _inflight.release()
    return result
