#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority

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


def _replace(text, old, new):
    assert old in text, "refimpl anchor not found -- did the target change?"
    return text.replace(old, new, 1)


# 1. New label constants next to SONARR_UPG_LABEL/RADARR_UPG_LABEL.
OLD_1 = r"""SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')
"""
NEW_1 = r"""SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')
# Fast-track lane: upgrades whose underlying release is RECENT (current year or
# the immediately preceding one) behave like normal priority downloads -- top of
# the Deluge queue, never bottomed by prioritize_normal_torrents.
SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')
RADARR_UPG_PRIORITY_LABEL = os.environ.get('RADARR_UPGRADE_PRIORITY_LABEL', 'radarr-upgrade-recent')
"""

# 2. _is_recent_year helper, right before relabel_radarr_upgrades.
OLD_2 = r'''def relabel_radarr_upgrades():
    """Check radarr-labeled torrents in Deluge and relabel upgrades."""
'''
NEW_2 = r'''def _is_recent_year(year, now=None):
    """True iff `year` is a truthy int-or-int-like value within the last two
    calendar years (current year or the immediately preceding one). None/0/
    missing/non-numeric -> False."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False
    return bool(y) and y >= now.year - 1

def relabel_radarr_upgrades():
    """Check radarr-labeled torrents in Deluge and relabel upgrades."""
'''

# 3. Radarr: split recent (priority lane, queue_top) from older (throttled lane).
OLD_3 = r"""            movie = movies.get(movie_id)
            if movie and movie.get('hasFile'):
                log.info(f'Relabeling upgrade: {info.get("name")}')
                ensure_label_exists_named(RADARR_UPG_LABEL)
                set_torrent_label(torrent_hash, RADARR_UPG_LABEL)
                relabeled_hashes.append(torrent_hash)
                relabeled += 1
        if relabeled_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [relabeled_hashes], 'id': 10},
                timeout=10
            )
            log.info(f'Moved {len(relabeled_hashes)} upgrade torrents to bottom of queue')
"""
NEW_3 = r"""            movie = movies.get(movie_id)
            if movie and movie.get('hasFile'):
                log.info(f'Relabeling upgrade: {info.get("name")}')
                if _is_recent_year(movie.get('year')):
                    # Recent release (this year or last): fast-track it like a
                    # normal priority download instead of the throttled lane.
                    ensure_label_exists_named(RADARR_UPG_PRIORITY_LABEL)
                    set_torrent_label(torrent_hash, RADARR_UPG_PRIORITY_LABEL)
                    priority_hashes.append(torrent_hash)
                else:
                    ensure_label_exists_named(RADARR_UPG_LABEL)
                    set_torrent_label(torrent_hash, RADARR_UPG_LABEL)
                    relabeled_hashes.append(torrent_hash)
                relabeled += 1
        if relabeled_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_bottom', 'params': [relabeled_hashes], 'id': 10},
                timeout=10
            )
            log.info(f'Moved {len(relabeled_hashes)} upgrade torrents to bottom of queue')
        if priority_hashes:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_top', 'params': [priority_hashes], 'id': 12},
                timeout=10
            )
            log.info(f'Moved {len(priority_hashes)} recent upgrade torrents to top of queue')
"""

# 3b. Radarr: declare priority_hashes next to relabeled_hashes.
OLD_3B = r"""        download_to_movie = {rec['downloadId'].lower(): rec.get('movieId') for rec in queue_records if rec.get('downloadId')}
        relabeled = 0
        relabeled_hashes = []
"""
NEW_3B = r"""        download_to_movie = {rec['downloadId'].lower(): rec.get('movieId') for rec in queue_records if rec.get('downloadId')}
        relabeled = 0
        relabeled_hashes = []
        priority_hashes = []
"""

# 4. Sonarr: read airDateUtc/airDate from the episode response already fetched.
OLD_4 = r"""            try:
                er = requests.get(
                    f'{SONARR_URL}/api/v3/episode/{episode_id}',
                    headers={'X-Api-Key': SONARR_API_KEY},
                    timeout=10
                )
                er.raise_for_status()
                has_file = er.json().get('hasFile', False)
            except Exception as e:
                log.warning(f'Sonarr episode {episode_id} lookup failed: {e}')
                continue
            if has_file:
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
        return len(relabeled_hashes)
"""
NEW_4 = r"""            has_file = False
            air_year = None
            try:
                er = requests.get(
                    f'{SONARR_URL}/api/v3/episode/{episode_id}',
                    headers={'X-Api-Key': SONARR_API_KEY},
                    timeout=10
                )
                er.raise_for_status()
                ep_data = er.json() or {}
                has_file = ep_data.get('hasFile', False)
                air_date = ep_data.get('airDateUtc') or ep_data.get('airDate') or ''
                try:
                    air_year = int(str(air_date)[:4]) if str(air_date)[:4].isdigit() else None
                except (TypeError, ValueError):
                    air_year = None
            except Exception as e:
                log.warning(f'Sonarr episode {episode_id} lookup failed: {e}')
                continue
            if has_file:
                log.info(f'Relabeling upgrade: {info.get("name")}')
                if _is_recent_year(air_year):
                    # Recent episode (aired this year or last): fast-track it.
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
                json={'method': 'core.queue_top', 'params': [priority_hashes], 'id': 12},
                timeout=10
            )
            log.info(f'Moved {len(priority_hashes)} recent sonarr upgrade torrents to top of queue')
        total = len(relabeled_hashes) + len(priority_hashes)
        log.info(f'Relabeled {total} torrents ({len(relabeled_hashes)} throttled, {len(priority_hashes)} fast-tracked)')
        return total
"""

# 4b. Sonarr: declare priority_hashes next to relabeled_hashes.
OLD_4B = r"""        if dl and ep:
                download_to_episode.setdefault(dl.lower(), ep)
        relabeled_hashes = []
"""
NEW_4B = r"""        if dl and ep:
                download_to_episode.setdefault(dl.lower(), ep)
        relabeled_hashes = []
        priority_hashes = []
"""

# 5. prioritize_normal_torrents: recent-upgrade labels join the TOP sweep only.
OLD_5 = r"""        priority_labels = {'sonarr', 'radarr'}
        upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}
"""
NEW_5 = r"""        priority_labels = {'sonarr', 'radarr', SONARR_UPG_PRIORITY_LABEL, RADARR_UPG_PRIORITY_LABEL}
        upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}
"""

for old, new in ((OLD_1, NEW_1), (OLD_2, NEW_2), (OLD_3B, NEW_3B),
                 (OLD_3, NEW_3), (OLD_4B, NEW_4B), (OLD_4, NEW_4),
                 (OLD_5, NEW_5)):
    t = _replace(t, old, new)

p.write_text(t)
print("refimpl applied")
