import os
import re
import json as _json
import time
import logging
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify

from media_share import share_bp, init_db as init_share_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Activity log (review digest) ─────────────────────────────────────────────
# Persistent record of every consequential action the automation takes, so
# the user can review what happened at /digest whenever they're ready
# (instead of a push notification they have to deal with immediately).
ACTIVITY_LOG = os.environ.get('ACTIVITY_LOG', '/data/activity-log.jsonl')
DIGEST_STATE = os.environ.get('DIGEST_STATE', '/data/digest-state.json')

def record_activity(category, summary):
    try:
        with open(ACTIVITY_LOG, 'a') as f:
            f.write(_json.dumps({
                'ts': datetime.now().isoformat(timespec='seconds'),
                'category': category,
                'summary': summary,
            }) + '\n')
    except OSError as e:
        log.debug(f'activity log write failed: {e}')

app = Flask(__name__)
app.register_blueprint(share_bp)

DELUGE_URL       = os.environ.get('DELUGE_URL', 'http://10.0.0.2:8112')
DELUGE_PASSWORD  = os.environ.get('DELUGE_PASSWORD', 'PASSWORDHERE')
SONARR_URL       = os.environ.get('SONARR_URL', 'http://10.0.0.8:8989')
SONARR_API_KEY   = os.environ.get('SONARR_API_KEY', '')
RADARR_URL       = os.environ.get('RADARR_URL', 'http://10.0.0.7:7878')
RADARR_API_KEY   = os.environ.get('RADARR_API_KEY', '')
SUPERSEDED_LABEL  = 'superseded'
LIBRARY_SEED_LABEL = 'library-seed'
SONARR_UPG_LABEL  = os.environ.get('SONARR_UPGRADE_LABEL', 'sonarr-upgrade')
RADARR_UPG_LABEL  = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')
# Rolling cutoff: Radarr grabs for movies released more than N years ago
# get throttled into the -upgrade lane even on first fetch. Ages
# automatically.
OLD_GAP_YEARS     = int(os.environ.get('OLD_GAP_YEARS', '10'))
SEEDING_DIR      = os.environ.get('SEEDING_DIR', '/data/Downloads/Just4Seeding')
SEED_DAYS        = int(os.environ.get('SEED_DAYS', '21'))
# Root of the movies library from the CONTAINER's perspective — used by
# /orphan-scan. Radarr may report paths from the host's perspective, so
# matching is done by filename basename rather than absolute path.
MOVIES_LIBRARY   = os.environ.get('MOVIES_LIBRARY', '/media/movies')
PLEX_URL         = os.environ.get('PLEX_URL', 'http://10.0.0.6:32400')
PLEX_TOKEN       = os.environ.get('PLEX_TOKEN', '')
# Translate Plex-container paths → arr-webhook-container paths for
# filesystem deletes. Comma-separated `plex_prefix:local_prefix` pairs.
# Default handles the standard split-mount setup: Plex sees /data/...,
# arr-webhook has /media/... mounts for the same shares.
PLEX_PATH_MAP = [
    tuple(pair.split(':', 1))
    for pair in os.environ.get(
        'PLEX_PATH_MAP',
        '/data/Movies:/media/movies,/data/TV Shows:/media/tv',
    ).split(',')
    if ':' in pair
]

def _translate_plex_path(p):
    if not p:
        return p
    for plex_prefix, local_prefix in PLEX_PATH_MAP:
        # Require a directory boundary so a short prefix like `/data/M`
        # doesn't accidentally match `/data/Movies` AND `/data/Music`.
        prefix = plex_prefix.rstrip('/')
        if p == prefix or p.startswith(prefix + '/'):
            return local_prefix.rstrip('/') + p[len(prefix):]
    return p
# Comma-separated library titles to skip in plex-dupe-scan (case-insensitive).
PLEX_SKIP_LIBRARIES = {s.strip().lower() for s in os.environ.get('PLEX_SKIP_LIBRARIES', 'Adult,XXX,NSFW,Music,Music Videos').split(',') if s.strip()}
# Plex ratingKeys never touched by /plex-dupe-fix. Env var is the seed
# ("Melody keeps multiple Eras Tour versions"); runtime additions go
# into /data/plex_dupe_keep.json via the /plex-dupe-keep endpoint so
# updates don't require a rebuild.
PLEX_DUPE_KEEP = {s.strip() for s in os.environ.get('PLEX_DUPE_KEEP', '25705').split(',') if s.strip()}
PLEX_DUPE_KEEP_PATH = os.environ.get('PLEX_DUPE_KEEP_PATH', '/data/plex_dupe_keep.json')

def _load_plex_dupe_keep():
    """Merge the env-var seed with any runtime entries in the JSON file."""
    keys = set(PLEX_DUPE_KEEP)
    try:
        if os.path.exists(PLEX_DUPE_KEEP_PATH):
            with open(PLEX_DUPE_KEEP_PATH) as f:
                data = _json.load(f)
                for entry in data.get('entries', []):
                    if entry.get('plex_key'):
                        keys.add(str(entry['plex_key']))
    except Exception as e:
        log.warning(f'plex-dupe-keep load failed: {e}')
    return keys

def _save_plex_dupe_keep(entries):
    try:
        os.makedirs(os.path.dirname(PLEX_DUPE_KEEP_PATH), exist_ok=True)
        with open(PLEX_DUPE_KEEP_PATH, 'w') as f:
            _json.dump({'entries': entries}, f, indent=2)
    except Exception as e:
        log.error(f'plex-dupe-keep save failed: {e}')
        raise
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

_created_labels_cache = set()
_created_labels_lock = threading.Lock()

def _ensure_deluge_label(label):
    """Deluge silently rejects set-label calls for labels that don't
    exist yet. Auto-create on first use. Serialized with a lock so
    concurrent grab webhooks don't race on the cache check."""
    if not label:
        return
    with _created_labels_lock:
        if label in _created_labels_cache:
            return
        try:
            resp = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'label.get_labels', 'params': [], 'id': 40},
                timeout=10,
            )
            resp.raise_for_status()
            existing = set(resp.json().get('result') or [])
            _created_labels_cache.update(existing)
            if label not in existing:
                session.post(
                    f'{DELUGE_URL}/json',
                    json={'method': 'label.add', 'params': [label], 'id': 41},
                    timeout=10,
                ).raise_for_status()
                _created_labels_cache.add(label)
                log.info(f'Auto-created Deluge label: {label}')
        except Exception as e:
            log.warning(f'ensure_label({label}) failed: {e}')

def set_torrent_label(torrent_hash, label):
    _ensure_deluge_label(label)
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
    name_words = set(re.findall(r'[a-z0-9]+', torrent_name.lower().replace("'", "")))
    for variant in title_variants:
        v_words = re.findall(r'[a-z0-9]+', variant.lower().replace("'", ""))
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
        if removed:
            record_activity('cleanup', f'Removed {removed} superseded torrent(s) past the {SEED_DAYS}-day seed window')
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
        if relabeled and not dry_run:
            record_activity('dedup', f'Radarr dedup: relabeled {relabeled} duplicate torrent(s) as superseded')
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
        if relabeled:
            record_activity('dedup', f'Sonarr dedup: relabeled {relabeled} duplicate torrent(s) as superseded')
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
        if removed:
            record_activity('cleanup', f'Removed {removed} unpacked RAR torrent(s) past the 21-day window')
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
        if removed:
            record_activity('dedup', f'Radarr queue: removed {removed} duplicate queue entr(y/ies)')
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
            # Radarr and Sonarr name the "include unknown" queue param differently;
            # each ignores the other's, so send both.
            r = requests.get(
                f'{url}/api/v3/queue',
                headers={'X-Api-Key': key},
                params={
                    'pageSize': 500,
                    'includeUnknownMovieItems': True,
                    'includeUnknownSeriesItems': True,
                },
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
                record_activity('import-rescue', f'{label}: auto-imported stuck download "{title}"')
                continue
            if _ib_seen_recently(download_id):
                continue
            msgs = rec.get('statusMessages') or []
            reason = '; '.join(m.get('messages', ['?'])[0] for m in msgs if m.get('messages')) or 'unknown'
            log.warning(f'[importBlocked] {label}: notifying — "{title}" ({detail}) — reason: {reason}')
            record_activity('import-stuck', f'{label}: import stuck, needs manual look — "{title}" ({reason})')
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

def relabel_download_to_base(download_id, source):
    """Flip a finished download's Deluge label from the '-upgrade' variant back
    to the base one, keyed on the torrent hash (downloadId) directly.

    This deliberately does NOT depend on the title/filename matching the
    supersede step needs — the tag transition is what the user watches, and it
    must fire whenever the completed torrent is present, even if its name has
    punctuation, it's a season pack, or the supersede matcher can't pin the
    "new" torrent. Uses the env-configurable label so a customised
    SONARR_UPGRADE_LABEL / RADARR_UPGRADE_LABEL stays consistent with the grab
    throttle that applied it.

    Returns (result, name):
      'flipped'   — was upgrade-labeled, now base
      'already'   — some other/base label already, nothing to do
      'not_found' — Deluge has no torrent under this hash
    """
    base_label = 'radarr' if source == 'Radarr' else 'sonarr'
    upgrade_label = (SONARR_UPG_LABEL if source == 'Sonarr' else RADARR_UPG_LABEL).lower()
    status = session.post(
        f'{DELUGE_URL}/json',
        json={
            'method': 'core.get_torrent_status',
            'params': [download_id, ['label', 'name']],
            'id': 93,
        },
        timeout=10,
    ).json().get('result') or {}
    if not status:
        return 'not_found', None
    name = status.get('name', download_id)
    current = (status.get('label') or '').lower()
    if current != upgrade_label:
        log.info(f'{source}: {download_id} ({name}) label is "{current or "(none)"}", '
                 f'not "{upgrade_label}" — no flip needed')
        return 'already', name
    set_torrent_label(download_id, base_label)
    log.info(f'{source}: relabeled {download_id} ({name}) {upgrade_label} → {base_label}')
    return 'flipped', name


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

    download_id = (data.get('downloadId') or '').lower()
    try:
        deluge_login()
        ensure_label_exists()
        # Flip the '-upgrade' tag back to the base label FIRST, keyed on
        # downloadId — independent of the title/filename matching the supersede
        # step below needs. This flip used to live *after* the "can't identify
        # new torrent" abort, so any match miss (special characters, season
        # packs, or a downloadId/infohash mismatch) aborted before it and left
        # the finished torrent silently stuck wearing '-upgrade'.
        flip_result = 'not_found'
        if download_id:
            flip_result, flip_name = relabel_download_to_base(download_id, source)
            if flip_result == 'flipped':
                record_activity('relabel', f'{source}: "{flip_name}" upgrade → base after import')
        torrents = get_all_torrents()
    except Exception as e:
        log.error(f'Deluge connection failed: {e}')
        return

    # Identify the new torrent to skip it
    # First try downloadId direct hash lookup (most accurate)
    new_torrent_hash = None
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

    if not new_torrent_hash:
        log.warning(f'{source}: aborting supersede — cannot identify new torrent, '
                    f'risk superseding the wrong one')
        # If we also couldn't flip the tag (no torrent under this downloadId),
        # the finished upgrade is stuck wearing '-upgrade' with no automatic
        # recovery — make it loud instead of silent.
        if flip_result == 'not_found':
            upgrade_label = SONARR_UPG_LABEL if source == 'Sonarr' else RADARR_UPG_LABEL
            log.error(f'{source}: STUCK upgrade tag — "{series_title}" {search_term}: no torrent '
                      f'under downloadId "{download_id or "(none)"}", tag not flipped and old copy '
                      f'not superseded')
            record_activity('relabel-stuck',
                            f'{source}: "{series_title}" {search_term} may be stuck as {upgrade_label}')
            send_pushover(
                title=f'{source}: upgrade tag may be stuck',
                message=(f'{series_title} {search_term}\n'
                         f'Import completed but no Deluge torrent matched downloadId '
                         f'"{download_id or "(none)"}", so the "{upgrade_label}" tag could not be '
                         f'flipped back. Check Deluge for a torrent still tagged {upgrade_label}.'),
            )
        return

    log.info(f'{source}: will skip new torrent {new_torrent_hash}')
    # Fallback flip: if the new torrent was pinned by filename/season (empty or
    # mismatched downloadId) the early downloadId flip was a no-op — flip the
    # matched hash now. Idempotent with the early attempt.
    if flip_result != 'flipped':
        try:
            relabel_download_to_base(new_torrent_hash, source)
        except Exception as e:
            log.warning(f'{source}: post-import relabel failed for {new_torrent_hash}: {e}')

    # Find and supersede old torrents. Sweeps everything that matches
    # the title+search_term regardless of label (radarr, sonarr,
    # library-seed, blank) — the point of an upgrade is to replace the
    # old file wherever it came from. Already-superseded is the one
    # skip because it's already in the flow.
    for torrent_hash, info in torrents.items():
        if torrent_hash == new_torrent_hash:
            continue
        if info.get('label') == SUPERSEDED_LABEL:
            continue
        name = info.get('name', '')
        if torrent_matches_any_title(name, title_variants) and search_term.lower() in name.lower():
            if proper_repack:
                log.info(f'{source}: immediately deleting {torrent_hash} - {name} (proper/repack)')
                record_activity('supersede', f'{source}: deleted "{name}" (replaced by PROPER/REPACK)')
                remove_torrent(torrent_hash)
            else:
                log.info(f'{source}: superseding {torrent_hash} - {name}')
                record_activity('supersede', f'{source}: superseded "{name}" after upgrade import')
                set_torrent_label(torrent_hash, SUPERSEDED_LABEL)
                move_torrent_storage(torrent_hash, SEEDING_DIR)


def handle_import_relabel(data, source):
    """Download event that is NOT an upgrade: the finished torrent can still
    be wearing an -upgrade label — Radarr's old-gap lane and the monthly
    sweep both throttle no-file gap-fills as radarr-upgrade, and those
    imports arrive with isUpgrade=false so handle_upgrade_import never sees
    them. Flip the label back to the base one here, mirroring the
    post-import relabel in handle_upgrade_import."""
    download_id = (data.get('downloadId') or '').lower()
    if not download_id:
        return
    try:
        deluge_login()
        result, name = relabel_download_to_base(download_id, source)
        if result == 'flipped':
            record_activity('relabel', f'{source}: "{name}" upgrade → base after gap-fill import')
    except Exception as e:
        log.warning(f'{source}: non-upgrade post-import relabel failed for {download_id}: {e}')


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
    """
    Return a reason string if this grab should be throttled:
      - 'upgrade' — movie already has a file
      - 'old_gap' — no file, but release year is older than the rolling cutoff
    Returns None otherwise.
    """
    try:
        movie_id = data.get('movie', {}).get('id')
        if not movie_id:
            return None
        r = requests.get(
            f"{RADARR_URL}/api/v3/movie/{movie_id}",
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        movie = r.json()
        if movie.get('hasFile'):
            return 'upgrade'
        year = movie.get('year')
        if year and year < (datetime.now().year - OLD_GAP_YEARS):
            return 'old_gap'
        return None
    except Exception as e:
        log.error(f"Radarr upgrade check failed: {e}")
        return None

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
        # Revive-superseded: if this hash is already in Deluge as
        # `superseded` AND fully downloaded, Radarr/Sonarr is re-grabbing a
        # file we already have on disk. Instead of throttling it as an
        # upgrade, flip it back to the active label + kick a rescan so the
        # arr re-imports for free (no bandwidth). The currently-active
        # torrent's Download webhook path handles supersede-on-import.
        existing = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrent_status',
                'params': [download_id, ['label', 'progress', 'save_path']],
                'id': 91,
            },
            timeout=10,
        ).json().get('result') or {}
        existing_label = existing.get('label')
        existing_progress = existing.get('progress') or 0
        existing_save_path = existing.get('save_path') or ''
        # In-flight case: same hash already in Deluge, still downloading, and
        # already labeled as an -upgrade. Means Radarr/Sonarr re-fired the
        # Grab webhook for a throttled download that was already in progress
        # (rare but seen). Sanity-check label + save_path, then bail — nothing
        # else to do since the download is already running.
        if existing_label in (SONARR_UPG_LABEL, RADARR_UPG_LABEL) and existing_progress < 99.0:
            downloads_root = os.environ.get('DOWNLOADS_MOUNT', '/data/Downloads')
            if existing_label != upgrade_label:
                log.warning(
                    f"{source}: in-flight grab {download_id} has wrong upgrade label "
                    f"'{existing_label}' (expected '{upgrade_label}') — correcting"
                )
                ensure_label_exists_named(upgrade_label)
                set_torrent_label(download_id, upgrade_label)
            else:
                log.info(f"{source}: in-flight grab {download_id} already labeled correctly")
            if existing_save_path and not existing_save_path.startswith(downloads_root):
                log.error(
                    f"{source}: in-flight grab {download_id} has save_path "
                    f"'{existing_save_path}' outside {downloads_root} — investigate"
                )
            return
        if existing_label == SUPERSEDED_LABEL and existing_progress >= 99.0:
            base_label = 'sonarr' if source == 'Sonarr' else 'radarr'
            log.warning(
                f"{source}: grab {download_id} matches already-superseded complete torrent — "
                f"reviving with label '{base_label}' and firing rescan"
            )
            ensure_label_exists_named(base_label)
            set_torrent_label(download_id, base_label)
            try:
                if source == 'Radarr':
                    scan_cmd, url, key = 'DownloadedMoviesScan', RADARR_URL, RADARR_API_KEY
                else:
                    scan_cmd, url, key = 'DownloadedEpisodesScan', SONARR_URL, SONARR_API_KEY
                # NOTE: save_path is the Deluge container's view. Radarr/
                # Sonarr must have the same path mounted the same way for
                # the scan to find the file. Our stack has matching mounts
                # (/data/Downloads/... everywhere) so this holds — but if
                # that ever diverges the scan will silently no-op.
                save_path = existing.get('save_path') or ''
                if save_path and key:
                    requests.post(
                        f'{url}/api/v3/command',
                        headers={'X-Api-Key': key},
                        json={'name': scan_cmd, 'path': save_path, 'downloadClientId': download_id},
                        timeout=15,
                    )
                    log.info(f"{source}: fired {scan_cmd} on {save_path}")
            except Exception as e:
                log.error(f"{source}: revive rescan failed: {e}")
            return
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
            log.info('Waiting 5 minutes before relabeling upgrades...')
            time.sleep(300)  # 5 minutes
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
    elif event == 'Download':
        if data.get('isUpgrade'):
            handle_upgrade_import(data, 'Radarr')
        else:
            # Gap-fill imports arrive with isUpgrade=false but may wear an
            # -upgrade label from the grab throttle — flip it back.
            threading.Thread(target=handle_import_relabel, args=(data, 'Radarr'), daemon=True).start()
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook/sonarr', methods=['POST'])
def sonarr_webhook():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get('eventType', '')
    log.info(f'Sonarr event: {event} | isUpgrade: {data.get("isUpgrade")} | downloadId: {data.get("downloadId")}')
    if event == 'Grab':
        threading.Thread(target=handle_grab, args=(data, 'Sonarr'), daemon=True).start()
    elif event == 'Download':
        if data.get('isUpgrade'):
            handle_upgrade_import(data, 'Sonarr')
        else:
            # Gap-fill imports arrive with isUpgrade=false but may wear an
            # -upgrade label from the grab throttle — flip it back.
            threading.Thread(target=handle_import_relabel, args=(data, 'Sonarr'), daemon=True).start()
    return jsonify({'status': 'ok'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200


# ── Review digest ─────────────────────────────────────────────────────────────
# Running tracker of everything the automation has done, reviewable whenever
# the user is ready (deliberately NOT a push notification). GET /digest shows
# activity since the last review; POST /digest/reviewed marks it read.

def _digest_state():
    try:
        with open(DIGEST_STATE) as f:
            return _json.load(f)
    except (OSError, ValueError):
        return {}

def _digest_entries(since_iso=None):
    entries = []
    try:
        with open(ACTIVITY_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except ValueError:
                    continue
                if since_iso and e.get('ts', '') <= since_iso:
                    continue
                entries.append(e)
    except OSError:
        pass
    return entries

@app.route('/digest', methods=['GET'])
def digest():
    state = _digest_state()
    since = state.get('last_reviewed_at')
    show_all = request.args.get('all', '').lower() in ('1', 'true', 'yes')
    entries = _digest_entries(None if show_all else since)
    entries.reverse()  # newest first
    if request.args.get('format') == 'json':
        return jsonify({'ok': True, 'since': since, 'count': len(entries), 'entries': entries})

    counts = {}
    for e in entries:
        counts[e.get('category', '?')] = counts.get(e.get('category', '?'), 0) + 1
    summary = ' · '.join(f'{v} {k}' for k, v in sorted(counts.items())) or 'nothing new'
    rows = ''.join(
        f"<tr><td class='ts'>{e.get('ts','')}</td>"
        f"<td class='cat cat-{e.get('category','')}'>{e.get('category','')}</td>"
        f"<td>{e.get('summary','').replace('<','&lt;')}</td></tr>"
        for e in entries
    )
    return f'''<!doctype html><html><head><title>plex-automation digest</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ background:#0e1216; color:#cdd6e0; font: 14px/1.5 -apple-system, sans-serif; margin: 2rem auto; max-width: 900px; padding: 0 1rem; }}
h1 {{ font-size: 1.3rem; }} .sub {{ color:#7a8794; margin-bottom: 1rem; }}
table {{ width:100%; border-collapse: collapse; }}
td {{ padding: 4px 8px; border-bottom: 1px solid #1c2530; vertical-align: top; }}
.ts {{ color:#7a8794; white-space: nowrap; }}
.cat {{ white-space: nowrap; font-weight: 600; }}
.cat-import-stuck {{ color:#ff6b6b; }} .cat-import-rescue {{ color:#ffd166; }}
.cat-supersede {{ color:#66d9ef; }} .cat-cleanup {{ color:#a9dc76; }}
.cat-dedup {{ color:#c39ac9; }} .cat-relabel {{ color:#78dce8; }}
button {{ background:#2563eb; color:#fff; border:0; border-radius:6px; padding:8px 14px; font-size:14px; cursor:pointer; }}
a {{ color:#78dce8; }}
</style></head><body>
<h1>plex-automation — activity since last review</h1>
<div class="sub">{len(entries)} item(s): {summary}
{f" · reviewed up to {since}" if since and not show_all else ""}
· <a href="/digest?all=1">show everything</a></div>
<form method="post" action="/digest/reviewed"><button type="submit">Mark all reviewed</button></form>
<table>{rows or "<tr><td>Nothing to review 🎉</td></tr>"}</table>
</body></html>'''

@app.route('/digest/reviewed', methods=['POST'])
def digest_reviewed():
    state = _digest_state()
    state['last_reviewed_at'] = datetime.now().isoformat(timespec='seconds')
    try:
        with open(DIGEST_STATE, 'w') as f:
            _json.dump(state, f)
    except OSError as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    if request.headers.get('Accept', '').startswith('application/json'):
        return jsonify({'ok': True, 'last_reviewed_at': state['last_reviewed_at']})
    return '<meta http-equiv="refresh" content="0;url=/digest">', 200

# Debug: look up torrents in Deluge whose name contains ?name=<substr>
# and report save_path + files. Useful when a Radarr import fails with
# "no video files found" and you need to see what Deluge thinks it
# downloaded and where.
# Radarr expects completed downloads to live at
# <save_path>/<torrent_name>/... but Deluge saves single-file torrents
# bare at <save_path>/<name>.mkv. Radarr then tries to scan the file
# as if it were a directory and fails with "no video files found".
#
# This endpoint walks every Radarr/Sonarr-labeled Deluge torrent, and
# for any single-file bare torrent, creates <save_path>/<name-stem>/
# and hardlinks the file into it. Deluge keeps seeding the original
# untouched; Radarr's next import scan now finds the file where it
# expects. Dry-run by default; pass ?apply=1 to actually fix.
@app.route('/fix-bare-torrents', methods=['POST'])
def fix_bare_torrents():
    apply_ = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    # Downloads dir inside this container — should match wherever Deluge
    # writes to on the host, seen through this container's bind mount.
    downloads_root = os.environ.get('DOWNLOADS_MOUNT', '/data/Downloads')
    if not os.path.isdir(downloads_root):
        return jsonify({'ok': False, 'error': f'DOWNLOADS_MOUNT not a dir: {downloads_root}'}), 400
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'save_path', 'files', 'label', 'progress']],
                'id': 77,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    fixed = []
    skipped = []
    errors = []
    for h, info in (resp.json().get('result') or {}).items():
        label = (info.get('label') or '')
        if label not in ('radarr', 'radarr-upgrade', 'sonarr', 'sonarr-upgrade'):
            continue
        if (info.get('progress') or 0) < 100:
            continue
        files = info.get('files') or []
        if len(files) != 1:
            continue
        file_rel = files[0].get('path') if isinstance(files[0], dict) else str(files[0])
        if not file_rel or '/' in file_rel:  # already inside a subfolder
            continue
        deluge_save = info.get('save_path') or ''
        # Translate Deluge's host path to our container's mount. Simplest
        # assumption: Deluge's save_path suffix after /Downloads/ matches
        # ours. So map /data/Downloads/Complete → downloads_root + suffix.
        # Since our container mounts the same /data/Downloads, the paths
        # already match — no translation needed.
        src = os.path.join(deluge_save, file_rel)
        if not os.path.isfile(src):
            errors.append({'hash': h, 'reason': f'source not found: {src}'})
            continue
        stem = file_rel.rsplit('.', 1)[0]  # drop extension
        target_dir = os.path.join(deluge_save, stem)
        target = os.path.join(target_dir, file_rel)
        if os.path.exists(target):
            skipped.append({'hash': h, 'reason': 'already wrapped', 'target': target})
            continue
        if not apply_:
            fixed.append({'hash': h, 'name': info.get('name'), 'action': 'would wrap', 'target': target})
            continue
        try:
            os.makedirs(target_dir, exist_ok=True)
            os.link(src, target)
            fixed.append({'hash': h, 'name': info.get('name'), 'action': 'wrapped', 'target': target})
            log.info(f'fix-bare-torrents: hardlinked {src} → {target}')
        except OSError as e:
            errors.append({'hash': h, 'reason': str(e), 'target': target})
    return jsonify({
        'ok': True,
        'dry_run': not apply_,
        'fixed_count': len(fixed),
        'skipped_count': len(skipped),
        'error_count': len(errors),
        'fixed': fixed,
        'skipped': skipped,
        'errors': errors,
    }), 200

# Nuclear cleanup: remove every Deluge torrent that either (a) has
# incomplete data (progress < 100 or files missing) or (b) is NOT
# labeled radarr/radarr-upgrade. Use before a full re-grab pass so
# Radarr's search starts from a clean slate.
#
# Dry-run by default — hit with ?apply=1 to actually delete.
@app.route('/purge-non-radarr', methods=['POST'])
def purge_non_radarr():
    apply_ = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    keep_labels = {
        s.strip().lower() for s in
        request.args.get('keep', 'radarr,radarr-upgrade,superseded').split(',')
        if s.strip()
    }
    # A torrent is treated as "broken" when its progress is below this
    # threshold. Default 100 (anything not fully downloaded). Pass
    # ?broken_below=1 to only sweep the truly-never-started zeros while
    # leaving partial downloads alone.
    try:
        broken_below = float(request.args.get('broken_below', '100'))
    except ValueError:
        broken_below = 100.0
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'progress', 'state', 'total_size']],
                'id': 55,
            },
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    to_purge = []
    keep_healthy = []
    for h, info in (resp.json().get('result') or {}).items():
        label = (info.get('label') or '').lower()
        progress = info.get('progress') or 0
        broken = progress < broken_below
        wrong_label = label not in keep_labels
        if broken or wrong_label:
            to_purge.append({
                'hash': h,
                'name': info.get('name'),
                'label': info.get('label'),
                'progress': progress,
                'reason': 'broken' if broken else 'wrong_label',
                'size_gb': round((info.get('total_size') or 0) / (1024**3), 2),
            })
        else:
            keep_healthy.append(h)

    removed = 0
    errors = []
    if apply_:
        # Remove with data. Batch in chunks so we don't hammer Deluge.
        BATCH = 25
        hashes = [t['hash'] for t in to_purge]
        for i in range(0, len(hashes), BATCH):
            chunk = hashes[i:i + BATCH]
            for h in chunk:
                try:
                    session.post(
                        f'{DELUGE_URL}/json',
                        json={'method': 'core.remove_torrent', 'params': [h, True], 'id': 66},
                        timeout=15,
                    ).raise_for_status()
                    removed += 1
                except Exception as e:
                    errors.append({'hash': h, 'error': str(e)})
        log.info(f'purge-non-radarr: removed {removed}, {len(errors)} errors')

    return jsonify({
        'ok': True,
        'dry_run': not apply_,
        'keep_labels': sorted(keep_labels),
        'purge_count': len(to_purge),
        'keep_count': len(keep_healthy),
        'removed': removed,
        'error_count': len(errors),
        'purge_sample': to_purge[:20],  # first 20 for preview; full list too big to dump
        'purge_total_gb': round(sum(t['size_gb'] for t in to_purge), 2),
    }), 200

# Radarr-side inspection: shows movie state (hasFile, tracked path,
# monitored, queue entries, recent import/grab history) for anything
# matching ?name=<substr>. Complement to /deluge-lookup.
@app.route('/radarr-lookup', methods=['GET'])
def radarr_lookup():
    q = (request.args.get('name') or '').lower()
    if not q:
        return jsonify({'ok': False, 'error': 'name query param required'}), 400
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=20,
        )
        r.raise_for_status()
        movies = [m for m in r.json() if q in (m.get('title') or '').lower()]
    except Exception as e:
        return jsonify({'ok': False, 'error': f'movie lookup failed: {e}'}), 500
    out = []
    for m in movies:
        mid = m.get('id')
        entry = {
            'id': mid,
            'title': m.get('title'),
            'year': m.get('year'),
            'monitored': m.get('monitored'),
            'hasFile': m.get('hasFile'),
            'tracked_file': (m.get('movieFile') or {}).get('relativePath'),
            'path': m.get('path'),
        }
        # Recent history: what has Radarr done with this movie
        try:
            h = requests.get(
                f'{RADARR_URL}/api/v3/history/movie',
                headers={'X-Api-Key': RADARR_API_KEY},
                params={'movieId': mid},
                timeout=15,
            )
            h.raise_for_status()
            events = h.json()
            events.sort(key=lambda e: e.get('date', ''), reverse=True)
            entry['history'] = [
                {
                    'date': e.get('date'),
                    'event': e.get('eventType'),
                    'source_title': e.get('sourceTitle'),
                    'download_id': e.get('downloadId'),
                }
                for e in events[:10]
            ]
        except Exception as e:
            entry['history_error'] = str(e)
        # Queue entries
        try:
            q_resp = requests.get(
                f'{RADARR_URL}/api/v3/queue',
                headers={'X-Api-Key': RADARR_API_KEY},
                params={'pageSize': 500, 'movieId': mid},
                timeout=15,
            )
            q_resp.raise_for_status()
            entry['queue'] = [
                {
                    'title': rec.get('title'),
                    'status': rec.get('status'),
                    'trackedDownloadState': rec.get('trackedDownloadState'),
                    'errorMessage': rec.get('errorMessage'),
                    'protocol': rec.get('protocol'),
                }
                for rec in (q_resp.json().get('records') or [])
                if rec.get('movieId') == mid
            ]
        except Exception as e:
            entry['queue_error'] = str(e)
        out.append(entry)
    return jsonify({'ok': True, 'count': len(out), 'movies': out}), 200

# Find Radarr-tracked movie files that DON'T have a matching torrent in
# Deluge. These are movies you have on disk but aren't currently seeding
# — either older imports from before the arr setup, or files where the
# torrent was removed. Useful for deciding what to re-search / re-grab.
@app.route('/no-seed-check', methods=['GET'])
def no_seed_check():
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'files']],
                'id': 44,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Deluge fetch failed: {e}'}), 500
    seed_basenames = set()
    for h, info in (resp.json().get('result') or {}).items():
        n = info.get('name')
        if n:
            seed_basenames.add(os.path.basename(n))
        for f in (info.get('files') or []):
            p = f.get('path') if isinstance(f, dict) else str(f)
            if p:
                seed_basenames.add(os.path.basename(p))
    unseeded = []
    for m in r.json():
        if not m.get('hasFile'):
            continue
        mf = m.get('movieFile') or {}
        rel = mf.get('relativePath') or (mf.get('path') or '')
        base = os.path.basename(rel) if rel else ''
        if not base:
            continue
        if base in seed_basenames:
            continue
        unseeded.append({
            'id': m.get('id'),
            'title': m.get('title'),
            'year': m.get('year'),
            'tracked_file': base,
            'size_gb': round((mf.get('size') or 0) / (1024**3), 2),
        })
    total_gb = round(sum(u['size_gb'] for u in unseeded), 2)
    return jsonify({
        'ok': True,
        'radarr_with_file': sum(1 for m in r.json() if m.get('hasFile')),
        'seeding_files': len(seed_basenames),
        'unseeded_count': len(unseeded),
        'unseeded_total_gb': total_gb,
        'unseeded': unseeded,
    }), 200

# Delete a curated list of paths under the movies library. POST body is
# newline-separated paths (or JSON {"paths":[...]}). Any path outside
# MOVIES_LIBRARY is rejected — safety guard against typos. Dry-run by
# default; ?apply=1 to actually remove.
@app.route('/delete-paths', methods=['POST'])
def delete_paths():
    apply_ = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    body = request.get_data(as_text=True) or ''
    paths = []
    body_stripped = body.strip()
    if body_stripped.startswith('{'):
        try:
            data = _json.loads(body_stripped)
            paths = data.get('paths', [])
        except Exception as e:
            return jsonify({'ok': False, 'error': f'JSON parse: {e}'}), 400
    else:
        paths = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith('#')]
    if not paths:
        return jsonify({'ok': False, 'error': 'no paths provided in body'}), 400

    lib_root = os.path.realpath(MOVIES_LIBRARY)
    results = {'deleted': [], 'skipped': [], 'errors': []}
    for p in paths:
        real = os.path.realpath(p)
        if not real.startswith(lib_root + os.sep) and real != lib_root:
            results['errors'].append({'path': p, 'reason': f'outside MOVIES_LIBRARY ({lib_root})'})
            continue
        if not os.path.isfile(real):
            results['skipped'].append({'path': p, 'reason': 'not a file / already gone'})
            continue
        if not apply_:
            results['deleted'].append({'path': p, 'action': 'would delete'})
            continue
        try:
            os.remove(real)
            results['deleted'].append({'path': p, 'action': 'deleted'})
            log.info(f'delete-paths: removed {p}')
        except OSError as e:
            results['errors'].append({'path': p, 'reason': str(e)})
    return jsonify({
        'ok': True,
        'dry_run': not apply_,
        'received': len(paths),
        'deleted_count': len(results['deleted']),
        'skipped_count': len(results['skipped']),
        'error_count': len(results['errors']),
        'results': results,
    }), 200

@app.route('/deluge-lookup', methods=['GET', 'POST'])
def deluge_lookup():
    q = (request.args.get('name') or '').lower()
    if not q:
        return jsonify({'ok': False, 'error': 'name query param required'}), 400
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'save_path', 'files', 'label', 'progress', 'state']],
                'id': 99,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    hits = []
    for h, info in (resp.json().get('result') or {}).items():
        if q in (info.get('name') or '').lower():
            hits.append({
                'hash': h,
                'name': info.get('name'),
                'label': info.get('label'),
                'save_path': info.get('save_path'),
                'state': info.get('state'),
                'progress': info.get('progress'),
                'files': [f.get('path') if isinstance(f, dict) else str(f) for f in (info.get('files') or [])],
            })
    return jsonify({'ok': True, 'count': len(hits), 'hits': hits}), 200

AUTO_RESCUE_MAX_ATTEMPTS = 6  # ~90 min at 15-min intervals
_rescue_attempts = {}  # hash → attempt count

# Detect "grabbed but never imported" torrents and fire a scan to
# recover them. Radarr/Sonarr sometimes silently drop imports (folder
# name mismatch, transient path glitch, etc). This walks Deluge for
# completed radarr/sonarr-labeled torrents, cross-references the
# download_id against the *arr history, and if a `grabbed` event has
# no matching downloadFolderImported / downloadFolderImported after
# it, we call DownloadedMoviesScan / DownloadedEpisodesScan against
# the save_path so Radarr/Sonarr re-attempts the import.
def _auto_rescue(service, dry_run=False):
    if service == 'radarr':
        url, key = RADARR_URL, RADARR_API_KEY
        labels = {'radarr', 'radarr-upgrade'}
        history_path = '/api/v3/history'
        scan_cmd = 'DownloadedMoviesScan'
        import_event = 'downloadFolderImported'
    elif service == 'sonarr':
        url, key = SONARR_URL, SONARR_API_KEY
        labels = {'sonarr', 'sonarr-upgrade'}
        history_path = '/api/v3/history'
        scan_cmd = 'DownloadedEpisodesScan'
        import_event = 'downloadFolderImported'
    else:
        return {'ok': False, 'error': f'unknown service {service}'}
    if not key:
        return {'ok': False, 'error': f'no {service.upper()}_API_KEY'}
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'progress']],
                'id': 61,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return {'ok': False, 'error': f'deluge fetch failed: {e}'}
    stuck = []
    triggered = []
    skipped_reasons = {'not_labeled': 0, 'not_complete': 0, 'no_grab': 0, 'already_imported': 0, 'max_attempts': 0}
    active_hashes = set()
    for h, info in torrents.items():
        if (info.get('label') or '').lower() not in labels:
            skipped_reasons['not_labeled'] += 1
            continue
        if (info.get('progress') or 0) < 99.0:
            skipped_reasons['not_complete'] += 1
            continue
        try:
            hr = requests.get(
                f'{url}{history_path}',
                headers={'X-Api-Key': key},
                params={'downloadId': h.upper(), 'pageSize': 50},
                timeout=20,
            )
            hr.raise_for_status()
            records = hr.json().get('records') or []
        except Exception as e:
            log.warning(f'auto-rescue: history lookup for {h[:8]} failed: {e}')
            continue
        grabbed_dates = [r.get('date') for r in records if r.get('eventType') == 'grabbed']
        imported_dates = [r.get('date') for r in records if r.get('eventType') == import_event]
        if not grabbed_dates:
            skipped_reasons['no_grab'] += 1
            continue
        last_grab = max(grabbed_dates)
        last_import = max(imported_dates) if imported_dates else ''
        # Manual DownloadedMoviesScan strips the downloadId from the
        # resulting downloadFolderImported event, so history?downloadId
        # can miss legit imports. Fall back to matching by sourceTitle
        # substring against the torrent name — cross-references any
        # import in the movie's full history whose sourceTitle looks
        # like this torrent.
        if not last_import:
            try:
                tname_key = (info.get('name') or '').lower().replace('.', ' ').replace('_', ' ').strip()
                if tname_key:
                    hr2 = requests.get(
                        f'{url}{history_path}',
                        headers={'X-Api-Key': key},
                        params={'sourceTitle': info.get('name'), 'pageSize': 50},
                        timeout=20,
                    )
                    if hr2.ok:
                        alt_imports = [
                            r.get('date') for r in (hr2.json().get('records') or [])
                            if r.get('eventType') == import_event
                            and tname_key[:40] in (r.get('sourceTitle') or '').lower().replace('.', ' ').replace('_', ' ')
                        ]
                        if alt_imports:
                            last_import = max(alt_imports)
            except Exception as e:
                log.debug(f'auto-rescue: sourceTitle fallback failed for {h[:8]}: {e}')
        if last_import >= last_grab:
            skipped_reasons['already_imported'] += 1
            _rescue_attempts.pop(h, None)
            continue
        active_hashes.add(h)
        attempts = _rescue_attempts.get(h, 0)
        if attempts >= AUTO_RESCUE_MAX_ATTEMPTS:
            skipped_reasons['max_attempts'] += 1
            if attempts == AUTO_RESCUE_MAX_ATTEMPTS:
                log.warning(f'auto-rescue {service}: giving up on "{info.get("name")}" after {attempts} attempts — needs manual import')
                record_activity('import-rescue', f'auto-rescue {service}: gave up on "{info.get("name")}" after {attempts} attempts')
                _rescue_attempts[h] = attempts + 1
            continue
        entry = {
            'hash': h,
            'name': info.get('name'),
            'save_path': info.get('save_path'),
            'label': info.get('label'),
            'last_grab': last_grab,
            'last_import': last_import or None,
        }
        stuck.append(entry)
        if dry_run:
            continue
        scan_path = info.get('save_path') or ''
        if info.get('name'):
            scan_path = os.path.join(scan_path, info.get('name'))
        try:
            cr = requests.post(
                f'{url}/api/v3/command',
                headers={'X-Api-Key': key, 'Content-Type': 'application/json'},
                json={'name': scan_cmd, 'path': scan_path, 'importMode': 'auto'},
                timeout=20,
            )
            cr.raise_for_status()
            cmd_id = cr.json().get('id')
            entry['scan_cmd_id'] = cmd_id
            triggered.append(entry)
            _rescue_attempts[h] = attempts + 1
            log.info(f'auto-rescue {service}: fired {scan_cmd} for {info.get("name")} → cmd {cmd_id} (attempt {attempts + 1}/{AUTO_RESCUE_MAX_ATTEMPTS})')
            record_activity('import-rescue', f'auto-rescue {service}: triggered import scan for "{info.get("name")}"')
        except Exception as e:
            log.warning(f'auto-rescue: scan trigger failed for {h[:8]}: {e}')
    # ponytail: only prune hashes we saw in this service's label set
    for stale_h in [k for k in _rescue_attempts if k in torrents
                    and (torrents[k].get('label') or '').lower() in labels
                    and k not in active_hashes]:
        del _rescue_attempts[stale_h]
    return {
        'ok': True,
        'service': service,
        'dry_run': dry_run,
        'stuck_count': len(stuck),
        'triggered_count': len(triggered),
        'skipped': skipped_reasons,
        'stuck': stuck,
    }

def auto_rescue_scheduler():
    # Runs every 15 minutes. Fires DownloadedMoviesScan/EpisodesScan for
    # any completed labeled torrent that Radarr/Sonarr grabbed but never
    # imported. Silent on the happy path — logs on rescue.
    while True:
        try:
            for svc in ('radarr', 'sonarr'):
                res = _auto_rescue(svc, dry_run=False)
                if res.get('triggered_count'):
                    log.info(f'auto-rescue {svc}: rescued {res["triggered_count"]} stuck imports')
        except Exception as e:
            log.error(f'auto-rescue scheduler failed: {e}')
        time.sleep(900)

@app.route('/auto-rescue', methods=['POST', 'GET'])
def auto_rescue_endpoint():
    service = (request.args.get('service') or 'both').lower()
    dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes')
    if request.args.get('reset', '').lower() in ('1', 'true', 'yes'):
        _rescue_attempts.clear()
        log.info('auto-rescue: retry counters reset')
    if service == 'both':
        return jsonify({
            'ok': True,
            'radarr': _auto_rescue('radarr', dry_run=dry_run),
            'sonarr': _auto_rescue('sonarr', dry_run=dry_run),
        }), 200
    return jsonify(_auto_rescue(service, dry_run=dry_run)), 200

# ── Plex dupe scanner ────────────────────────────────────────────────────────
# Two dupe classes:
#   multi_version — one Plex movie/episode with >1 attached Media entry.
#     Normal cause: an old + new file both exist under the same item.
#     Fix: keep the higher-scoring one, drop the other.
#   merged_metadata — multiple physically different films/episodes that
#     Plex bundled into one library item (the Pinocchio case). Detected
#     by comparing each Media's filename against the parent item title.
#     Fix: NOT automatic; requires user to split in Plex UI.
#
# Read-only for now — reports the list. Add ?apply=1 once we trust it.
def _plex_get(path, params=None):
    if not PLEX_TOKEN:
        raise RuntimeError('no PLEX_TOKEN set')
    p = dict(params or {})
    p['X-Plex-Token'] = PLEX_TOKEN
    r = requests.get(f'{PLEX_URL}{path}', params=p, headers={'Accept': 'application/json'}, timeout=30)
    r.raise_for_status()
    return r.json()

TITLE_TOKEN_RE = re.compile(r'[a-z0-9]+')
FILENAME_YEAR_RE = re.compile(r'(?<![a-z0-9])(19\d{2}|20\d{2})(?![a-z0-9])', re.IGNORECASE)

def _filename_title_tokens(basename):
    stem = basename.rsplit('.', 1)[0]
    m = FILENAME_YEAR_RE.search(stem)
    if m:
        stem = stem[:m.start()]
    return set(TITLE_TOKEN_RE.findall(stem.lower().replace("'", '')))

def _filename_year(basename):
    m = FILENAME_YEAR_RE.search(basename)
    return int(m.group(1)) if m else None

def _year_in_title(text):
    return set(int(y) for y in FILENAME_YEAR_RE.findall(text or ''))

@app.route('/plex-dupe-scan', methods=['GET'])
def plex_dupe_scan():
    if not PLEX_TOKEN:
        return jsonify({'ok': False, 'error': 'no PLEX_TOKEN (add to .env)'}), 400
    try:
        sections = _plex_get('/library/sections').get('MediaContainer', {}).get('Directory') or []
    except Exception as e:
        return jsonify({'ok': False, 'error': f'plex sections fetch failed: {e}'}), 500
    multi_version = []
    merged_metadata = []
    scanned = {'movie': 0, 'show_episodes': 0}
    skipped_libraries = []
    for sec in sections:
        stype = sec.get('type')
        skey = sec.get('key')
        stitle = sec.get('title') or ''
        if stype not in ('movie', 'show'):
            continue
        if stitle.lower() in PLEX_SKIP_LIBRARIES:
            skipped_libraries.append(stitle)
            continue
        if stype == 'movie':
            try:
                items = _plex_get(f'/library/sections/{skey}/all', {'includeGuids': 1}).get('MediaContainer', {}).get('Metadata') or []
            except Exception as e:
                log.warning(f'plex section {skey} fetch failed: {e}')
                continue
            for item in items:
                scanned['movie'] += 1
                media = item.get('Media') or []
                title = item.get('title') or ''
                year = item.get('year')
                title_tokens = set(TITLE_TOKEN_RE.findall(title.lower().replace("'", '')))
                title_tokens -= {'the', 'a', 'an', 'of', 'and'}
                media_summaries = []
                mismatched = []
                for m in media:
                    parts = m.get('Part') or []
                    for p in parts:
                        f = p.get('file') or ''
                        base = os.path.basename(f)
                        media_summaries.append({
                            'media_id': m.get('id'),
                            'part_id': p.get('id'),
                            'file': f,
                            'size_gb': round((p.get('size') or 0) / (1024**3), 2),
                            'resolution': m.get('videoResolution'),
                            'bitrate': m.get('bitrate'),
                            'duration_min': round((m.get('duration') or 0) / 60000, 1),
                        })
                        f_tokens = _filename_title_tokens(base) - {'the', 'a', 'an', 'of', 'and'}
                        f_year = _filename_year(base)
                        if title_tokens and f_tokens:
                            # False positive killers:
                            #   - "Wonder Woman 1984" grabs 1984 as year from title itself
                            #     → skip year_mismatch if f_year appears in parent title text
                            #   - "Dune: Part One" file "Dune (2021)" → file tokens ⊆ title tokens
                            #     means it's the same movie, Plex just has a fuller title
                            title_years = _year_in_title(title)
                            file_is_subset = f_tokens.issubset(title_tokens)
                            overlap = len(title_tokens & f_tokens) / max(1, len(title_tokens))
                            reverse_overlap = len(title_tokens & f_tokens) / max(1, len(f_tokens))
                            year_mismatch = (
                                year and f_year
                                and abs(f_year - year) > 1
                                and f_year not in title_years
                            )
                            # Real mismatch: filename doesn't overlap AND isn't a
                            # subset of title, OR year genuinely disagrees.
                            if year_mismatch or (overlap < 0.5 and reverse_overlap < 0.7 and not file_is_subset):
                                mismatched.append({
                                    'file': base,
                                    'file_year': f_year,
                                    'title_tokens_overlap': round(overlap, 2),
                                    'year_mismatch': bool(year_mismatch),
                                })
                if len(media) > 1:
                    multi_version.append({
                        'library': sec.get('title'),
                        'plex_key': item.get('ratingKey'),
                        'title': title,
                        'year': year,
                        'media_count': len(media),
                        'media': media_summaries,
                    })
                if mismatched:
                    merged_metadata.append({
                        'library': sec.get('title'),
                        'plex_key': item.get('ratingKey'),
                        'title': title,
                        'year': year,
                        'mismatched_files': mismatched,
                        'all_media': media_summaries,
                    })
        elif stype == 'show':
            try:
                shows = _plex_get(f'/library/sections/{skey}/all').get('MediaContainer', {}).get('Metadata') or []
            except Exception as e:
                log.warning(f'plex show section {skey} fetch failed: {e}')
                continue
            for show in shows:
                show_key = show.get('ratingKey')
                try:
                    episodes = _plex_get(f'/library/metadata/{show_key}/allLeaves').get('MediaContainer', {}).get('Metadata') or []
                except Exception as e:
                    log.debug(f'episode fetch for show {show_key} failed: {e}')
                    continue
                for ep in episodes:
                    scanned['show_episodes'] += 1
                    media = ep.get('Media') or []
                    if len(media) > 1:
                        media_summaries = []
                        for m in media:
                            for p in (m.get('Part') or []):
                                media_summaries.append({
                                    'media_id': m.get('id'),
                                    'part_id': p.get('id'),
                                    'file': p.get('file'),
                                    'size_gb': round((p.get('size') or 0) / (1024**3), 2),
                                    'resolution': m.get('videoResolution'),
                                    'duration_min': round((m.get('duration') or 0) / 60000, 1),
                                })
                        multi_version.append({
                            'library': sec.get('title'),
                            'plex_key': ep.get('ratingKey'),
                            'title': f"{show.get('title')} S{ep.get('parentIndex'):02d}E{ep.get('index'):02d} — {ep.get('title')}",
                            'media_count': len(media),
                            'media': media_summaries,
                        })
    return jsonify({
        'ok': True,
        'scanned': scanned,
        'skipped_libraries': skipped_libraries,
        'counts': {
            'multi_version': len(multi_version),
            'merged_metadata': len(merged_metadata),
        },
        'multi_version': multi_version,
        'merged_metadata': merged_metadata,
    }), 200

# Emergency: strip sonarr/radarr labels from torrents whose save_path
# is inside /data/Media/ (library seeds, not real arr downloads). Used
# to undo an over-eager /relabel-by-plex run that made Sonarr/Radarr
# treat every library seed as a new-download-needing-import.
# Dry-run default. ?apply=1 to actually relabel.
@app.route('/unlabel-library-seeds', methods=['POST', 'GET'])
def unlabel_library_seeds():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    new_label = request.args.get('new_label', '')  # default: blank
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path']],
                'id': 94,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    targets = []
    for h, info in torrents.items():
        label = (info.get('label') or '').lower()
        if label not in ('sonarr', 'radarr'):
            continue
        save_path = info.get('save_path') or ''
        # Library seeds live under /data/Media/*, real arr grabs land in
        # /data/Downloads/Complete/. Anything not in Downloads is a
        # library seed we shouldn't have labeled.
        if save_path.startswith('/data/Downloads/'):
            continue
        entry = {
            'hash': h,
            'name': info.get('name'),
            'save_path': save_path,
            'old_label': label,
            'new_label': new_label or '(blank)',
        }
        if apply:
            try:
                set_torrent_label(h, new_label)
                entry['result'] = 'relabeled'
            except Exception as e:
                entry['result'] = f'FAILED: {e}'
        targets.append(entry)
    return jsonify({
        'ok': True,
        'apply': apply,
        'count': len(targets),
        'targets': targets,
    }), 200

# Cross-reference unlabeled Deluge torrents against Plex's library.
# Any torrent whose file basename appears in Plex gets the appropriate
# label (radarr for movies, sonarr for TV). Anything unmatched goes on
# the review list — that's the "how did this get here" bucket.
# Dry-run by default; ?apply=1 to actually relabel.
@app.route('/relabel-by-plex', methods=['GET', 'POST'])
def relabel_by_plex():
    if not PLEX_TOKEN:
        return jsonify({'ok': False, 'error': 'no PLEX_TOKEN'}), 400
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    # Build basename → label map from Plex. Track collisions (same
    # basename appearing in both movie and TV libraries) so we can
    # punt them to the review bucket instead of guessing.
    plex_basenames = {}  # basename → 'radarr' | 'sonarr'
    plex_basename_collisions = set()
    try:
        sections = _plex_get('/library/sections').get('MediaContainer', {}).get('Directory') or []
    except Exception as e:
        return jsonify({'ok': False, 'error': f'plex sections fetch failed: {e}'}), 500
    for sec in sections:
        stype = sec.get('type')
        skey = sec.get('key')
        stitle = sec.get('title') or ''
        if stype not in ('movie', 'show'):
            continue
        if stitle.lower() in PLEX_SKIP_LIBRARIES:
            continue
        target_label = 'radarr' if stype == 'movie' else 'sonarr'
        try:
            if stype == 'movie':
                items = _plex_get(f'/library/sections/{skey}/all').get('MediaContainer', {}).get('Metadata') or []
                for item in items:
                    for m in (item.get('Media') or []):
                        for p in (m.get('Part') or []):
                            fname = os.path.basename(p.get('file') or '')
                            if not fname:
                                continue
                            prev = plex_basenames.get(fname)
                            if prev and prev != target_label:
                                plex_basename_collisions.add(fname)
                            plex_basenames[fname] = target_label
            else:
                shows = _plex_get(f'/library/sections/{skey}/all').get('MediaContainer', {}).get('Metadata') or []
                for show in shows:
                    show_key = show.get('ratingKey')
                    try:
                        eps = _plex_get(f'/library/metadata/{show_key}/allLeaves').get('MediaContainer', {}).get('Metadata') or []
                    except Exception:
                        continue
                    for ep in eps:
                        for m in (ep.get('Media') or []):
                            for p in (m.get('Part') or []):
                                fname = os.path.basename(p.get('file') or '')
                                if fname:
                                    plex_basenames[fname] = target_label
        except Exception as e:
            log.warning(f'relabel-by-plex: section {skey} fetch failed: {e}')
            continue
    # Now walk unlabeled Deluge torrents
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'files']],
                'id': 92,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    plan = {'radarr': [], 'sonarr': [], 'review': []}
    for h, info in torrents.items():
        if (info.get('label') or '').strip():
            continue
        tname = info.get('name') or ''
        # Try matching by torrent name (single-file case) and each file's basename
        candidates = [tname] + [
            os.path.basename(f.get('path') if isinstance(f, dict) else str(f))
            for f in (info.get('files') or [])
        ]
        matched_label = None
        matched_via = None
        collision = False
        for cand in candidates:
            if cand in plex_basename_collisions:
                collision = True
                matched_via = cand
                break
            if cand in plex_basenames:
                matched_label = plex_basenames[cand]
                matched_via = cand
                break
        entry = {
            'hash': h,
            'name': tname,
            'save_path': info.get('save_path'),
            'matched_via': matched_via,
        }
        if collision:
            entry['reason'] = 'basename collides between movie + TV libraries'
            plan['review'].append(entry)
        elif matched_label:
            entry['label'] = matched_label
            if apply:
                try:
                    set_torrent_label(h, matched_label)
                    entry['result'] = f'labeled {matched_label}'
                except Exception as e:
                    entry['result'] = f'FAILED: {e}'
            plan[matched_label].append(entry)
        else:
            plan['review'].append(entry)
    return jsonify({
        'ok': True,
        'apply': apply,
        'counts': {k: len(v) for k, v in plan.items()},
        'plex_basename_index_size': len(plex_basenames),
        'plan': plan,
    }), 200

# Manage the /plex-dupe-fix keep-list without rebuilding the container.
# GET  /plex-dupe-keep                    → list all keeps (env + file)
# POST /plex-dupe-keep {plex_key,title,reason}  → append
# DELETE /plex-dupe-keep?plex_key=X       → remove
@app.route('/plex-dupe-keep', methods=['GET', 'POST', 'DELETE'])
def plex_dupe_keep_manage():
    # Load current file entries
    entries = []
    if os.path.exists(PLEX_DUPE_KEEP_PATH):
        try:
            with open(PLEX_DUPE_KEEP_PATH) as f:
                entries = (_json.load(f).get('entries') or [])
        except Exception as e:
            return jsonify({'ok': False, 'error': f'read failed: {e}'}), 500
    if request.method == 'GET':
        return jsonify({
            'ok': True,
            'env_seed': sorted(PLEX_DUPE_KEEP),
            'file_entries': entries,
            'effective': sorted(_load_plex_dupe_keep()),
        }), 200
    if request.method == 'POST':
        body = request.get_json(force=True, silent=True) or {}
        plex_key = str(body.get('plex_key') or '').strip()
        if not plex_key:
            return jsonify({'ok': False, 'error': 'plex_key required'}), 400
        # Idempotent: replace if already present
        entries = [e for e in entries if str(e.get('plex_key')) != plex_key]
        entries.append({
            'plex_key': plex_key,
            'title': body.get('title') or '',
            'reason': body.get('reason') or '',
            'added': time.strftime('%Y-%m-%d'),
        })
        try:
            _save_plex_dupe_keep(entries)
        except Exception as e:
            return jsonify({'ok': False, 'error': f'save failed: {e}'}), 500
        return jsonify({'ok': True, 'entries': entries}), 200
    # DELETE
    plex_key = str(request.args.get('plex_key') or '').strip()
    if not plex_key:
        return jsonify({'ok': False, 'error': 'plex_key query param required'}), 400
    before = len(entries)
    entries = [e for e in entries if str(e.get('plex_key')) != plex_key]
    if len(entries) == before:
        return jsonify({'ok': True, 'removed': False, 'note': 'not in file (may be in env seed)'}), 200
    try:
        _save_plex_dupe_keep(entries)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'save failed: {e}'}), 500
    return jsonify({'ok': True, 'removed': True, 'entries': entries}), 200

# Move a set of torrents' storage locations via Deluge — used for
# one-off cleanups where scene packs landed in the wrong show folder.
# Body: {"moves":[{"hash":"…","dest":"/data/Media/TV Shows/Whatever"}]}
# Pauses each torrent before moving to avoid the "queued/rechecking
# refuses move_storage" edge case, then resumes.
@app.route('/torrent-move', methods=['POST'])
def torrent_move():
    body = request.get_json(force=True, silent=True) or {}
    moves = body.get('moves') or []
    if not moves:
        return jsonify({'ok': False, 'error': 'body must be {"moves":[{hash,dest}]}'}), 400
    try:
        deluge_login()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge login failed: {e}'}), 500
    results = []
    for i, m in enumerate(moves):
        h = (m.get('hash') or '').lower()
        dest = m.get('dest')
        entry = {'hash': h, 'dest': dest}
        if not h or not dest:
            entry['result'] = 'missing hash or dest'
            results.append(entry)
            continue
        try:
            # Pause
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.pause_torrent', 'params': [[h]], 'id': 100 + i * 4},
                timeout=15,
            ).raise_for_status()
            # Move storage
            mr = session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.move_storage', 'params': [[h], dest], 'id': 101 + i * 4},
                timeout=60,
            )
            mr.raise_for_status()
            move_result = mr.json()
            if move_result.get('error'):
                entry['result'] = f'move_storage error: {move_result.get("error")}'
                # try to resume anyway
                session.post(
                    f'{DELUGE_URL}/json',
                    json={'method': 'core.resume_torrent', 'params': [[h]], 'id': 102 + i * 4},
                    timeout=15,
                )
                results.append(entry)
                continue
            # Resume
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.resume_torrent', 'params': [[h]], 'id': 103 + i * 4},
                timeout=15,
            ).raise_for_status()
            entry['result'] = 'moved + resumed'
        except Exception as e:
            entry['result'] = f'FAILED: {e}'
        results.append(entry)
    return jsonify({'ok': True, 'results': results}), 200

# Act on Plex dupes surfaced by /plex-dupe-scan. Uses the same
# supersede-and-move flow as post-import: lower-quality file's torrent
# gets relabeled `superseded` and moved to SEEDING_DIR so Plex loses
# sight of it and cleanup_superseded takes it out after SEED_DAYS.
# Dry-run default. ?apply=1 executes.
def _find_torrent_by_filepath(torrents, filepath):
    """Find the Deluge torrent seeding a specific file.

    Two-pass match to avoid the "S01E01.mkv in two different shows"
    ambiguity that basename-only matching had:
      1. Full-path suffix match against the torrent's save_path + file path
         (or save_path + name for single-file torrents). Deterministic.
      2. Basename-only fallback, but only when exactly one candidate exists.
         Multiple basename matches → return None (unsafe to guess)."""
    if not filepath:
        return None, None
    target_norm = filepath.rstrip('/')
    target_base = os.path.basename(target_norm)
    basename_hits = []
    for h, info in torrents.items():
        save_path = (info.get('save_path') or '').rstrip('/')
        # Single-file torrents
        name = info.get('name') or ''
        if name and save_path:
            full = f'{save_path}/{name}'
            if full == target_norm or target_norm.endswith('/' + name) and target_norm == full:
                return h, info
        if name == target_base:
            basename_hits.append((h, info))
        # Multi-file torrents
        for f in (info.get('files') or []):
            p = f.get('path') if isinstance(f, dict) else str(f)
            if not p:
                continue
            full = f'{save_path}/{p}' if save_path else p
            if full == target_norm or target_norm.endswith('/' + p):
                return h, info
            if os.path.basename(p) == target_base:
                basename_hits.append((h, info))
                break
    unique_hashes = {h for h, _ in basename_hits}
    if len(unique_hashes) == 1:
        return basename_hits[0]
    return None, None

@app.route('/plex-dupe-fix', methods=['POST', 'GET'])
def plex_dupe_fix():
    if not PLEX_TOKEN:
        return jsonify({'ok': False, 'error': 'no PLEX_TOKEN'}), 400
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    # Reuse the scan output rather than duplicating the walk.
    # Flask views return (response, status) tuples — unpack first.
    try:
        scan_result = plex_dupe_scan()
        scan_resp = scan_result[0] if isinstance(scan_result, tuple) else scan_result
        scan = scan_resp.get_json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'scan failed: {e}'}), 500
    if not scan or not scan.get('ok'):
        return jsonify({'ok': False, 'error': 'scan returned no data'}), 500
    try:
        deluge_login()
        ensure_label_exists()
        dresp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'files', 'progress']],
                'id': 91,
            },
            timeout=30,
        )
        dresp.raise_for_status()
        torrents = dresp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    plan = {
        'quality_upgrade': [],
        'wrong_show_attach': [],
        'same_file_ghost': [],
        'kept_by_allowlist': [],
        'unknown': [],
    }
    keep_keys = _load_plex_dupe_keep()
    for entry in scan.get('multi_version') or []:
        plex_key = str(entry.get('plex_key') or '')
        if plex_key in keep_keys:
            plan['kept_by_allowlist'].append({
                'plex_key': plex_key, 'title': entry.get('title'),
            })
            continue
        media_list = entry.get('media') or []
        if len(media_list) < 2:
            continue
        # Identical filenames = Plex ghost entry. Manual fix (Refresh Metadata).
        basenames = [os.path.basename(m.get('file') or '') for m in media_list]
        if len(set(basenames)) == 1:
            plan['same_file_ghost'].append({
                'plex_key': plex_key,
                'title': entry.get('title'),
                'file': basenames[0],
                'action': 'Plex → item → ⋯ → Refresh Metadata',
            })
            continue
        # Score each Media: (resolution_rank, size_gb). Higher wins.
        res_rank = {'4k': 4, '2160': 4, '1080': 3, '720': 2, '576': 1, '480': 1, None: 0}
        def score(m):
            return (res_rank.get(str(m.get('resolution') or '').lower(), 0),
                    m.get('size_gb') or 0)
        # Determine title-token overlap for each Media vs parent
        parent_title_tokens = set(TITLE_TOKEN_RE.findall((entry.get('title') or '').lower().replace("'", '')))
        parent_title_tokens -= {'the', 'a', 'an', 'of', 'and', 's01e01', 'episode'}
        def matches_parent(m):
            base = os.path.basename(m.get('file') or '')
            f_tokens = _filename_title_tokens(base) - {'the', 'a', 'an', 'of', 'and'}
            if not parent_title_tokens or not f_tokens:
                return True
            overlap = len(parent_title_tokens & f_tokens) / max(1, len(parent_title_tokens))
            reverse = len(parent_title_tokens & f_tokens) / max(1, len(f_tokens))
            return overlap >= 0.3 or reverse >= 0.5
        good = [m for m in media_list if matches_parent(m)]
        bad = [m for m in media_list if not matches_parent(m)]
        if bad and good:
            # Wrong-show attach: the "bad" Media entries don't belong here.
            # Filesystem move required (into their real show folder), not
            # into Just4Seeding — so we surface manual instructions.
            plan['wrong_show_attach'].append({
                'plex_key': plex_key,
                'title': entry.get('title'),
                'keep_files': [os.path.basename(m.get('file') or '') for m in good],
                'detach_files': [
                    {
                        'file': m.get('file'),
                        'basename': os.path.basename(m.get('file') or ''),
                        'media_id': m.get('media_id'),
                        'size_gb': m.get('size_gb'),
                    } for m in bad
                ],
                'action': 'move each detach_file to its correct show folder, then trigger Plex library scan',
            })
            continue
        # Quality upgrade case: all Media match parent, delete lower quality
        sorted_media = sorted(media_list, key=score, reverse=True)
        keeper = sorted_media[0]
        losers = sorted_media[1:]
        upgrade_actions = []
        for loser in losers:
            fpath = loser.get('file') or ''
            h, tinfo = _find_torrent_by_filepath(torrents, fpath)
            action = {
                'file': fpath,
                'basename': os.path.basename(fpath),
                'size_gb': loser.get('size_gb'),
                'resolution': loser.get('resolution'),
                'media_id': loser.get('media_id'),
                'torrent_hash': h,
                'torrent_name': tinfo.get('name') if tinfo else None,
                'torrent_label': tinfo.get('label') if tinfo else None,
            }
            if apply:
                try:
                    if h and tinfo:
                        set_torrent_label(h, SUPERSEDED_LABEL)
                        move_torrent_storage(h, SEEDING_DIR)
                        action['result'] = f'torrent superseded + moved to {SEEDING_DIR}'
                    else:
                        # No torrent seeding it — delete the physical file.
                        # Plex loses it on next library scan. Path is
                        # translated from Plex's container view to
                        # arr-webhook's mounted view.
                        local = _translate_plex_path(fpath)
                        action['local_path'] = local
                        if not os.path.exists(local):
                            action['result'] = f'file not found at {local} (path map miss?)'
                        else:
                            try:
                                os.remove(local)
                                action['result'] = f'deleted {local}'
                            except PermissionError:
                                action['result'] = f'PERMISSION DENIED at {local} — mount may be ro'
                            except Exception as e:
                                action['result'] = f'delete failed: {e}'
                except Exception as e:
                    action['result'] = f'FAILED: {e}'
            upgrade_actions.append(action)
        plan['quality_upgrade'].append({
            'plex_key': plex_key,
            'title': entry.get('title'),
            'keep': {
                'file': os.path.basename(keeper.get('file') or ''),
                'size_gb': keeper.get('size_gb'),
                'resolution': keeper.get('resolution'),
            },
            'supersede': upgrade_actions,
        })
    return jsonify({
        'ok': True,
        'apply': apply,
        'counts': {k: len(v) for k, v in plan.items()},
        'plan': plan,
    }), 200

# List Sonarr seasons where NO episode has a season-pack import in
# history — i.e. seasons that were only ever grabbed as individual
# episodes. Useful for finding candidates to re-grab as season packs
# (better quality consistency, easier to seed).
@app.route('/missing-season-packs', methods=['GET'])
def missing_season_packs():
    if not SONARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no SONARR_API_KEY'}), 400
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=60,
        )
        r.raise_for_status()
        series_list = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Sonarr fetch failed: {e}'}), 500
    results = []
    for s in series_list:
        if not s.get('monitored'):
            continue
        sid = s.get('id')
        for season in (s.get('seasons') or []):
            snum = season.get('seasonNumber')
            if snum == 0:
                continue
            stats = season.get('statistics') or {}
            episode_count = stats.get('episodeCount') or 0
            file_count = stats.get('episodeFileCount') or 0
            if episode_count == 0 or file_count == 0:
                continue
            if file_count < episode_count:
                continue
            try:
                efr = requests.get(
                    f'{SONARR_URL}/api/v3/episodefile',
                    headers={'X-Api-Key': SONARR_API_KEY},
                    params={'seriesId': sid},
                    timeout=20,
                )
                efr.raise_for_status()
                files = [f for f in (efr.json() or []) if f.get('seasonNumber') == snum]
            except Exception as e:
                log.warning(f'season-packs: episodeFile fetch failed for {sid}/{snum}: {e}')
                continue
            # A season pack import lands every episode under a single
            # pack folder (e.g. `FBI.S08.WEBRip.x265-Vyndros/…`), so
            # dirname(originalFilePath) is uniform across all files.
            # Individually-grabbed episodes each have their own parent.
            # Combine with (releaseGroup, quality) for extra robustness —
            # different release groups can never be from the same pack
            # even if names collide.
            def _pack_key(f):
                orig = f.get('originalFilePath') or ''
                parent = os.path.dirname(orig).lower() if orig else ''
                # Some Vyndros-style packs name every file by episode
                # title with no shared pack folder — fall back to
                # (releaseGroup, quality) which still collapses across
                # a real pack even when the parent is missing.
                return (
                    (f.get('releaseGroup') or '').lower(),
                    (f.get('quality', {}).get('quality', {}).get('name') or ''),
                    parent,
                )
            distinct_packs = {_pack_key(f) for f in files}
            # If every file shares releaseGroup + quality, treat as one
            # pack regardless of parent-dir variance — covers Vyndros
            # naming convention.
            groups = {(f.get('releaseGroup') or '').lower() for f in files}
            qualities = {(f.get('quality', {}).get('quality', {}).get('name') or '') for f in files}
            uniform_group_and_quality = len(groups) == 1 and len(qualities) == 1
            sample_parents = sorted({p[2] for p in distinct_packs if p[2]})[:3]
            if len(distinct_packs) > 1 and not uniform_group_and_quality:
                results.append({
                    'series_id': sid,
                    'series_title': s.get('title'),
                    'season': snum,
                    'episodes': episode_count,
                    'files': file_count,
                    'distinct_releases': len(distinct_packs),
                    'sample_groups': sorted(groups),
                    'sample_parents': sample_parents,
                })
    results.sort(key=lambda x: (x['series_title'].lower(), x['season']))
    return jsonify({
        'ok': True,
        'count': len(results),
        'search_hint': 'POST /api/v3/command {"name":"SeasonSearch","seriesId":ID,"seasonNumber":N} to Sonarr',
        'seasons': results,
    }), 200

# List unlabeled Deluge torrents matching TV patterns (SxxExx, "Season N",
# "Complete Series", TV-quality strings). These are typically pre-Sonarr
# leftovers — grabbed manually before this project. Read-only; returns
# the list so the user can decide what to do (relabel, move, delete).
@app.route('/unlabeled-tv-scan', methods=['GET'])
def unlabeled_tv_scan():
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'time_added', 'total_size', 'progress', 'state']],
                'id': 88,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    tv_re = re.compile(
        r'(s\d{1,2}e\d{1,2}|season[\s._-]*\d+|complete[\s._-]series|\b\d{3,4}p[\s._-]*(hdtv|web|webrip))',
        re.IGNORECASE,
    )
    hits = []
    total_gb = 0.0
    for h, info in torrents.items():
        if (info.get('label') or '').strip():
            continue
        name = info.get('name') or ''
        if not tv_re.search(name):
            continue
        size_gb = round((info.get('total_size') or 0) / (1024**3), 2)
        total_gb += size_gb
        hits.append({
            'hash': h,
            'name': name,
            'save_path': info.get('save_path'),
            'size_gb': size_gb,
            'state': info.get('state'),
            'progress': info.get('progress'),
            'time_added': info.get('time_added'),
        })
    hits.sort(key=lambda x: x.get('time_added') or 0)
    return jsonify({
        'ok': True,
        'count': len(hits),
        'total_gb': round(total_gb, 2),
        'torrents': hits,
    }), 200

# Cross-reference Deluge completed torrents against Radarr/Sonarr state
# to surface the "grabbed → downloaded → never imported" failures that
# the queue endpoint hides once retries expire.
#
#   bucket_failed_import: Deluge torrent labeled radarr/radarr-upgrade,
#     progress ~100, but the matching Radarr movie has hasFile=false.
#     This is the actionable list — real failed imports.
#   bucket_no_seed: Radarr hasFile=true but no torrent name/file basename
#     matches in Deluge. Not a failure, just not seeding anymore.
#   bucket_orphan_torrent: labeled radarr/radarr-upgrade but no Radarr
#     movie matches the torrent name at all.
@app.route('/import-audit', methods=['GET'])
def import_audit():
    service = (request.args.get('service') or 'radarr').lower()
    if service != 'radarr':
        return jsonify({'ok': False, 'error': 'only radarr supported currently'}), 400
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=60,
        )
        r.raise_for_status()
        movies = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'files', 'progress', 'state']],
                'id': 77,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Deluge fetch failed: {e}'}), 500
    radarr_labels = {'radarr', 'radarr-upgrade'}
    seed_basenames = set()
    for h, info in torrents.items():
        n = info.get('name')
        if n:
            seed_basenames.add(os.path.basename(n))
        for f in (info.get('files') or []):
            p = f.get('path') if isinstance(f, dict) else str(f)
            if p:
                seed_basenames.add(os.path.basename(p))
    bucket_failed = []
    bucket_no_seed = []
    bucket_orphan = []
    matched_movie_ids = set()
    for h, info in torrents.items():
        if (info.get('label') or '').lower() not in radarr_labels:
            continue
        if (info.get('progress') or 0) < 99.0:
            continue
        tname = info.get('name') or ''
        hit = None
        for m in movies:
            titles = {
                (m.get('title') or '').lower(),
                (m.get('originalTitle') or '').lower(),
            }
            titles.discard('')
            if torrent_matches_any_title(tname, titles):
                hit = m
                break
        if not hit:
            bucket_orphan.append({
                'hash': h,
                'name': tname,
                'label': info.get('label'),
                'save_path': info.get('save_path'),
            })
            continue
        matched_movie_ids.add(hit.get('id'))
        if not hit.get('hasFile'):
            bucket_failed.append({
                'movie_id': hit.get('id'),
                'title': hit.get('title'),
                'year': hit.get('year'),
                'hash': h,
                'torrent_name': tname,
                'save_path': info.get('save_path'),
                'state': info.get('state'),
            })
    for m in movies:
        if not m.get('hasFile'):
            continue
        mf = m.get('movieFile') or {}
        rel = mf.get('relativePath') or (mf.get('path') or '')
        base = os.path.basename(rel) if rel else ''
        if not base:
            continue
        if base in seed_basenames:
            continue
        bucket_no_seed.append({
            'movie_id': m.get('id'),
            'title': m.get('title'),
            'year': m.get('year'),
            'tracked_file': base,
            'size_gb': round((mf.get('size') or 0) / (1024**3), 2),
        })
    return jsonify({
        'ok': True,
        'service': service,
        'totals': {
            'radarr_movies': len(movies),
            'deluge_torrents': len(torrents),
            'radarr_labeled_torrents': sum(1 for i in torrents.values() if (i.get('label') or '').lower() in radarr_labels),
        },
        'bucket_failed_import': bucket_failed,
        'bucket_orphan_torrent': bucket_orphan,
        'bucket_no_seed': bucket_no_seed,
        'counts': {
            'failed_import': len(bucket_failed),
            'orphan_torrent': len(bucket_orphan),
            'no_seed': len(bucket_no_seed),
        },
    }), 200

# Bulk sweep for torrents stuck wearing the '-upgrade' label despite their
# import having actually completed already. The 2026-07-18 fix
# (relabel_download_to_base, called from handle_upgrade_import) only flips
# the tag going forward, at the moment a webhook fires -- it never backfilled
# torrents that were already stuck before that fix landed, and identification
# failures (messy names, season packs) can still let new ones slip through.
# This finds them independently, by cross-referencing completed (progress
# >= 99%) upgrade-labeled torrents against Radarr/Sonarr's own import state,
# the same proven pattern /import-audit already uses for Radarr.
#
# Radarr: torrent title-matched to a movie; movie.hasFile == true means the
# import landed and the tag is stale.
# Sonarr: torrent title-matched to a series; season number parsed from the
# torrent name (S01, S01E02, etc.) -- if it can't be parsed, the torrent is
# skipped rather than guessed at. episodeFileCount >= episodeCount for that
# season means the import (single episode or full pack) landed.
#
# Defaults to dry-run (?apply=1 to actually flip labels), matching the
# convention every other bulk-action endpoint here already uses.
SEASON_RE = re.compile(r'[Ss](\d{1,2})(?:[Ee]\d{1,3})?')

@app.route('/fix-stuck-upgrade-tags', methods=['GET', 'POST'])
def fix_stuck_upgrade_tags():
    service = (request.args.get('service') or 'both').lower()
    apply = request.args.get('apply') == '1'
    results = {'radarr': [], 'sonarr': []}
    errors = []

    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'progress']],
                'id': 94,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Deluge fetch failed: {e}'}), 500

    if service in ('radarr', 'both') and RADARR_API_KEY:
        try:
            r = requests.get(f'{RADARR_URL}/api/v3/movie', headers={'X-Api-Key': RADARR_API_KEY}, timeout=60)
            r.raise_for_status()
            movies = r.json()
        except Exception as e:
            errors.append(f'Radarr fetch failed: {e}')
            movies = []
        for h, info in torrents.items():
            if (info.get('label') or '').lower() != RADARR_UPG_LABEL.lower():
                continue
            if (info.get('progress') or 0) < 99.0:
                continue
            tname = info.get('name') or ''
            hit = None
            for m in movies:
                titles = {(m.get('title') or '').lower(), (m.get('originalTitle') or '').lower()}
                titles.discard('')
                if torrent_matches_any_title(tname, titles):
                    hit = m
                    break
            if not hit or not hit.get('hasFile'):
                continue
            entry = {'hash': h, 'name': tname, 'movie_title': hit.get('title'), 'year': hit.get('year')}
            if apply:
                result, _ = relabel_download_to_base(h, 'Radarr')
                entry['result'] = result
                if result == 'flipped':
                    record_activity('fix-stuck-upgrade-tags', f'Radarr: flipped stale upgrade tag on "{tname}"')
            results['radarr'].append(entry)

    if service in ('sonarr', 'both') and SONARR_API_KEY:
        try:
            r = requests.get(f'{SONARR_URL}/api/v3/series', headers={'X-Api-Key': SONARR_API_KEY}, timeout=60)
            r.raise_for_status()
            series_list = r.json()
        except Exception as e:
            errors.append(f'Sonarr fetch failed: {e}')
            series_list = []
        for h, info in torrents.items():
            if (info.get('label') or '').lower() != SONARR_UPG_LABEL.lower():
                continue
            if (info.get('progress') or 0) < 99.0:
                continue
            tname = info.get('name') or ''
            season_match = SEASON_RE.search(tname)
            if not season_match:
                continue  # can't safely determine which season -- skip rather than guess
            season_num = int(season_match.group(1))
            hit = None
            for s in series_list:
                titles = {(s.get('title') or '').lower()}
                titles |= {(t.get('title') or '').lower() for t in (s.get('alternateTitles') or [])}
                titles.discard('')
                if torrent_matches_any_title(tname, titles):
                    hit = s
                    break
            if not hit:
                continue
            season = next((sn for sn in (hit.get('seasons') or []) if sn.get('seasonNumber') == season_num), None)
            if not season:
                continue
            stats = season.get('statistics') or {}
            episode_count = stats.get('episodeCount') or 0
            file_count = stats.get('episodeFileCount') or 0
            if episode_count == 0 or file_count < episode_count:
                continue  # season not fully imported yet -- correctly still throttled
            entry = {'hash': h, 'name': tname, 'series_title': hit.get('title'), 'season': season_num}
            if apply:
                result, _ = relabel_download_to_base(h, 'Sonarr')
                entry['result'] = result
                if result == 'flipped':
                    record_activity('fix-stuck-upgrade-tags', f'Sonarr: flipped stale upgrade tag on "{tname}"')
            results['sonarr'].append(entry)

    return jsonify({
        'ok': True,
        'apply': apply,
        'errors': errors,
        'counts': {k: len(v) for k, v in results.items()},
        'radarr': results['radarr'],
        'sonarr': results['sonarr'],
    }), 200

# Radarr movies whose tracked file lacks surround audio, HDR, and x265.
# All three markers = "modern release"; missing all three = older encode
# worth flagging for upgrade. Query args:
#   ?missing=surround,hdr,x265 (any/all; default: all three)
@app.route('/quality-audit', methods=['GET'])
def quality_audit():
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    want_missing = {s.strip().lower() for s in (request.args.get('missing') or 'surround,hdr,x265').split(',') if s.strip()}
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=60,
        )
        r.raise_for_status()
        movies = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500
    hits = []
    for m in movies:
        if not m.get('hasFile'):
            continue
        mf = m.get('movieFile') or {}
        mi = mf.get('mediaInfo') or {}
        channels = mi.get('audioChannels') or 0
        video_codec = (mi.get('videoCodec') or '').lower()
        dyn_range = (mi.get('videoDynamicRange') or '').lower()
        dyn_type = (mi.get('videoDynamicRangeType') or '').lower()
        rel = mf.get('relativePath') or ''
        rel_l = rel.lower()
        # Fall back to filename markers when mediaInfo is missing
        has_surround = channels >= 5.1 or any(t in rel_l for t in ['5.1', '7.1', 'atmos', 'truehd', 'ddp', 'dts'])
        # HDR fallback markers: real releases use dot/space delimited tags
        # like `.DV.`, `.DoVi.`, `.HDR10+.`. Match with delimiters so we
        # don't falsely fire on words that contain "dv" or "hdr" as
        # substrings.
        has_hdr = bool(dyn_range) or any(
            t in rel_l for t in [
                '.hdr.', ' hdr ', '.hdr10.', '.hdr10+.', '.dv.', ' dv ',
                '.dovi.', 'dolby.vision', 'dolby vision', '.uhd.',
            ]
        )
        has_x265 = 'x265' in video_codec or 'hevc' in video_codec or any(t in rel_l for t in ['x265', 'hevc', 'h265', 'h.265'])
        missing = set()
        if not has_surround:
            missing.add('surround')
        if not has_hdr:
            missing.add('hdr')
        if not has_x265:
            missing.add('x265')
        # match rule: movie qualifies if it's missing every marker the caller asked about
        if not want_missing.issubset(missing):
            continue
        hits.append({
            'id': m.get('id'),
            'title': m.get('title'),
            'year': m.get('year'),
            'file': rel,
            'size_gb': round((mf.get('size') or 0) / (1024**3), 2),
            'channels': channels,
            'video_codec': video_codec,
            'dyn_range': dyn_range or dyn_type or None,
            'missing': sorted(missing),
        })
    hits.sort(key=lambda x: (x['title'] or '').lower())
    return jsonify({
        'ok': True,
        'want_missing': sorted(want_missing),
        'count': len(hits),
        'search_hint': 'POST /api/v3/command {"name":"MoviesSearch","movieIds":[ID]}',
        'movies': hits,
    }), 200

# List monitored Radarr movies with hasFile=false — the gap-fill list.
# Returns enough metadata to prioritize (release dates, availability,
# added date, quality profile, tags). Sort options exposed via ?sort=.
# Trigger searches one at a time via:
#   curl -X POST http://radarr/api/v3/command -d '{"name":"MoviesSearch","movieIds":[ID]}'
@app.route('/missing-movies', methods=['GET'])
def missing_movies():
    if not RADARR_API_KEY:
        return jsonify({'ok': False, 'error': 'no RADARR_API_KEY'}), 400
    sort_by = (request.args.get('sort') or 'added').lower()
    include_unmonitored = request.args.get('unmonitored', '').lower() in ('1', 'true', 'yes')
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=60,
        )
        r.raise_for_status()
        movies = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500
    missing = []
    for m in movies:
        if m.get('hasFile'):
            continue
        if not include_unmonitored and not m.get('monitored'):
            continue
        missing.append({
            'id': m.get('id'),
            'title': m.get('title'),
            'year': m.get('year'),
            'monitored': m.get('monitored'),
            'added': m.get('added'),
            'minimum_availability': m.get('minimumAvailability'),
            'status': m.get('status'),
            'in_cinemas': m.get('inCinemas'),
            'digital_release': m.get('digitalRelease'),
            'physical_release': m.get('physicalRelease'),
            'quality_profile_id': m.get('qualityProfileId'),
            'tags': m.get('tags') or [],
            'tmdb_id': m.get('tmdbId'),
        })
    sort_keys = {
        'added': lambda x: x.get('added') or '',
        'year': lambda x: x.get('year') or 0,
        'title': lambda x: (x.get('title') or '').lower(),
        'digital': lambda x: x.get('digital_release') or '',
        'physical': lambda x: x.get('physical_release') or '',
        'cinemas': lambda x: x.get('in_cinemas') or '',
    }
    key_fn = sort_keys.get(sort_by, sort_keys['added'])
    reverse = sort_by in ('added', 'year', 'digital', 'physical', 'cinemas')
    missing.sort(key=key_fn, reverse=reverse)
    return jsonify({
        'ok': True,
        'count': len(missing),
        'sort': sort_by,
        'search_hint': 'POST /api/v3/command {"name":"MoviesSearch","movieIds":[ID]} to Radarr',
        'movies': missing,
    }), 200

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

# Scan an Incomplete directory for files not backed by any Deluge torrent.
# Deluge parks in-progress downloads under DOWNLOADS_MOUNT/Incomplete; when
# a torrent gets removed (or crashes) mid-download the partial files can
# be left behind as orphans. This surfaces anything on disk whose basename
# does not correspond to a name/file of a currently-tracked torrent.
# Dry-run default. ?apply=1 deletes.
@app.route('/incomplete-orphans', methods=['GET', 'POST'])
def incomplete_orphans():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    downloads_root = os.environ.get('DOWNLOADS_MOUNT', '/data/Downloads')
    incomplete_dir = os.path.join(downloads_root, 'Incomplete')
    if not os.path.isdir(incomplete_dir):
        return jsonify({'ok': False, 'error': f'{incomplete_dir} not a directory'}), 400
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'save_path', 'files']],
                'id': 96,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    # Union of every top-level name a torrent might land on disk as: the
    # torrent's `name`, plus the first path segment of each file entry.
    tracked_names = set()
    for info in torrents.values():
        n = info.get('name')
        if n:
            tracked_names.add(n)
        for f in (info.get('files') or []):
            p = f.get('path') if isinstance(f, dict) else str(f)
            if p:
                tracked_names.add(p.split('/', 1)[0])
    orphans = []
    kept = []
    for entry in sorted(os.listdir(incomplete_dir)):
        full = os.path.join(incomplete_dir, entry)
        try:
            size = _du(full)
        except Exception:
            size = 0
        rec = {
            'name': entry,
            'path': full,
            'size_gb': round(size / (1024**3), 2),
            'is_dir': os.path.isdir(full),
        }
        if entry in tracked_names:
            kept.append(rec)
            continue
        if apply:
            try:
                if os.path.isdir(full):
                    import shutil
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                rec['result'] = 'deleted'
            except Exception as e:
                rec['result'] = f'FAILED: {e}'
        orphans.append(rec)
    return jsonify({
        'ok': True,
        'apply': apply,
        'incomplete_dir': incomplete_dir,
        'counts': {'orphans': len(orphans), 'tracked': len(kept)},
        'orphans': orphans,
        'tracked': kept,
    }), 200

def _du(path):
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total

# Remove a single torrent by hash, deleting files. Use for the
# "superseded + still incomplete + not worth finishing" case (Radarr
# has already moved on to a newer release, so the partial download is
# dead weight). Body OR query: hash=<hash>. Dry-run default.
@app.route('/torrent-purge', methods=['POST', 'GET'])
def torrent_purge():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    torrent_hash = (request.args.get('hash') or '').lower().strip()
    if not torrent_hash:
        body = request.get_json(silent=True) or {}
        torrent_hash = (body.get('hash') or '').lower().strip()
    if not torrent_hash:
        return jsonify({'ok': False, 'error': 'hash required (query or body)'}), 400
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrent_status',
                'params': [torrent_hash, ['name', 'label', 'save_path', 'progress']],
                'id': 97,
            },
            timeout=10,
        )
        resp.raise_for_status()
        info = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    if not info:
        return jsonify({'ok': False, 'error': f'no torrent with hash {torrent_hash}'}), 404
    result = {
        'hash': torrent_hash,
        'name': info.get('name'),
        'label': info.get('label'),
        'save_path': info.get('save_path'),
        'progress': info.get('progress'),
    }
    if not apply:
        result['note'] = 'dry-run — pass ?apply=1 to remove torrent + files'
        return jsonify({'ok': True, 'apply': False, 'target': result}), 200
    try:
        remove_torrent(torrent_hash)
        result['result'] = 'removed torrent + files'
    except Exception as e:
        result['result'] = f'FAILED: {e}'
        return jsonify({'ok': False, 'apply': True, 'target': result}), 500
    return jsonify({'ok': True, 'apply': True, 'target': result}), 200

# Report-only audit of everything labeled `superseded`: how long each has
# been seeding, whether it's already past SEED_DAYS (i.e. would be swept
# by the next cleanup run), and whether its save_path is under SEEDING_DIR
# (where superseded torrents should live so Radarr/Sonarr don't re-see them).
# Use this before /run-cleanup or a monthly upgrade to eyeball what's about
# to disappear and catch any misfiled torrents.
@app.route('/superseded-audit', methods=['GET'])
def superseded_audit():
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'label', 'save_path', 'seeding_time', 'total_size']],
                'id': 95,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
    threshold = SEED_DAYS * 86400
    seeding_dir_norm = SEEDING_DIR.rstrip('/')
    ready_to_delete = []
    misfiled = []
    still_seeding = []
    for h, info in torrents.items():
        if info.get('label') != SUPERSEDED_LABEL:
            continue
        seed_sec = info.get('seeding_time') or 0
        save_path = (info.get('save_path') or '').rstrip('/')
        in_seeding_dir = save_path == seeding_dir_norm or save_path.startswith(seeding_dir_norm + '/')
        entry = {
            'hash': h,
            'name': info.get('name'),
            'save_path': save_path,
            'seed_days': round(seed_sec / 86400, 1),
            'size_gb': round((info.get('total_size') or 0) / (1024**3), 2),
            'in_seeding_dir': in_seeding_dir,
        }
        if not in_seeding_dir:
            entry['expected_dir'] = seeding_dir_norm
            misfiled.append(entry)
        if seed_sec >= threshold:
            ready_to_delete.append(entry)
        else:
            still_seeding.append(entry)
    ready_to_delete.sort(key=lambda x: -x['seed_days'])
    still_seeding.sort(key=lambda x: -x['seed_days'])
    return jsonify({
        'ok': True,
        'seed_days_threshold': SEED_DAYS,
        'seeding_dir': seeding_dir_norm,
        'counts': {
            'ready_to_delete': len(ready_to_delete),
            'still_seeding': len(still_seeding),
            'misfiled': len(misfiled),
        },
        'ready_to_delete': ready_to_delete,
        'still_seeding': still_seeding,
        'misfiled': misfiled,
    }), 200

# Manual trigger for the monthly upgrade cycle. Full cycle with the
# normal 30/90-minute waits by default; ?skip_waits=1 replaces them with
# a short interval so you can watch the whole pipeline end-to-end.
@app.route('/run-monthly-upgrade', methods=['POST'])
def run_monthly_upgrade():
    skip_waits = request.args.get('skip_waits', '').lower() in ('1', 'true', 'yes')
    def _cycle():
        log.info(f'Manual monthly upgrade cycle starting (skip_waits={skip_waits})')
        purge_stalled_upgrade_torrents()
        wait_a = 10 if skip_waits else 1800
        log.info(f'Waiting {wait_a}s before bulk search...')
        time.sleep(wait_a)
        radarr_bulk_search()
        wait_b = 30 if skip_waits else 300
        log.info(f'Waiting {wait_b}s before relabeling upgrades...')
        time.sleep(wait_b)
        relabel_radarr_upgrades()
        log.info('Manual monthly upgrade cycle complete')
    threading.Thread(target=_cycle, daemon=True).start()
    return jsonify({
        'ok': True,
        'skip_waits': skip_waits,
        'message': 'monthly upgrade cycle started; watch container logs',
    }), 200

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
        # Collect the basename of every file Radarr currently tracks.
        # Container's view of the movie library differs from Radarr's
        # (bind mount at /media/movies vs host /mnt/user/... etc), so
        # matching on absolute paths won't work. Basename matching is
        # robust because movie release filenames are effectively unique.
        # tracked_names: basenames Radarr currently has as movieFile.
        # radarr_index: (title_words_set, year) so we can decide whether
        # an unknown-basename file corresponds to a Radarr-managed movie
        # (safe dupe to delete) vs an untracked movie (do NOT delete).
        tracked_names = set()
        radarr_index = []
        for m in r.json():
            mf = m.get('movieFile') or {}
            path = mf.get('path') or mf.get('relativePath') or ''
            if path:
                tracked_names.add(os.path.basename(path))
            title = (m.get('title') or '').lower()
            year = m.get('year')
            if title and year:
                words = {w for w in re.findall(r'[a-z0-9]+', title) if len(w) >= 3}
                if words:
                    radarr_index.append((words, year))
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Radarr fetch failed: {e}'}), 500

    def _classify(path, basename):
        # Sample files — safe to delete.
        low = path.lower()
        if 'sample' in low or basename.lower().startswith('sample'):
            return 'sample'
        # Dupe of a Radarr-managed movie — the movie has a tracked file
        # already, this basename is different, so it's an old copy.
        # Match by title-words + year.
        name_lower = basename.lower()
        year_m = re.search(r'(?<!\d)(19\d{2}|20\d{2})(?!\d)', name_lower)
        if year_m:
            file_year = int(year_m.group(1))
            file_words = set(re.findall(r'[a-z0-9]+', name_lower))
            for words, year in radarr_index:
                if abs(year - file_year) > 1:
                    continue
                if words.issubset(file_words):
                    return 'dupe'
        return 'untracked'

    # Cross-check against Deluge: any file basename that appears in an
    # active torrent's file list is presumed to be seeding. We skip those
    # to protect the seed. Basename comparison works even when the
    # movies-library and downloads paths don't line up (bind mounts,
    # separate filesystems, etc).
    seeding_basenames = set()
    try:
        deluge_login()
        # Ask Deluge specifically for `files` — the default helper only
        # returns lightweight fields.
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'files']],
                'id': 42,
            },
            timeout=15,
        )
        resp.raise_for_status()
        for h, info in (resp.json().get('result') or {}).items():
            n = info.get('name')
            if n:
                seeding_basenames.add(os.path.basename(n))
            for f in (info.get('files') or []):
                p = f.get('path') if isinstance(f, dict) else str(f)
                if p:
                    seeding_basenames.add(os.path.basename(p))
    except Exception as e:
        log.warning(f'orphan-scan: Deluge cross-check failed ({e}) — treating all dupes as seeding for safety')
        seeding_basenames = None  # sentinel: unknown, be cautious

    buckets = {'sample': [], 'dupe': [], 'dupe_seeding': [], 'untracked': []}
    video_exts = ('.mkv', '.mp4', '.avi', '.m4v', '.mov')
    for root, _, files in os.walk(MOVIES_LIBRARY):
        for f in files:
            if not f.lower().endswith(video_exts):
                continue
            if f in tracked_names:
                continue
            full = os.path.normpath(os.path.join(root, f))
            try:
                st = os.stat(full)
                size = st.st_size
                nlink = st.st_nlink
            except OSError:
                size = 0
                nlink = 1
            cat = _classify(full, f)
            # Reroute dupe → dupe_seeding when Deluge is still seeding a
            # torrent with this filename. nlink checks are useless on
            # Unraid's /mnt/user FUSE (always reports 1 through shfs), so
            # Deluge is the source of truth for what's actively serving.
            if cat == 'dupe':
                if seeding_basenames is None or f in seeding_basenames:
                    cat = 'dupe_seeding'
            buckets[cat].append({'path': full, 'size': size, 'nlink': nlink})

    # Which categories to actually delete. `mode` query param picks:
    #   samples   — only sample files
    #   dupes     — samples + confirmed dupes (Radarr-tracked movie's old copy)
    #   all       — everything (danger, includes untracked)
    mode = request.args.get('mode', 'dupes')
    to_delete_buckets = []
    if delete:
        if mode == 'samples':
            to_delete_buckets = ['sample']
        elif mode == 'dupes':
            to_delete_buckets = ['sample', 'dupe']
        elif mode == 'all':
            to_delete_buckets = ['sample', 'dupe', 'untracked']
        else:
            return jsonify({'ok': False, 'error': f'unknown mode: {mode}'}), 400

    deleted = 0
    if delete:
        for cat in to_delete_buckets:
            for o in buckets[cat]:
                try:
                    os.remove(o['path'])
                    deleted += 1
                    log.info(f'orphan-scan[{cat}]: removed {o["path"]}')
                except OSError as e:
                    log.warning(f'orphan-scan: failed to remove {o["path"]}: {e}')

    def _gb(items):
        return round(sum(i['size'] for i in items) / (1024**3), 2)

    return jsonify({
        'ok': True,
        'dry_run': not delete,
        'delete_mode': mode if delete else None,
        'tracked_count': len(tracked_names),
        'counts': {k: len(v) for k, v in buckets.items()},
        'sizes_gb': {k: _gb(v) for k, v in buckets.items()},
        'deleted': deleted,
        'buckets': buckets,
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
    t5 = threading.Thread(target=auto_rescue_scheduler, daemon=True)
    t5.start()
    app.run(host='0.0.0.0', port=port, threaded=True)
