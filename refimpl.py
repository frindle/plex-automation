#!/usr/bin/env python3
"""Reference impl for: arr-sonarr-new-request-priority

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''def handle_grab(data, source):
    """
    Fires when Sonarr/Radarr sends a grab to Deluge.
    Check via API if this is an upgrade, then throttle if over 10GB.
    """
    download_id = (data.get('downloadId') or '').lower()
    if not download_id:
        log.warning(f"{source}: On Grab but no downloadId, skipping")
        return

    # Check if this is an upgrade via API since isUpgrade is not in Grab payload
    if source == 'Sonarr':
        upgrade = is_upgrade_sonarr(data)
    else:
        upgrade = is_upgrade_radarr(data)

    if not upgrade:
        log.info(f"{source}: grab {download_id} is a new release, not throttling")
        return'''

NEW = r'''def prioritize_new_sonarr_grab(download_id):
    """Move a NEW (non-upgrade) Sonarr request ahead of everything already in
    the Deluge queue. Upgrades are deliberately pushed to core.queue_bottom so
    they throttle behind real work; new requests used to get nothing at all and
    sat at the FIFO tail behind every in-flight download until the hourly
    prioritize_normal_torrents pass (up to an hour later). Best-effort: any
    Deluge failure is logged and swallowed, never raised out of handle_grab."""
    try:
        deluge_login()
        session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'core.queue_top', 'params': [[download_id]], 'id': 92},
            timeout=10
        )
        log.info(f'Sonarr: new request {download_id} moved to top of Deluge queue')
    except Exception as e:
        log.error(f"Sonarr: failed to prioritize new grab {download_id}: {e}")

def handle_grab(data, source):
    """
    Fires when Sonarr/Radarr sends a grab to Deluge.
    Check via API if this is an upgrade, then throttle if over 10GB.
    """
    download_id = (data.get('downloadId') or '').lower()
    if not download_id:
        log.warning(f"{source}: On Grab but no downloadId, skipping")
        return

    # Check if this is an upgrade via API since isUpgrade is not in Grab payload
    if source == 'Sonarr':
        upgrade = is_upgrade_sonarr(data)
    else:
        upgrade = is_upgrade_radarr(data)

    if not upgrade:
        log.info(f"{source}: grab {download_id} is a new release, not throttling")
        if source == 'Sonarr':
            prioritize_new_sonarr_grab(download_id)
        return'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
