import os
import re
import json as _json
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
# Root of the movies library — used by /orphan-scan to find files that
# Radarr no longer tracks. Override in the compose file if your layout
# differs from /mnt/user/data/Media/Movies.
MOVIES_LIBRARY   = os.environ.get('MOVIES_LIBRARY', '/mnt/user/data/Media/Movies')
PUSHOVER_TOKEN   = os.environ.get('PUSHOVER_TOKEN', '')
PUSHOVER_USER    = os.environ.get('PUSHOVER_USER', '')
IMPORTBLOCKED_INTERVAL = int(os.environ.get('IMPORTBLOCKED_INTERVAL', '900'))  # 15 min

PROPER_REPACK_RE = re.compile(r'\b(PROPER|REPACK|RERIP)\b', re.IGNORECASE)
EPISODE_RE       = re.compile(r'S\d{2}E\d{2}', re.IGNORECASE)

session = requests.Session()

# ── Arr API helpers ──────────────────────────────────────────────────────────

def get_sonarr_series_titles(series_id):
    """Return a set of title variants for a Sonarr series. Alt titles
    ignored — same reason as Radarr: TMDB alt titles have caused
    catastrophic over-matching in dedup."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/series/{series_id}',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        titles = {data.get('title', ''), data.get('originalTitle', '')}
        normalized = {t.lower() for t in titles if t}
        log.info(f'Sonarr series {series_id} title variants: {normalized}')
        return normalized
    except Exception as e:
        log.error(f'Failed to fetch Sonarr series {series_id}: {e}')
        return set()

def get_radarr_movie_titles(movie_id):
    """Return a set of title variants for a Radarr movie. We deliberately
    IGNORE alternateTitles — Radarr's TMDB-sourced alt-title list has
    included single digits, common English words, and other tokens that
    matched millions of unrelated torrents. Primary title and originalTitle
    are enough for dedup identification; we already require the release
    year to match separately, which catches remakes."""
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie/{movie_id}',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        titles = {data.get('title', ''), data.get('originalTitle', '')}
        normalized = {t.lower() for t in titles if t}
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
    """Check if every word of a variant appears as a whole word in the
    torrent name. Substring matching is dangerous: title "21" matched
    "2160p", title "X" matched "x265", title "It" matched every "it" in
    every filename. Word-based matching kills that class of false positive.

    Requires ALL words of the variant to be present as separate tokens.
    Also requires the variant to contain at least one word of ≥3 chars —
    otherwise short/ambiguous variants like "2" (Radarr sometimes lists a
    lone digit as an alt title for sequels) match every torrent whose
    name contains that digit as a token."""
    name_words = set(re.findall(r'[a-z0-9]+', torrent_name.lower()))
    for variant in title_variants:
        v_words = re.findall(r'[a-z0-9]+', variant.lower())
        if not v_words:
            continue
        if max(len(w) for w in v_words) < 3:
            continue
        if all(w in name_words for w in v_words):
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
        dedup_via_radarr()
        dedup_via_sonarr()
        cleanup_unpacked_torrents()
        time.sleep(86400)


# ── Radarr/Sonarr → Deluge dedup ─────────────────────────────────────────────
# For each *arr movie/series with a tracked file, sweep Deluge for torrents
# that match the title but aren't the tracked file. Relabel those extras as
# 'superseded' so the existing cleanup_superseded pass removes them after
# SEED_DAYS. This is what cleans up the pile-of-dupes from the pre-fix era.

def _extract_year(text):
    """Pull a 4-digit release year (1900-2099) from a torrent or file name.
    Returns int or None. Uses word-boundary lookarounds so we don't hit
    parts of a larger number."""
    if not text:
        return None
    m = re.search(r'(?<!\d)(19\d{2}|20\d{2})(?!\d)', text)
    return int(m.group(1)) if m else None

def _torrent_name_matches_file(torrent_name, tracked_relative_path):
    """True if the torrent name looks like the tracked file (fuzzy match on
    name minus extension). Radarr's relativePath is like 'Movie 2022...mkv';
    the Deluge torrent name may lack the extension or match exactly."""
    if not tracked_relative_path:
        return False
    name = torrent_name.lower()
    tracked = tracked_relative_path.lower()
    # Strip common release extensions from both sides for the comparison
    for ext in ('.mkv', '.mp4', '.avi'):
        if tracked.endswith(ext):
            tracked = tracked[:-len(ext)]
        if name.endswith(ext):
            name = name[:-len(ext)]
    return name == tracked or tracked in name or name in tracked

def _radarr_last_imported_download_id(movie_id):
    """Ask Radarr for the most recent successful import for a movie —
    that's Radarr's chosen keeper, and its downloadId maps to a Deluge
    hash. Returns lowercase hash str or None."""
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/history/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'movieId': movie_id, 'eventType': 'downloadFolderImported'},
            timeout=15,
        )
        r.raise_for_status()
        # Response is a list; take the most recent import event.
        events = r.json()
        if not events:
            return None
        # Sort by date desc if not already
        events.sort(key=lambda e: e.get('date', ''), reverse=True)
        for e in events:
            did = (e.get('downloadId') or '').lower()
            if did:
                return did
        return None
    except Exception as e:
        log.warning(f'Radarr history lookup failed for movie {movie_id}: {e}')
        return None

def dedup_via_radarr(dry_run=False):
    log.info(f'Running Radarr → Deluge dedup pass{" (DRY RUN)" if dry_run else ""}...')
    if not RADARR_API_KEY:
        log.info('  no RADARR_API_KEY, skip')
        return
    try:
        deluge_login()
        ensure_label_exists()
        torrents = get_all_torrents()
        radarr_torrents = {h: i for h, i in torrents.items() if i.get('label') in ('radarr', RADARR_UPG_LABEL)}
        if not radarr_torrents:
            log.info('  no radarr-labeled torrents to check')
            return
        # Pull only movies with a tracked file
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        relabeled = 0
        for movie in r.json():
            if not movie.get('hasFile'):
                continue
            tracked = ((movie.get('movieFile') or {}).get('relativePath')) or ''
            if not tracked:
                continue
            title_variants = get_radarr_movie_titles(movie['id'])
            if not title_variants:
                title_variants = {movie.get('title', '').lower()}
            movie_year = movie.get('year')
            # Match torrents against this movie. Both title AND year must
            # line up — title-only matching bled across sequels/remakes
            # (Ghostbusters '84 vs Ghostbusters II '89, Scream '96 vs
            # Scream '22, etc.). A ±1 year fudge covers release vs
            # production year drift.
            matched_hashes = []
            for h, info in radarr_torrents.items():
                name = info.get('name', '')
                if not torrent_matches_any_title(name, title_variants):
                    continue
                t_year = _extract_year(name)
                if movie_year and t_year and abs(t_year - movie_year) > 1:
                    continue
                matched_hashes.append(h)
            if len(matched_hashes) <= 1:
                continue
            # Safety: only relabel extras if we can identify AT LEAST ONE
            # keeper. Fast path: torrent name matches the currently-tracked
            # file. Fallback path: ask Radarr's history for the downloadId
            # of the last successful import — that hash IS the keeper by
            # definition. Only if BOTH fail do we skip the movie.
            keepers = [h for h in matched_hashes if _torrent_name_matches_file(radarr_torrents[h].get('name', ''), tracked)]
            keeper_source = 'filename-match'
            if not keepers:
                imported_hash = _radarr_last_imported_download_id(movie['id'])
                if imported_hash and imported_hash in {h.lower() for h in matched_hashes}:
                    # imported_hash is lowercase; find the matching original-case hash
                    keepers = [h for h in matched_hashes if h.lower() == imported_hash]
                    keeper_source = 'radarr-history'
            if not keepers:
                log.warning(f'  skip movie {movie["id"]} ({movie.get("title")}): {len(matched_hashes)} torrents matched title but no keeper identified (filename mismatch + no Radarr history) — not relabeling anything')
                continue
            log.info(f'  movie {movie["id"]} ({movie.get("title")}): keeper via {keeper_source} = {keepers[0][:12]}')
            for h in matched_hashes:
                if h in keepers:
                    continue
                name = radarr_torrents[h].get('name', '')
                action = 'WOULD relabel' if dry_run else 'relabeling'
                log.info(f'  {action} superseded: "{name}" (movie {movie["id"]}: {movie.get("title")})')
                if not dry_run:
                    set_torrent_label(h, SUPERSEDED_LABEL)
                relabeled += 1
        log.info(f'Radarr dedup complete{" (DRY RUN)" if dry_run else ""}: {"would relabel" if dry_run else "relabeled"} {relabeled} superseded torrent(s)')
    except Exception as e:
        log.error(f'Radarr dedup failed: {e}')

def dedup_via_sonarr():
    log.info('Running Sonarr → Deluge dedup pass...')
    if not SONARR_API_KEY:
        log.info('  no SONARR_API_KEY, skip')
        return
    try:
        deluge_login()
        ensure_label_exists()
        torrents = get_all_torrents()
        sonarr_torrents = {h: i for h, i in torrents.items() if i.get('label') in ('sonarr', SONARR_UPG_LABEL)}
        if not sonarr_torrents:
            log.info('  no sonarr-labeled torrents to check')
            return
        # Sonarr episodeFile lookup: per series, gather all tracked file
        # relativePaths, then per torrent that matches the series title,
        # relabel superseded if its name doesn't correspond to any tracked file.
        r = requests.get(
            f'{SONARR_URL}/api/v3/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        relabeled = 0
        for series in r.json():
            series_id = series['id']
            title_variants = get_sonarr_series_titles(series_id)
            if not title_variants:
                title_variants = {series.get('title', '').lower()}
            # Fetch tracked episode files
            try:
                ef = requests.get(
                    f'{SONARR_URL}/api/v3/episodefile',
                    headers={'X-Api-Key': SONARR_API_KEY},
                    params={'seriesId': series_id},
                    timeout=20,
                )
                ef.raise_for_status()
                tracked_paths = [(f.get('relativePath') or '').lower() for f in ef.json()]
            except Exception:
                continue
            if not tracked_paths:
                continue
            # Match torrents against this series
            matched_hashes = []
            for h, info in sonarr_torrents.items():
                name = info.get('name', '')
                if torrent_matches_any_title(name, title_variants):
                    matched_hashes.append(h)
            if len(matched_hashes) <= len(tracked_paths):
                continue  # 1 torrent per tracked file is normal
            # Safety: only relabel extras if AT LEAST ONE torrent maps to
            # a currently-tracked file. Otherwise we can't identify winners
            # and would blow away every active seed for the series.
            keepers = set()
            for h in matched_hashes:
                name = sonarr_torrents[h].get('name', '').lower()
                if any(_torrent_name_matches_file(name, p) for p in tracked_paths):
                    keepers.add(h)
            if not keepers:
                log.warning(f'  skip series {series_id} ({series.get("title")}): {len(matched_hashes)} torrents matched but NONE mapped to tracked files — not relabeling anything')
                continue
            for h in matched_hashes:
                if h in keepers:
                    continue
                log.info(f'  relabeling superseded: "{sonarr_torrents[h].get("name")}" (series {series_id}: {series.get("title")})')
                set_torrent_label(h, SUPERSEDED_LABEL)
                relabeled += 1
        log.info(f'Sonarr dedup complete: relabeled {relabeled} superseded torrent(s)')
    except Exception as e:
        log.error(f'Sonarr dedup failed: {e}')


# ── Unpackerr — remove torrents that had to be extracted ────────────────────
# When unpackerr had to unrar a download so *arr could import, the .rar/.r00
# files stay in the torrent's original folder. Radarr hardlinks the extracted
# .mkv into Media, so the torrent's disk-space cost is pure archive. After
# SEED_DAYS have passed since unpack we remove the torrent + its rar files.
#
# We identify "unpacked" torrents by: the torrent's save_path contains .rar
# files AND Radarr/Sonarr has an imported file that references this download.
# Track state in a sidecar JSON so we know the "unpacked at" timestamp.

_UNPACK_STATE_PATH = os.environ.get('UNPACK_STATE_PATH', '/data/unpacked_torrents.json')

def _load_unpack_state():
    try:
        with open(_UNPACK_STATE_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def _save_unpack_state(state):
    try:
        with open(_UNPACK_STATE_PATH, 'w') as f:
            _json.dump(state, f)
    except Exception as e:
        log.warning(f'[unpack] failed to persist state: {e}')

def _torrent_has_rar(save_path, torrent_name):
    """Best-effort: check whether the torrent folder contains a rar set.
    Different Deluge setups mount paths differently; assume the container
    has visibility into /data (standard on Unraid)."""
    try:
        base = os.path.join(save_path, torrent_name)
        if os.path.isdir(base):
            for entry in os.listdir(base):
                low = entry.lower()
                if low.endswith('.rar') or re.match(r'.*\.r\d\d$', low):
                    return True
        # Some torrents are single-file at save_path with .rar
        if os.path.isdir(save_path):
            for entry in os.listdir(save_path):
                low = entry.lower()
                if torrent_name.lower() in low and (low.endswith('.rar') or re.match(r'.*\.r\d\d$', low)):
                    return True
    except Exception:
        pass
    return False

def cleanup_unpacked_torrents():
    log.info('Running unpacked-torrent removal pass...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        state = _load_unpack_state()
        now = time.time()
        removed = 0
        # First: mark newly discovered rar-torrents
        for h, info in torrents.items():
            if h in state:
                continue
            if info.get('label') == SUPERSEDED_LABEL:
                continue
            if _torrent_has_rar(info.get('save_path', ''), info.get('name', '')):
                state[h] = {'first_seen_rar_at': now, 'name': info.get('name', '')}
                log.info(f'[unpack] marking rar-torrent (aging out in {SEED_DAYS}d): {info.get("name")}')
        # Then: remove those that have aged out
        threshold = SEED_DAYS * 86400
        for h in list(state.keys()):
            if h not in torrents:
                # torrent no longer in Deluge, drop from state
                state.pop(h, None)
                continue
            age = now - state[h].get('first_seen_rar_at', now)
            if age >= threshold:
                log.info(f'[unpack] removing rar-torrent aged {age/86400:.1f}d: {state[h].get("name")}')
                remove_torrent(h)
                state.pop(h, None)
                removed += 1
        _save_unpack_state(state)
        log.info(f'Unpacked-torrent cleanup complete: removed {removed}')
    except Exception as e:
        log.error(f'Unpacked-torrent cleanup failed: {e}')


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

# ── Pushover ─────────────────────────────────────────────────────────────────

def send_pushover(title, message, url=None):
    """Fire a Pushover notification. No-op if credentials aren't set."""
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        log.info(f'[pushover] skip (no creds): {title} — {message[:80]}')
        return
    payload = {
        'token': PUSHOVER_TOKEN,
        'user':  PUSHOVER_USER,
        'title': title,
        'message': message[:1024],
    }
    if url:
        payload['url'] = url
    try:
        r = requests.post('https://api.pushover.net/1/messages.json', data=payload, timeout=10)
        if not r.ok:
            log.warning(f'[pushover] HTTP {r.status_code}: {r.text[:200]}')
    except Exception as e:
        log.warning(f'[pushover] send failed: {e}')

# ── importBlocked queue handler ─────────────────────────────────────────────
# Root cause of the dupe wall: Radarr/Sonarr grab an upgrade, download completes,
# but the import fails (ambiguous movieId, "Manual Import required", etc). The
# stuck queue entry gets cleared eventually with no import. Repeat over months
# → wall of dupes seeding in Deluge that nobody knows about.
#
# This poller: every IMPORTBLOCKED_INTERVAL, walk both /api/v3/queue endpoints
# for records with trackedDownloadState=importBlocked. If Radarr/Sonarr has
# already resolved the target (movieId or seriesId+episodeId), try ManualImport
# with the resolved candidate. Otherwise Pushover-notify. Per-downloadId dedupe
# so we don't repeat pushes.

_ib_seen = {}  # downloadId → epoch of last notify

def _ib_seen_recently(download_id, ttl_hours=24):
    now = time.time()
    for k, ts in list(_ib_seen.items()):
        if now - ts > ttl_hours * 3600:
            _ib_seen.pop(k, None)
    if download_id in _ib_seen:
        return True
    _ib_seen[download_id] = now
    return False

def _try_radarr_manual_import(record):
    download_id = record.get('downloadId') or ''
    movie_id = record.get('movieId')
    if not (download_id and movie_id):
        return False, 'no downloadId or movieId'
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/manualimport',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'downloadId': download_id, 'filterExistingFiles': 'true'},
            timeout=20,
        )
        r.raise_for_status()
        candidates = r.json() or []
    except Exception as e:
        return False, f'manualimport lookup failed: {e}'
    good = [c for c in candidates if (c.get('movie') or {}).get('id') == movie_id and c.get('rejections') in (None, [])]
    if not good:
        return False, f'no clean candidate ({len(candidates)} total, none matched movieId {movie_id})'
    files = []
    for c in good:
        files.append({
            'path': c.get('path'),
            'movieId': movie_id,
            'quality': c.get('quality'),
            'languages': c.get('languages'),
            'releaseGroup': c.get('releaseGroup'),
            'downloadId': download_id,
        })
    try:
        cmd = requests.post(
            f'{RADARR_URL}/api/v3/command',
            headers={'X-Api-Key': RADARR_API_KEY, 'Content-Type': 'application/json'},
            json={'name': 'ManualImport', 'files': files, 'importMode': 'auto'},
            timeout=20,
        )
        cmd.raise_for_status()
        return True, f'ManualImport queued for movieId={movie_id}'
    except Exception as e:
        return False, f'ManualImport POST failed: {e}'

def _try_sonarr_manual_import(record):
    download_id = record.get('downloadId') or ''
    series_id = record.get('seriesId')
    episode_id = record.get('episodeId')
    if not (download_id and series_id):
        return False, 'no downloadId or seriesId'
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/manualimport',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'downloadId': download_id, 'filterExistingFiles': 'true'},
            timeout=20,
        )
        r.raise_for_status()
        candidates = r.json() or []
    except Exception as e:
        return False, f'manualimport lookup failed: {e}'
    good = [c for c in candidates if (c.get('series') or {}).get('id') == series_id and c.get('rejections') in (None, [])]
    if not good:
        return False, f'no clean candidate ({len(candidates)} total, none matched seriesId {series_id})'
    files = []
    for c in good:
        files.append({
            'path': c.get('path'),
            'seriesId': series_id,
            'episodeIds': [e.get('id') for e in (c.get('episodes') or [])] or ([episode_id] if episode_id else []),
            'quality': c.get('quality'),
            'languages': c.get('languages'),
            'releaseGroup': c.get('releaseGroup'),
            'downloadId': download_id,
        })
    try:
        cmd = requests.post(
            f'{SONARR_URL}/api/v3/command',
            headers={'X-Api-Key': SONARR_API_KEY, 'Content-Type': 'application/json'},
            json={'name': 'ManualImport', 'files': files, 'importMode': 'auto'},
            timeout=20,
        )
        cmd.raise_for_status()
        return True, f'ManualImport queued for seriesId={series_id}'
    except Exception as e:
        return False, f'ManualImport POST failed: {e}'

def check_import_blocked():
    """Poll Radarr + Sonarr queues for importBlocked records. Auto-resolve or notify."""
    for label, url, key, resolver in [
        ('Radarr', RADARR_URL, RADARR_API_KEY, _try_radarr_manual_import),
        ('Sonarr', SONARR_URL, SONARR_API_KEY, _try_sonarr_manual_import),
    ]:
        if not key:
            continue
        try:
            r = requests.get(
                f'{url}/api/v3/queue',
                headers={'X-Api-Key': key},
                params={'pageSize': 500, 'includeUnknownMovieItems': True},
                timeout=15,
            )
            r.raise_for_status()
            records = r.json().get('records', [])
        except Exception as e:
            log.warning(f'[importBlocked] {label} queue fetch failed: {e}')
            continue
        blocked = [rec for rec in records if rec.get('trackedDownloadState') == 'importBlocked']
        for rec in blocked:
            download_id = rec.get('downloadId') or f'noid-{rec.get("id")}'
            title = rec.get('title', '<no title>')
            ok, detail = resolver(rec)
            if ok:
                log.info(f'[importBlocked] {label}: auto-imported "{title}" — {detail}')
                continue
            if _ib_seen_recently(download_id):
                continue
            msgs = rec.get('statusMessages') or []
            reason = '; '.join(m.get('messages', ['?'])[0] for m in msgs if m.get('messages')) or 'unknown'
            log.warning(f'[importBlocked] {label}: notifying — "{title}" ({detail}) — reason: {reason}')
            send_pushover(
                title=f'{label} import stuck',
                message=f'{title}\nReason: {reason}\nDetail: {detail}',
                url=f'{url}/activity/queue',
            )

def import_blocked_scheduler():
    # Small warm-up delay so we don't hammer *arr the moment the app boots.
    time.sleep(60)
    while True:
        try:
            check_import_blocked()
        except Exception as e:
            log.error(f'[importBlocked] scheduler tick failed: {e}')
        time.sleep(IMPORTBLOCKED_INTERVAL)

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

# Emergency revert for the mass-superseded-relabel bug. Flips every
# torrent currently labeled `superseded` back to `radarr` or `sonarr`
# based on which system knows about it. Idempotent; safe to hit twice.
@app.route('/revert-superseded', methods=['POST'])
def revert_superseded():
    try:
        deluge_login()
        torrents = get_all_torrents()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    reverted = 0
    skipped = 0
    for h, info in torrents.items():
        if info.get('label') != SUPERSEDED_LABEL:
            continue
        name = info.get('name', '').lower()
        # Guess the source by looking at the file extension pattern —
        # single .mkv/.mp4 is usually a Radarr movie, folders full of
        # SxxExx are Sonarr. Fall back to radarr since it's the most
        # common relabel target from the bad dedup pass.
        new_label = 'sonarr' if EPISODE_RE.search(name) else 'radarr'
        try:
            set_torrent_label(h, new_label)
            reverted += 1
        except Exception as e:
            log.warning(f'revert: failed to relabel {h}: {e}')
            skipped += 1
    return jsonify({'ok': True, 'reverted': reverted, 'skipped': skipped}), 200

# Manual trigger for the dedup pass — use to verify the fixed matcher
# behaves before waiting for the 24h scheduled tick.
@app.route('/run-dedup', methods=['POST'])
def run_dedup():
    dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes')
    threading.Thread(target=dedup_via_radarr, args=(dry_run,), daemon=True).start()
    threading.Thread(target=dedup_via_sonarr, daemon=True).start()
    return jsonify({
        'ok': True,
        'dry_run': dry_run,
        'message': f'dedup passes started{" (DRY RUN — no changes will be made)" if dry_run else ""}; check container logs',
    }), 200

# Manual trigger for cleanup_superseded — remove torrents currently
# labeled `superseded` that have been seeding at least SEED_DAYS.
@app.route('/run-cleanup', methods=['POST'])
def run_cleanup():
    threading.Thread(target=cleanup_superseded, daemon=True).start()
    return jsonify({'ok': True, 'message': f'cleanup started; will remove superseded torrents seeded ≥ {SEED_DAYS} days'}), 200

# Compare files on disk in MOVIES_LIBRARY against Radarr's tracked
# movieFile.path values. Anything on disk that Radarr isn't tracking is
# an orphan (duplicate imports, old files Radarr replaced but didn't
# delete, manual downloads that never got imported, etc).
#
# Default: dry-run — returns the list, no deletions. Pass ?delete=1 to
# actually remove. Extremely destructive; require explicit opt-in.
@app.route('/orphan-scan', methods=['POST', 'GET'])
def orphan_scan():
    delete = request.args.get('delete', '').lower() in ('1', 'true', 'yes')
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    if not os.path.isdir(MOVIES_LIBRARY):
        return jsonify({'ok': False, 'error': f'MOVIES_LIBRARY not found: {MOVIES_LIBRARY}'}), 400
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        # Collect the exact path of every file Radarr currently tracks.
        # movieFile.path may be relative to the movie folder OR absolute
        # depending on Radarr version — handle both.
        tracked_paths = set()
        for m in r.json():
            mf = m.get('movieFile') or {}
            path = mf.get('path')
            if not path:
                continue
            if not os.path.isabs(path):
                # Radarr stores movie.path (the folder) + movieFile.relativePath
                folder = m.get('path', '')
                path = os.path.join(folder, path)
            tracked_paths.add(os.path.normpath(path))
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500
    orphans = []
    video_exts = ('.mkv', '.mp4', '.avi', '.m4v', '.mov')
    for root, _, files in os.walk(MOVIES_LIBRARY):
        for f in files:
            if not f.lower().endswith(video_exts):
                continue
            full = os.path.normpath(os.path.join(root, f))
            if full in tracked_paths:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            orphans.append({'path': full, 'size': size})
    total_bytes = sum(o['size'] for o in orphans)
    deleted = 0
    if delete:
        for o in orphans:
            try:
                os.remove(o['path'])
                deleted += 1
                log.info(f'orphan-scan: removed {o["path"]}')
            except OSError as e:
                log.warning(f'orphan-scan: failed to remove {o["path"]}: {e}')
    return jsonify({
        'ok': True,
        'dry_run': not delete,
        'tracked_count': len(tracked_paths),
        'orphan_count': len(orphans),
        'orphan_total_gb': round(total_bytes / (1024**3), 2),
        'deleted': deleted,
        'orphans': orphans,
    }), 200

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
    t4 = threading.Thread(target=import_blocked_scheduler, daemon=True)
    t4.start()
    app.run(host='0.0.0.0', port=port, threaded=True)
