import os
import re
import time
import logging
import threading
import requests
from flask import Flask, request, jsonify

from media_share import share_bp, init_db as init_share_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.register_blueprint(share_bp)

DELUGE_URL       = os.environ.get('DELUGE_URL', 'http://10.0.0.2:8112')
DELUGE_PASSWORD  = os.environ.get('DELUGE_PASSWORD', 'PASSWORDHERE')
SONARR_URL       = os.environ.get('SONARR_URL', 'http://10.0.0.8:8989')
SONARR_API_KEY   = os.environ.get('SONARR_API_KEY', '')
RADARR_URL       = os.environ.get('RADARR_URL', 'http://10.0.0.7:7878')
RADARR_API_KEY   = os.environ.get('RADARR_API_KEY', '')
SUPERSEDED_LABEL  = 'superseded'
SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')
SEEDING_DIR      = os.environ.get('SEEDING_DIR', '/data/Downloads/Just4Seeding')
SEED_DAYS        = int(os.environ.get('SEED_DAYS', '21'))

PROPER_REPACK_RE = re.compile(r'\b(PROPER|REPACK|RERIP)\b', re.IGNORECASE)
EPISODE_RE       = re.compile(r'S\d{2}E\d{2}', re.IGNORECASE)

session = requests.Session()

# ── Arr API helpers ──────────────────────────────────────────────────────────

def get_sonarr_series_titles(series_id):
    """Return a list of all known title variants for a Sonarr series."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/series/{series_id}',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        titles = set()
        titles.add(data.get('title', ''))
        titles.add(data.get('sortTitle', ''))
        for alt in data.get('alternateTitles', []):
            titles.add(alt.get('title', ''))
        # Normalise: lowercase, replace spaces/colons/ampersands with dot
        normalized = set()
        for t in titles:
            if t:
                normalized.add(t.lower())
                normalized.add(re.sub(r'[ :&]+', '.', t.lower()))
                normalized.add(re.sub(r'[ :&]+', '', t.lower()))
        log.info(f'Sonarr series {series_id} title variants: {normalized}')
        return normalized
    except Exception as e:
        log.error(f'Failed to fetch Sonarr series {series_id}: {e}')
        return set()

def get_radarr_movie_titles(movie_id):
    """Return a list of all known title variants for a Radarr movie."""
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie/{movie_id}',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        titles = set()
        titles.add(data.get('title', ''))
        titles.add(data.get('sortTitle', ''))
        for alt in data.get('alternateTitles', []):
            titles.add(alt.get('title', ''))
        normalized = set()
        for t in titles:
            if t:
                normalized.add(t.lower())
                normalized.add(re.sub(r'[ :&]+', '.', t.lower()))
                normalized.add(re.sub(r'[ :&]+', '', t.lower()))
        log.info(f'Radarr movie {movie_id} title variants: {normalized}')
        return normalized
    except Exception as e:
        log.error(f'Failed to fetch Radarr movie {movie_id}: {e}')
        return set()

# ── Deluge helpers ───────────────────────────────────────────────────────────

def deluge_login():
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'auth.login', 'params': [DELUGE_PASSWORD], 'id': 1},
        timeout=10
    )
    resp.raise_for_status()
    if not resp.json().get('result'):
        raise Exception('Deluge login failed')
    log.info('Logged in to Deluge')

def ensure_label_exists():
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'label.get_labels', 'params': [], 'id': 2},
        timeout=10
    )
    resp.raise_for_status()
    labels = resp.json().get('result', [])
    if SUPERSEDED_LABEL not in labels:
        session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'label.add', 'params': [SUPERSEDED_LABEL], 'id': 3},
            timeout=10
        )
        log.info(f'Created label: {SUPERSEDED_LABEL}')

def set_torrent_label(torrent_hash, label):
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'label.set_torrent', 'params': [torrent_hash, label], 'id': 4},
        timeout=10
    )
    resp.raise_for_status()
    log.info(f'Set label "{label}" on {torrent_hash}')

def move_torrent_storage(torrent_hash, dest):
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.move_storage', 'params': [[torrent_hash], dest], 'id': 5},
        timeout=30
    )
    resp.raise_for_status()
    log.info(f'Moved {torrent_hash} to {dest}')

def remove_torrent(torrent_hash):
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.remove_torrent', 'params': [torrent_hash, True], 'id': 7},
        timeout=30
    )
    resp.raise_for_status()
    log.info(f'Removed torrent {torrent_hash} and deleted files')

def get_all_torrents():
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={
            'method': 'core.get_torrents_status',
            'params': [{}, ['name', 'label', 'save_path', 'seeding_time']],
            'id': 6
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json().get('result', {})

# ── Matching helpers ─────────────────────────────────────────────────────────

def torrent_matches_any_title(torrent_name, title_variants):
    """Check if a torrent name contains any of the title variants."""
    name_normalized = re.sub(r'[ :&]+', '.', torrent_name.lower())
    name_plain = re.sub(r'[ :&.]+', '', torrent_name.lower())
    for variant in title_variants:
        variant_dotted = re.sub(r'[ :&]+', '.', variant)
        variant_plain = re.sub(r'[ :&.]+', '', variant)
        if variant_dotted and (variant_dotted in name_normalized or variant_plain in name_plain):
            return True
    return False

def find_new_torrent_hash(new_filename, torrents):
    """Find the new torrent by exact filename match."""
    new_name = new_filename.lower()
    for torrent_hash, info in torrents.items():
        torrent_name = info.get('name', '').lower()
        if torrent_name in new_name or new_name in torrent_name:
            log.info(f'Identified new torrent: {torrent_hash} - {info.get("name")}')
            return torrent_hash
    return None

def find_season_pack_hash(title_variants, season_term, torrents):
    """Find season pack torrent — matches title variants + season but no episode number."""
    for torrent_hash, info in torrents.items():
        name = info.get('name', '')
        if (torrent_matches_any_title(name, title_variants) and
                season_term.lower() in name.lower() and
                not EPISODE_RE.search(name)):
            log.info(f'Identified season pack: {torrent_hash} - {name}')
            return torrent_hash
    return None

def is_proper_repack(filename):
    return bool(PROPER_REPACK_RE.search(filename))

# ── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup_superseded():
    log.info(f'Running daily cleanup of superseded torrents older than {SEED_DAYS} days...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            log.warning("Cleanup: no torrents returned from Deluge, skipping")
            return
        threshold_seconds = SEED_DAYS * 86400
        removed = 0
        for torrent_hash, info in torrents.items():
            if info.get('label') != SUPERSEDED_LABEL:
                continue
            if info.get('seeding_time', 0) >= threshold_seconds:
                log.info(f'Cleanup: removing {info.get("name")} (seeded {info["seeding_time"]/86400:.1f} days)')
                remove_torrent(torrent_hash)
                removed += 1
        log.info(f'Cleanup complete: removed {removed} superseded torrents')
    except Exception as e:
        log.error(f'Cleanup failed: {e}')

def cleanup_scheduler():
    while True:
        cleanup_superseded()
        cleanup_radarr_queue_dupes()
        time.sleep(86400)


def cleanup_radarr_queue_dupes():
    """Remove duplicate queue entries for the same movie, keeping the highest scoring one."""
    log.info('Running Radarr queue duplicate cleanup...')
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/queue',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'pageSize': 500, 'includeUnknownMovieItems': False},
            timeout=15
        )
        r.raise_for_status()
        records = r.json().get('records', [])

        # Group queue items by movieId
        by_movie = {}
        for item in records:
            movie_id = item.get('movieId')
            if not movie_id:
                continue
            if movie_id not in by_movie:
                by_movie[movie_id] = []
            by_movie[movie_id].append(item)

        removed = 0
        for movie_id, items in by_movie.items():
            if len(items) <= 1:
                continue
            # Sort by custom format score descending, keep highest
            items.sort(key=lambda x: x.get('customFormatScore', 0), reverse=True)
            best = items[0]
            log.info(f'Radarr queue: movie {movie_id} has {len(items)} entries, keeping "{best.get("title")}" (score: {best.get("customFormatScore", 0)})')
            for item in items[1:]:
                queue_id = item.get('id')
                log.info(f'Radarr queue: removing duplicate "{item.get("title")}" (score: {item.get("customFormatScore", 0)})')
                del_r = requests.delete(
                    f'{RADARR_URL}/api/v3/queue/{queue_id}',
                    headers={'X-Api-Key': RADARR_API_KEY},
                    params={'removeFromClient': True, 'blocklist': False},
                    timeout=15
                )
                del_r.raise_for_status()
                removed += 1
        log.info(f'Radarr queue cleanup complete: removed {removed} duplicate entries')
    except Exception as e:
        log.error(f'Radarr queue cleanup failed: {e}')

# ── Core upgrade handler ─────────────────────────────────────────────────────

def handle_upgrade_import(data, source):
    if source == 'Sonarr':
        episode_file = data.get('episodeFile', {})
        new_path = episode_file.get('path', '')
        episodes = data.get('episodes', [])
        if not episodes:
            log.warning(f'{source}: no episode info in payload, skipping')
            return
        ep = episodes[0]
        season_num = ep.get('seasonNumber', 0)
        is_season_pack = len(episodes) > 1
        season_term = f"S{season_num:02d}"
        search_term = season_term if is_season_pack else f"S{season_num:02d}E{ep.get('episodeNumber', 0):02d}"
        if is_season_pack:
            log.info(f'{source}: season pack detected ({len(episodes)} episodes), searching by season "{search_term}"')
        series_id = data.get('series', {}).get('id')
        series_title = data.get('series', {}).get('title', '')
        title_variants = get_sonarr_series_titles(series_id) if series_id else {series_title.lower()}

    elif source == 'Radarr':
        movie_file = data.get('movieFile', {})
        new_path = movie_file.get('path', '')
        movie = data.get('movie', {})
        series_title = movie.get('title', '')
        search_term = str(movie.get('year', ''))
        season_term = None
        is_season_pack = False
        movie_id = movie.get('id')
        title_variants = get_radarr_movie_titles(movie_id) if movie_id else {series_title.lower()}
    else:
        return

    new_filename = new_path.split('/')[-1].rsplit('.', 1)[0] if new_path else ''
    proper_repack = is_proper_repack(new_filename)

    log.info(f'{source}: {"PROPER/REPACK" if proper_repack else "quality upgrade"} imported. New file: "{new_filename}"')
    log.info(f'{source}: looking for superseded torrents matching "{series_title}" {search_term}')

    try:
        deluge_login()
        ensure_label_exists()
        torrents = get_all_torrents()
    except Exception as e:
        log.error(f'Deluge connection failed: {e}')
        return

    # Identify the new torrent to skip it
    # First try downloadId direct hash lookup (most accurate)
    download_id = (data.get('downloadId') or '').lower()
    if download_id and download_id in torrents:
        new_torrent_hash = download_id
        log.info(f'{source}: identified new torrent by downloadId: {new_torrent_hash}')
    elif is_season_pack and season_term:
        new_torrent_hash = find_season_pack_hash(title_variants, season_term, torrents)
        if not new_torrent_hash:
            log.warning(f'{source}: could not identify season pack torrent, will skip none')
    else:
        new_torrent_hash = find_new_torrent_hash(new_filename, torrents)
        if not new_torrent_hash:
            log.warning(f'{source}: could not identify new torrent by filename, will skip none')

    if new_torrent_hash:
        log.info(f'{source}: will skip new torrent {new_torrent_hash}')

    # Find and supersede old torrents
    for torrent_hash, info in torrents.items():
        if torrent_hash == new_torrent_hash:
            continue
        if info.get('label') == SUPERSEDED_LABEL:
            continue
        name = info.get('name', '')
        if torrent_matches_any_title(name, title_variants) and search_term.lower() in name.lower():
            if proper_repack:
                log.info(f'{source}: immediately deleting {torrent_hash} - {name} (proper/repack)')
                remove_torrent(torrent_hash)
            else:
                log.info(f'{source}: superseding {torrent_hash} - {name}')
                set_torrent_label(torrent_hash, SUPERSEDED_LABEL)
                move_torrent_storage(torrent_hash, SEEDING_DIR)


def is_upgrade_sonarr(data):
    """Check if this grab is an upgrade by seeing if the episode already has a file."""
    try:
        episodes = data.get('episodes', [])
        if not episodes:
            return False
        episode_id = episodes[0].get('id')
        if not episode_id:
            return False
        r = requests.get(
            f"{SONARR_URL}/api/v3/episode/{episode_id}",
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get('hasFile', False)
    except Exception as e:
        log.error(f"Sonarr upgrade check failed: {e}")
        return False

def is_upgrade_radarr(data):
    """Check if this grab is an upgrade by seeing if the movie already has a file."""
    try:
        movie_id = data.get('movie', {}).get('id')
        if not movie_id:
            return False
        r = requests.get(
            f"{RADARR_URL}/api/v3/movie/{movie_id}",
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get('hasFile', False)
    except Exception as e:
        log.error(f"Radarr upgrade check failed: {e}")
        return False

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
        return

    # Only throttle if release is over 10GB
    release_size = data.get('release', {}).get('size', 0)
    size_gb = release_size / (1024 ** 3)
    if size_gb < 10:
        log.info(f"{source}: upgrade grab {download_id} is {size_gb:.1f}GB, under 10GB threshold, not throttling")
        return

    upgrade_label = SONARR_UPG_LABEL if source == 'Sonarr' else RADARR_UPG_LABEL
    log.info(f"{source}: upgrade grab {download_id} is {size_gb:.1f}GB, will label as '{upgrade_label}'")
    # Brief delay to let Deluge register the torrent
    time.sleep(3)
    try:
        deluge_login()
        ensure_label_exists_named(upgrade_label)
        set_torrent_label(download_id, upgrade_label)
        session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'core.queue_bottom', 'params': [[download_id]], 'id': 11},
            timeout=10
        )
        log.info(f"{source}: moved {download_id} to bottom of queue")
    except Exception as e:
        log.error(f"{source}: failed to label upgrade torrent: {e}")

def ensure_label_exists_named(label):
    resp = session.post(
        f"{DELUGE_URL}/json",
        json={"method": "label.get_labels", "params": [], "id": 2},
        timeout=10
    )
    resp.raise_for_status()
    labels = resp.json().get("result", [])
    if label not in labels:
        session.post(
            f"{DELUGE_URL}/json",
            json={"method": "label.add", "params": [label], "id": 3},
            timeout=10
        )
        log.info(f"Created label: {label}")


def radarr_bulk_search():
    """Trigger a search for all monitored movies in Radarr to catch missed upgrades."""
    log.info('Running monthly Radarr bulk search for upgrades...')
    try:
        movies_r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=15
        )
        movies_r.raise_for_status()
        movie_ids = [m['id'] for m in movies_r.json() if m.get('monitored')]
        if not movie_ids:
            log.warning('Radarr bulk search: no monitored movies found, skipping')
            return
        r = requests.post(
            f'{RADARR_URL}/api/v3/command',
            headers={'X-Api-Key': RADARR_API_KEY},
            json={'name': 'MoviesSearch', 'movieIds': movie_ids},
            timeout=30
        )
        r.raise_for_status()
        log.info(f'Radarr bulk search triggered for {len(movie_ids)} movies: {r.json().get("name")} (id: {r.json().get("id")})')
    except Exception as e:
        log.error(f'Radarr bulk search failed: {e}')

def relabel_radarr_upgrades():
    """Check radarr-labeled torrents in Deluge and relabel upgrades."""
    log.info('Relabeling Radarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        # Get all radarr-labeled torrents that aren't already upgrade-labeled
        radarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'radarr'}
        if not radarr_torrents:
            log.info('No radarr-labeled torrents to check')
            return
        # Check each against Radarr API to see if movie already has a file
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=15
        )
        r.raise_for_status()
        movies = {m['id']: m for m in r.json()}
        # Get queue to match downloadIds to movieIds
        q = requests.get(
            f'{RADARR_URL}/api/v3/queue',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'pageSize': 500},
            timeout=15
        )
        q.raise_for_status()
        queue_records = q.json().get('records', [])
        # Map downloadId to movieId
        download_to_movie = {rec['downloadId'].lower(): rec.get('movieId') for rec in queue_records if rec.get('downloadId')}
        relabeled = 0
        relabeled_hashes = []
        for torrent_hash, info in radarr_torrents.items():
            movie_id = download_to_movie.get(torrent_hash.lower())
            if not movie_id:
                continue
            movie = movies.get(movie_id)
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
        log.info(f'Relabeled {relabeled} torrents as radarr-upgrade')
    except Exception as e:
        log.error(f'Radarr upgrade relabeling failed: {e}')

def purge_stalled_upgrade_torrents():
    """Remove radarr-upgrade torrents that haven't downloaded more than 5MB."""
    log.info('Purging stalled radarr-upgrade torrents...')
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'core.get_torrents_status', 'params': [{}, ['name', 'label', 'progress', 'total_done']], 'id': 6},
            timeout=10
        )
        resp.raise_for_status()
        torrents = resp.json().get('result', {})
        if not torrents:
            return
        to_remove = []
        for h, i in torrents.items():
            if i.get('label') == RADARR_UPG_LABEL:
                total_done = i.get('total_done', 0)
                if total_done < 5 * 1024 * 1024:  # less than 5MB downloaded
                    log.info(f'Purging stalled upgrade: {i.get("name")} ({total_done/1024/1024:.1f}MB downloaded)')
                    to_remove.append(h)
                else:
                    log.info(f'Skipping in-progress upgrade: {i.get("name")} ({total_done/1024/1024:.1f}MB downloaded)')
        if to_remove:
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.remove_torrents', 'params': [to_remove, False], 'id': 9},
                timeout=30
            )
            log.info(f'Purged {len(to_remove)} stalled upgrade torrents')
        else:
            log.info('No stalled upgrade torrents to purge')
    except Exception as e:
        log.error(f'Purge stalled upgrades failed: {e}')

def monthly_search_scheduler():
    """
    On the 1st of each month:
    1. Purge stalled radarr-upgrade torrents
    2. Wait 30 minutes
    3. Trigger Radarr bulk search
    4. Wait 90 minutes
    5. Relabel new upgrade torrents
    """
    import datetime
    last_run_month = None
    while True:
        now = datetime.datetime.now()
        if now.day == 1 and now.month != last_run_month:
            last_run_month = now.month
            log.info('Monthly upgrade cycle starting: purging stalled upgrades...')
            purge_stalled_upgrade_torrents()
            log.info('Waiting 30 minutes before bulk search...')
            time.sleep(1800)  # 30 minutes
            radarr_bulk_search()
            log.info('Waiting 90 minutes before relabeling upgrades...')
            time.sleep(5400)  # 90 minutes
            relabel_radarr_upgrades()
            log.info('Monthly upgrade cycle complete')
        time.sleep(3600)  # check every hour


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
        if not top_hashes and not bottom_hashes:
            log.info('No torrents to reprioritize')
    except Exception as e:
        log.error(f'Queue prioritization failed: {e}')

def priority_scheduler():
    """Run queue prioritization every hour."""
    while True:
        time.sleep(3600)
        prioritize_normal_torrents()

# ── Routes ───────────────────────────────────────────────────────────────────


@app.route('/webhook/radarr', methods=['POST'])
def radarr_webhook():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get('eventType', '')
    log.info(f'Radarr event: {event} | isUpgrade: {data.get("isUpgrade")} | downloadId: {data.get("downloadId")}')
    if event == 'Grab':
        threading.Thread(target=handle_grab, args=(data, 'Radarr'), daemon=True).start()
    elif event == 'Download' and data.get('isUpgrade'):
        handle_upgrade_import(data, 'Radarr')
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook/sonarr', methods=['POST'])
def sonarr_webhook():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get('eventType', '')
    log.info(f'Sonarr event: {event} | isUpgrade: {data.get("isUpgrade")} | downloadId: {data.get("downloadId")}')
    if event == 'Grab':
        threading.Thread(target=handle_grab, args=(data, 'Sonarr'), daemon=True).start()
    elif event == 'Download' and data.get('isUpgrade'):
        handle_upgrade_import(data, 'Sonarr')
    return jsonify({'status': 'ok'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 9876))
    log.info(f'Starting arr-webhook listener on port {port}')
    log.info(f'Superseded torrents will be auto-removed after {SEED_DAYS} days of seeding')
    init_share_db()
    t = threading.Thread(target=cleanup_scheduler, daemon=True)
    t.start()
    t2 = threading.Thread(target=monthly_search_scheduler, daemon=True)
    t2.start()
    t3 = threading.Thread(target=priority_scheduler, daemon=True)
    t3.start()
    app.run(host='0.0.0.0', port=port, threaded=True)
