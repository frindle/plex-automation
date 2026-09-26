#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority-s3-sonarr-recent-fasttrack

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

# --- Change 1: add SONARR_UPG_PRIORITY_LABEL next to SONARR_UPG_LABEL --------
OLD_CONST = r"""SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')"""

NEW_CONST = r"""SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')"""

assert OLD_CONST in t, "refimpl anchor (constants) not found -- did the target change?"
t = t.replace(OLD_CONST, NEW_CONST, 1)

# --- Change 2: branch on episode air year inside relabel_sonarr_upgrades ----
OLD_BODY = r"""            if has_file:
                log.info(f'Relabeling upgrade: {info.get("name")}')
                ensure_label_exists_named(SONARR_UPG_LABEL)
                set_torrent_label(torrent_hash, SONARR_UPG_LABEL)
                relabeled_hashes.append(torrent_hash)
        if relabeled_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [relabeled_hashes], 'id': 10},
                timeout=10
            )
            log.info(f'Moved {len(relabeled_hashes)} sonarr upgrade torrents to bottom of queue')
        log.info(f'Relabeled {len(relabeled_hashes)} torrents as {SONARR_UPG_LABEL}')
        return len(relabeled_hashes)"""

NEW_BODY = r"""            if has_file:
                log.info(f'Relabeling upgrade: {info.get("name")}')
                air_date = er.json().get('airDateUtc') or er.json().get('airDate') or ''
                try:
                    air_year = int(str(air_date)[:4])
                except (TypeError, ValueError):
                    air_year = 0
                if _is_recent_year(air_year):
                    ensure_label_exists_named(SONARR_UPG_PRIORITY_LABEL)
                    set_torrent_label(torrent_hash, SONARR_UPG_PRIORITY_LABEL)
                    priority_hashes.append(torrent_hash)
                else:
                    ensure_label_exists_named(SONARR_UPG_LABEL)
                    set_torrent_label(torrent_hash, SONARR_UPG_LABEL)
                    relabeled_hashes.append(torrent_hash)
        if relabeled_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [relabeled_hashes], 'id': 10},
                timeout=10
            )
            log.info(f'Moved {len(relabeled_hashes)} sonarr upgrade torrents to bottom of queue')
        if priority_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_top', 'params': [priority_hashes], 'id': 10},
                timeout=10
            )
            log.info(f'Moved {len(priority_hashes)} recent sonarr upgrade torrents to top of queue')
        relabeled_count = len(relabeled_hashes) + len(priority_hashes)
        log.info(f'Relabeled {relabeled_count} torrents as {SONARR_UPG_LABEL}')
        return relabeled_count"""

assert OLD_BODY in t, "refimpl anchor (relabel body) not found -- did the target change?"
t = t.replace(OLD_BODY, NEW_BODY, 1)

# --- Change 3: add priority_hashes list alongside relabeled_hashes ----------
OLD_LISTS = r"""        download_to_episode = {}
        for rec in q.json().get('records', []):
            dl = rec.get('downloadId')
            ep = rec.get('episodeId')
            if dl and ep:
                download_to_episode.setdefault(dl.lower(), ep)
        relabeled_hashes = []"""

NEW_LISTS = r"""        download_to_episode = {}
        for rec in q.json().get('records', []):
            dl = rec.get('downloadId')
            ep = rec.get('episodeId')
            if dl and ep:
                download_to_episode.setdefault(dl.lower(), ep)
        relabeled_hashes = []
        priority_hashes = []"""

assert OLD_LISTS in t, "refimpl anchor (hash lists) not found -- did the target change?"
t = t.replace(OLD_LISTS, NEW_LISTS, 1)

p.write_text(t)
print("refimpl applied")
