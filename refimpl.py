#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority-s2-priority-hashes-list-sen

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

OLD = r'''def prioritize_normal_torrents():
    """Every hour, move sonarr/radarr labeled torrents to top and upgrade-labeled torrents to bottom of Deluge queue."""
    log.info('Reordering Deluge queue: normal downloads to top, upgrades to bottom...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        priority_labels = {'sonarr', 'radarr'}
        upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}
        top_hashes = [h for h, i in torrents.items() if i.get('label', '') in priority_labels]
        bottom_hashes = [h for h, i in torrents.items() if i.get('label', '') in upgrade_labels]
        if top_hashes:
            resp = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_top', 'params': [top_hashes], 'id': 8},
                timeout=10
            )
            resp.raise_for_status()
            log.info(f'Moved {len(top_hashes)} sonarr/radarr torrents to top of queue')
        if bottom_hashes:
            resp = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [bottom_hashes], 'id': 11},
                timeout=10
            )
            resp.raise_for_status()
            log.info(f'Moved {len(bottom_hashes)} upgrade torrents to bottom of queue')
        if not top_hashes and not bottom_hashes:'''

NEW = r'''def _select_priority_hashes(torrents, priority_labels):
    """Hashes (in torrent-map order) whose label is in `priority_labels` -- the list sent to core.queue_top."""
    return [h for h, i in torrents.items() if i.get('label', '') in priority_labels]

def prioritize_normal_torrents():
    """Every hour, move sonarr/radarr labeled torrents to top and upgrade-labeled torrents to bottom of Deluge queue."""
    log.info('Reordering Deluge queue: normal downloads to top, upgrades to bottom...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        priority_labels = {'sonarr', 'radarr'}
        upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}
        priority_hashes = _select_priority_hashes(torrents, priority_labels)
        bottom_hashes = [h for h, i in torrents.items() if i.get('label', '') in upgrade_labels]
        if priority_hashes:
            resp = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_top', 'params': [priority_hashes], 'id': 8},
                timeout=10
            )
            resp.raise_for_status()
            log.info(f'Moved {len(priority_hashes)} sonarr/radarr torrents to top of queue')
        if bottom_hashes:
            resp = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [bottom_hashes], 'id': 11},
                timeout=10
            )
            resp.raise_for_status()
            log.info(f'Moved {len(bottom_hashes)} upgrade torrents to bottom of queue')
        if not priority_hashes and not bottom_hashes:'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
