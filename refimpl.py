#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s9-relabel-count

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

RADARR_OLD = """    log.info('Relabeling Radarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        # Get all radarr-labeled torrents that aren't already upgrade-labeled
        radarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'radarr'}
        if not radarr_torrents:
            log.info('No radarr-labeled torrents to check')
            return"""

RADARR_NEW = """    log.info('Relabeling Radarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return 0
        # Get all radarr-labeled torrents that aren't already upgrade-labeled
        radarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'radarr'}
        if not radarr_torrents:
            log.info('No radarr-labeled torrents to check')
            return 0 # relevance: unobservable"""

RADARR_END_OLD = """        log.info(f'Relabeled {relabeled} torrents as radarr-upgrade')
    except Exception as e:
        log.error(f'Radarr upgrade relabeling failed: {e}')"""

RADARR_END_NEW = """        log.info(f'Relabeled {relabeled} torrents as radarr-upgrade')
        return relabeled
    except Exception as e:
        log.error(f'Radarr upgrade relabeling failed: {e}')
        return 0"""

SONARR_OLD = """    log.info('Relabeling Sonarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        sonarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'sonarr'}
        if not sonarr_torrents:
            log.info('No sonarr-labeled torrents to check')
            return"""

SONARR_NEW = """    log.info('Relabeling Sonarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return 0
        sonarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'sonarr'}
        if not sonarr_torrents:
            log.info('No sonarr-labeled torrents to check')
            return 0 # relevance: unobservable"""

SONARR_END_OLD = """        log.info(f'Relabeled {len(relabeled_hashes)} torrents as {SONARR_UPG_LABEL}')
    except Exception as e:
        log.error(f'Sonarr upgrade relabeling failed: {e}')"""

SONARR_END_NEW = """        log.info(f'Relabeled {len(relabeled_hashes)} torrents as {SONARR_UPG_LABEL}')
        return len(relabeled_hashes)
    except Exception as e:
        log.error(f'Sonarr upgrade relabeling failed: {e}')
        return 0"""

for old, new in ((RADARR_OLD, RADARR_NEW), (RADARR_END_OLD, RADARR_END_NEW),
                  (SONARR_OLD, SONARR_NEW), (SONARR_END_OLD, SONARR_END_NEW)):
    assert old in t, "refimpl anchor not found -- did the target change?\n" + old
    t = t.replace(old, new, 1)

p.write_text(t)
print("refimpl applied")
