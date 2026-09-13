#!/usr/bin/env python3
"""Reference impl for: plex-hnr-guard

Applies the minimal correct change: a single HnR choke point inside the
remove_torrent wrapper, plus routing the two direct-RPC bypasses through it.
The gate applies this, runs verify.sh, and reverts it -- proving the task is
satisfiable AND the verify actually enforces the spec.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

# --- 1. The guard: single choke point in the wrapper ----------------------
OLD_WRAP = """def remove_torrent(torrent_hash, remove_data=True):
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.remove_torrent', 'params': [torrent_hash, remove_data], 'id': 7},
        timeout=30
    )
    resp.raise_for_status()
    log.info(f'Removed torrent {torrent_hash}' + (' and deleted files' if remove_data else ' (files left on disk)'))"""

NEW_WRAP = """def remove_torrent(torrent_hash, remove_data=True, info=None):
    # Hit-and-run guard -- the single choke point every data-deleting removal
    # passes through. Never delete a torrent's files while the tracker still
    # knows it AND its seed obligation is unmet: 9 completed radarr-upgrade
    # torrents were deleted while still seeding + registered -> 9 HnRs. When
    # remove_data is True, fetch current status (unless the caller handed it in)
    # and downgrade to entry-only removal (files kept) if the torrent is still
    # registered and under the SEED_DAYS seed-time requirement.
    if remove_data:
        if info is None:
            try:
                sresp = session.post(
                    f'{DELUGE_URL}/json',
                    json={'method': 'core.get_torrent_status', 'params': [torrent_hash, ['tracker_status', 'seeding_time']], 'id': 8},
                    timeout=10
                )
                sresp.raise_for_status()
                info = sresp.json().get('result') or {}
            except Exception as e:
                log.warning(f'HnR guard: status fetch failed for {torrent_hash} ({e}); assuming still registered, keeping files')
                info = {}
        seeding_time = info.get('seeding_time') or 0
        if not torrent_is_unregistered(info) and seeding_time < SEED_DAYS * 86400:
            log.warning(f'HnR guard: refusing data deletion for {torrent_hash} (still registered, seeded {seeding_time/86400:.1f}d < {SEED_DAYS}d) -- removing entry, keeping files')
            record_activity('hnr-guard', f'refused data deletion for {torrent_hash}: still registered and under {SEED_DAYS}d seed window; files kept on disk')
            remove_data = False
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.remove_torrent', 'params': [torrent_hash, remove_data], 'id': 7},
        timeout=30
    )
    resp.raise_for_status()
    log.info(f'Removed torrent {torrent_hash}' + (' and deleted files' if remove_data else ' (files left on disk)'))"""

assert OLD_WRAP in t, "refimpl anchor (wrapper) not found -- did the target change?"
t = t.replace(OLD_WRAP, NEW_WRAP, 1)

# --- 2. Route purge_stalled_upgrade_torrents through the wrapper -----------
OLD_STALL = """        if to_remove:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.remove_torrents', 'params': [to_remove, False], 'id': 9},
                timeout=30
            )
            log.info(f'Purged {len(to_remove)} stalled upgrade torrents')"""

NEW_STALL = """        if to_remove:
            for _h in to_remove:
                remove_torrent(_h, remove_data=False)
            log.info(f'Purged {len(to_remove)} stalled upgrade torrents')"""

assert OLD_STALL in t, "refimpl anchor (purge_stalled) not found"
t = t.replace(OLD_STALL, NEW_STALL, 1)

# --- 3. Route purge_non_radarr through the wrapper -------------------------
OLD_PURGE = """            for h in chunk:
                try:
                    session.post(
                        f'{DELUGE_URL}/json',
                        json={'method': 'core.remove_torrent', 'params': [h, True], 'id': 66},
                        timeout=15,
                    ).raise_for_status()
                    removed += 1
                except Exception as e:
                    errors.append({'hash': h, 'error': str(e)})"""

NEW_PURGE = """            for h in chunk:
                try:
                    remove_torrent(h)
                    removed += 1
                except Exception as e:
                    errors.append({'hash': h, 'error': str(e)})"""

assert OLD_PURGE in t, "refimpl anchor (purge_non_radarr) not found"
t = t.replace(OLD_PURGE, NEW_PURGE, 1)

p.write_text(t)
print("refimpl applied")
