import os
import re
import json as _json
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
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

ACTIVITY_LOG_RETENTION_DAYS = int(os.environ.get('ACTIVITY_LOG_RETENTION_DAYS', '7'))

def trim_activity_log(retention_days=ACTIVITY_LOG_RETENTION_DAYS):
    """Drop entries older than retention_days, keeping the file bounded
    while still covering things like tracker Hit & Run windows (typically
    7 days) that need real evidence to diagnose after the fact. Rewrites
    the whole file rather than trimming in place -- this log is written to
    frequently but read/trimmed only once a day, so a full rewrite here is
    cheap relative to append cost on every real event."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec='seconds')
    try:
        with open(ACTIVITY_LOG) as f:
            lines = f.readlines()
    except OSError:
        return 0
    kept = []
    dropped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = _json.loads(line)
        except ValueError:
            kept.append(line)  # don't silently discard unparseable lines
            continue
        if e.get('ts', '') < cutoff:
            dropped += 1
        else:
            kept.append(line)
    if dropped:
        try:
            with open(ACTIVITY_LOG, 'w') as f:
                f.write('\n'.join(kept) + ('\n' if kept else ''))
            log.info(f'[activity-log] trimmed {dropped} entries older than {retention_days}d')
        except OSError as e:
            log.warning(f'[activity-log] trim write failed: {e}')
            return 0
    return dropped

def activity_log_trim_scheduler():
    time.sleep(300)  # let the app finish booting first
    while True:
        try:
            trim_activity_log()
        except Exception as e:
            log.error(f'[activity-log] trim failed: {e}')
        time.sleep(86400)

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
# Weekly stalled-seed review (every LABELED torrent, including
# LIBRARY_SEED_LABEL -- that's the actual intended target; unlabeled/
# unmanaged torrents are always skipped): a torrent must have seeded at
# least this many days AND uploaded less than the byte threshold since the
# previous week's check AND have more than STALL_MIN_SWARM_SEEDS other
# seeds already in the swarm (so unseeding our copy doesn't hurt
# availability) to be considered "not meaningfully seeding" and unseeded.
# This only unseeds (Deluge stops tracking it) -- the file(s) on disk are
# never deleted, since for library-seed torrents the file IS the live
# Plex media. For the "old library-seed torrents sitting around for
# months" use case, set STALL_SEED_MIN_DAYS=180 (6 months) at deploy time.
STALL_SEED_MIN_DAYS = int(os.environ.get('STALL_SEED_MIN_DAYS', '21'))
STALL_UPLOAD_THRESHOLD_BYTES = int(os.environ.get('STALL_UPLOAD_THRESHOLD_BYTES', str(10 * 1024 * 1024)))
STALL_CHECK_INTERVAL = int(os.environ.get('STALL_CHECK_INTERVAL', str(7 * 86400)))
# Swarm-safety gate: only remove a stalled torrent if the tracker reports
# MORE than this many other seeds already in the swarm. Deluge/libtorrent
# reports total_seeds == -1 when the tracker hasn't scraped yet (unknown,
# not zero) -- treated as "don't know, don't touch", never as safe to remove.
STALL_MIN_SWARM_SEEDS = int(os.environ.get('STALL_MIN_SWARM_SEEDS', '5'))
SEED_STATE_PATH = os.environ.get('SEED_STATE_PATH', '/data/seed_tracking.json')
# Off by default until proven safe. The manual preview endpoint
# (/run-stalled-seeds, dry-run by default) still works regardless of this
# flag — this only gates the automatic weekly background removal.
STALL_AUTOMATION_ENABLED = os.environ.get('STALL_AUTOMATION_ENABLED', 'false').lower() in ('1', 'true', 'yes')
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

def _map_prefix(p, pairs):
    """Rewrite path `p` using the first matching (from, to) prefix pair.
    Requires a directory boundary so a short prefix like `/data/M` doesn't
    accidentally match `/data/Movies` AND `/data/Music`."""
    if not p:
        return p
    for from_prefix, to_prefix in pairs:
        prefix = from_prefix.rstrip('/')
        if p == prefix or p.startswith(prefix + '/'):
            return to_prefix.rstrip('/') + p[len(prefix):]
    return p

def _translate_plex_path(p):
    return _map_prefix(p, PLEX_PATH_MAP)
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
# Bulk search pacing. One MoviesSearch over the whole library makes Radarr
# push every accepted release to Deluge back-to-back, so the client
# announces to the tracker for all of them at once. Private trackers
# rate-limit announces per-IP -- trip that and announces start timing out
# for *every* torrent, including the new release you wanted to be early on.
# Lower batch / higher delay is gentler on the tracker.
BULK_SEARCH_BATCH = int(os.environ.get('BULK_SEARCH_BATCH', '50'))
BULK_SEARCH_DELAY = int(os.environ.get('BULK_SEARCH_DELAY', '180'))  # secs between batches
# Sonarr batches are much smaller: one SeriesSearch fans out to every
# monitored episode in that series, so 50 series is an order of magnitude
# more announces than 50 movies. Tune down further if the tracker complains.
SONARR_BULK_SEARCH_BATCH = int(os.environ.get('SONARR_BULK_SEARCH_BATCH', '10'))

PROPER_REPACK_RE = re.compile(r'\b(PROPER|REPACK|RERIP)\b', re.IGNORECASE)
EPISODE_RE       = re.compile(r'S\d{2}E\d{2}', re.IGNORECASE)
# Season token (S01, S01E02, ...). Group 1 is the season number. Defined here
# (alongside EPISODE_RE) so the season-pack helpers can use it; re-used by the
# /fix-stuck-upgrade-tags route far below.
SEASON_RE = re.compile(r'[Ss](\d{1,2})(?:[Ee]\d{1,3})?')

session = requests.Session()

# ── Arr API helpers ──────────────────────────────────────────────────────────

def add_year_stripped_variants(variants):
    """Additively extend a set of lowercased title variants with year-stripped
    forms, so a series stored as 'Very Important People (2023)' also matches
    releases named without the disambiguation year. Originals are always kept;
    only trailing 4-digit years are stripped — never when the year IS the whole
    title ('1923'), and never non-year parentheticals ('the office (us)')."""
    out = set()
    for v in variants:
        if not isinstance(v, str) or not v:
            continue
        out.add(v)
        stripped = re.sub(r'\s*\(\d{4}\)\s*$', '', v)
        if stripped == v:
            # Bare trailing year, but only when preceded by other words.
            stripped = re.sub(r'\s+\d{4}$', '', v)
        stripped = stripped.strip()
        if (stripped and stripped != v
                and max((len(w) for w in stripped.split()), default=0) >= 3):
            out.add(stripped)
    return out

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
        normalized = add_year_stripped_variants(normalized)
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
    
    # Verify the move actually succeeded by checking the save_path
    status_resp = session.post(
        f'{DELUGE_URL}/json',
        json={
            'method': 'core.get_torrents_status',
            'params': [{}, ['save_path']],
            'id': 6,
        },
        timeout=10
    )
    try:
        status_result = status_resp.json().get('result', {})
        if torrent_hash in status_result:
            save_path = status_result[torrent_hash].get('save_path', '')
            if not save_path.startswith(dest):
                log.error(f'Move verification failed: {torrent_hash} did not land at expected destination {dest}')
                record_activity('supersede-move-failed', f'torrent {torrent_hash} move to {dest} failed')
    except Exception as e:
        log.warning(f'Failed to verify move for {torrent_hash}: {e}')

def supersede_torrent(torrent_hash):
    set_torrent_label(torrent_hash, SUPERSEDED_LABEL)
    move_torrent_storage(torrent_hash, SEEDING_DIR)

def remove_torrent(torrent_hash, remove_data=True):
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.remove_torrent', 'params': [torrent_hash, remove_data], 'id': 7},
        timeout=30
    )
    resp.raise_for_status()
    log.info(f'Removed torrent {torrent_hash}' + (' and deleted files' if remove_data else ' (files left on disk)'))

def get_all_torrents():
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={
            'method': 'core.get_torrents_status',
            'params': [{}, ['name', 'label', 'save_path', 'seeding_time', 'progress', 'state', 'total_uploaded', 'total_seeds', 'tracker_status']],
            'id': 6
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json().get('result', {})

# ── Hit-and-run protection ───────────────────────────────────────────────────

# Phrases a tracker uses to say a torrent is no longer in its database. Once
# that's true the torrent can't accrue seeding credit and can't cause a
# hit-and-run, so deleting it is free. Until then it can, whatever else is
# true about it.
UNREGISTERED_MARKERS = (
    'unregistered',
    'not registered',
    'torrent not found',
    'infohash not found',
    'torrent does not exist',
)


def torrent_is_unregistered(info):
    """True only when the tracker has affirmatively said it doesn't know this
    torrent. Unknown / empty / transient-error statuses return False: the
    fail-safe direction is 'keep seeding', because the cost of being wrong is
    a hit-and-run on the tracker and the cost of being right late is disk."""
    status = (info.get('tracker_status') or '').lower()
    return any(marker in status for marker in UNREGISTERED_MARKERS)


def should_hard_delete_on_upgrade(info, same_group):
    """True only when deleting a superseded torrent's DATA outright is safe:
    same release group AND the tracker has unregistered it AND its seed
    obligation is met (seeding_time >= SEED_DAYS * 86400 seconds). Otherwise the
    caller MUST soft-supersede (keep seeding) — a private tracker routinely
    unregisters a superseded torrent the moment the repack is posted while still
    enforcing its minimum seed time (The Diplomat S03E02, deleted same-day
    2026-09-01 -> hit-and-run). 'unregistered' != 'seed obligation waived'."""
    if not same_group:
        return False
    if not torrent_is_unregistered(info):
        return False
    return (info.get('seeding_time') or 0) >= SEED_DAYS * 86400


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

# Trailing release-group token, e.g. "...WEB.H264-GROUP" -> "GROUP". Strips a
# trailing tracker/site tag in brackets first (e.g. "-GROUP[TGx]") so that
# doesn't get mistaken for the group. Exact-match only, no fuzzy comparison —
# a false "same group" match is the only dangerous direction here (wrong
# delete), so when the group can't be confidently determined this returns
# None and the caller falls back to the safe supersede path.
RELEASE_GROUP_RE = re.compile(r'-([A-Za-z0-9]+)$')

def extract_release_group(name):
    if not name:
        return None
    stripped = re.sub(r'(\[[^\]]*\])+$', '', name).strip()
    m = RELEASE_GROUP_RE.search(stripped)
    return m.group(1).lower() if m else None

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

def queued_superseded_targets(torrents):
    """Superseded torrents sitting in Deluge's own 'Queued' state — waiting
    behind the active-torrent limit, not transferring, not seeding, nothing
    to lose by removing them now. (Deluge can hold a torrent here at any
    progress %, so this checks state, not progress.) Shared by the
    /purge-unstarted-superseded route and the post-import sweep."""
    return [
        {'hash': h, 'name': info.get('name'), 'progress': info.get('progress'), 'state': info.get('state')}
        for h, info in torrents.items()
        if info.get('label') == SUPERSEDED_LABEL and info.get('state') == 'Queued'
        # "Nothing to lose" holds only while nothing has been downloaded. A
        # Queued torrent sitting at partial or full progress HAS taken data
        # from the tracker and can still owe seed time, so purging it is a
        # hit-and-run in the same way the same-group delete was. Once the
        # tracker has dropped the torrent it can't owe anything, so that
        # releases the brake.
        and ((info.get('progress') or 0) == 0 or torrent_is_unregistered(info))
    ]

def purge_queued_superseded(targets):
    removed, failed = 0, []
    for t in targets:
        try:
            remove_torrent(t['hash'])
            removed += 1
        except Exception as e:
            failed.append({'hash': t['hash'], 'error': str(e)})
    return removed, failed

def cleanup_scheduler():
    while True:
        cleanup_superseded()
        cleanup_radarr_queue_dupes()
        cleanup_sonarr_queue_dupes()
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

def _strip_release_ext(s):
    for ext in ('.mkv', '.mp4', '.avi'):
        if s.endswith(ext):
            return s[:-len(ext)]
    return s

# Containment matching is only meaningful when BOTH sides are substantial.
# Confirmed live 2026-08-23: with no floor on the *entry* side, `_find_torrent_data`
# walked into macOS app bundles and matched two-letter locale directories against
# release names -- `en` inside "Halloween.Ends", `vi` inside "DoVi", `de` inside
# "Deepwater", `ms` inside "Backrooms". It proposed relinking eight UHD torrents to
# FileZilla's and CHIRP's locale folders and reported `missing: 0`, when in fact
# none of those torrents had any data on disk at all. The old `len(stem) >= 20`
# guard measured only the torrent name, which is always long, so it never fired.
_MIN_CONTAINMENT_LEN = 20

def _torrent_name_matches_file(torrent_name, tracked_relative_path):
    """True if the torrent name looks like the tracked file (fuzzy match on
    name minus extension). Radarr's relativePath is like 'Movie 2022...mkv';
    the Deluge torrent name may lack the extension or match exactly.

    An exact stem match always counts. Containment in either direction requires
    both sides to be at least _MIN_CONTAINMENT_LEN characters, so a short
    directory or file name can never match an arbitrary release."""
    if not tracked_relative_path:
        return False
    name = _strip_release_ext(torrent_name.lower())
    tracked = _strip_release_ext(tracked_relative_path.lower())
    if name == tracked:
        return True
    if len(name) < _MIN_CONTAINMENT_LEN or len(tracked) < _MIN_CONTAINMENT_LEN:
        return False
    return tracked in name or name in tracked

def _sonarr_series_imported_download_ids(series_id):
    """Set of lowercase downloadIds Sonarr has actually imported for this
    series, across all episodes — unambiguous keepers regardless of
    filename. Needed because Sonarr renames imported files to its own
    naming scheme, which often doesn't fuzzy-match the raw release name
    at all (confirmed live 2026-07-30: House of the Dragon S03E06 —
    filename matching against episodefile.relativePath never matched any
    of 3 duplicate torrents, so they never even formed a comparison group).
    eventType=3 is DownloadFolderImported (NzbDrone.Core.History.
    EpisodeHistoryEventType), confirmed against Sonarr's own source."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id, 'eventType': 3},
            timeout=15,
        )
        r.raise_for_status()
        return {(e.get('downloadId') or '').lower() for e in r.json() if e.get('downloadId')}
    except Exception as e:
        log.warning(f'Sonarr history lookup failed for series {series_id}: {e}')
        return set()

def _sonarr_keeper_pack_info(series_id):
    """Ask Sonarr which download is the CURRENT keeper for each episode (the
    MOST-RECENT DownloadFolderImported per episodeId, eventType=3) and derive
    the two facts the season-pack ⇄ singles decision needs:

      keeper_ids          — set of lowercase downloadIds that are the current
                            keeper for at least one episode. A single whose OWN
                            downloadId is in here is the live file for its
                            episode (e.g. a repack imported AFTER a pack) and
                            must be spared.
      keeper_pack_seasons — set of season numbers whose current keeper is a
                            season pack / multi-episode file.

    How a pack is detected: a season-pack import records a DownloadFolderImported
    event for EACH episode it delivered, all sharing the SAME downloadId, so the
    keeper downloadId for those episodes covers >= 2 episodes. A single covers
    exactly one. (The event's sourceTitle can NOT be used — Sonarr writes a
    per-episode SxxExx sourceTitle even for a pack import, confirmed live on The
    Agency S02 where all ten events carry the pack's downloadId but per-episode
    S02Exx titles.) A 2-parter multi-ep file trips the same >=2 rule, which is
    fine: it too legitimately replaced the singles for the episodes it covers.

    Coverage is derived from Sonarr's history, NOT from a Deluge torrent, on
    purpose: the pack is the authoritative keeper even when its own torrent is
    gone from the sonarr-labeled set — the real The Agency case (pack imported,
    pack torrent absent, ten singles still seeding). Distinct from
    _sonarr_series_imported_download_ids (flat historical set), which still
    contains the OLD singles a pack replaced and so cannot be used here."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id, 'eventType': 3},
            timeout=15,
        )
        r.raise_for_status()
        latest = {}  # episodeId -> (date_str, downloadId_lower)
        for e in r.json():
            did = (e.get('downloadId') or '').lower()
            if not did:
                continue
            ep_id = e.get('episodeId')
            date = e.get('date') or ''
            prev = latest.get(ep_id)
            if prev is None or date >= prev[0]:
                latest[ep_id] = (date, did)
        keeper_ids = {did for _, did in latest.values()}
        # Count episodes each keeper downloadId currently holds; a pack holds >=2.
        ep_count = {}
        for _, did in latest.values():
            ep_count[did] = ep_count.get(did, 0) + 1
        pack_dids = {did for did, n in ep_count.items() if n >= 2}
        if not pack_dids:
            return keeper_ids, set()
        # Map episodeId -> seasonNumber to name the pack-kept seasons.
        ep_season = {}
        try:
            er = requests.get(
                f'{SONARR_URL}/api/v3/episode',
                headers={'X-Api-Key': SONARR_API_KEY},
                params={'seriesId': series_id},
                timeout=20,
            )
            er.raise_for_status()
            ep_season = {ep.get('id'): ep.get('seasonNumber') for ep in er.json()}
        except Exception as ee:
            log.warning(f'Sonarr episode lookup failed for series {series_id}: {ee}')
            return keeper_ids, set()
        keeper_pack_seasons = set()
        for ep_id, (_, did) in latest.items():
            if did in pack_dids:
                season = ep_season.get(ep_id)
                if season is not None:
                    keeper_pack_seasons.add(int(season))
        return keeper_ids, keeper_pack_seasons
    except Exception as e:
        log.warning(f'Sonarr keeper lookup failed for series {series_id}: {e}')
        return set(), set()

def _sonarr_keeper_single_seasons(series_id):
    """Ask Sonarr which download is the CURRENT keeper for each episode (the
    MOST-RECENT DownloadFolderImported per episodeId, eventType=3) and return
    the set of season numbers whose current keeper is a SINGLE — i.e. a
    downloadId that covers EXACTLY ONE episode.

    Mirror of _sonarr_keeper_pack_info with the pack rule inverted: there a
    keeper downloadId covering >= 2 episodes means "pack kept this season";
    here a keeper downloadId covering exactly one episode is POSITIVE evidence
    that Sonarr keeps this season via singles (each episode imported by its own
    single-episode import). That positive evidence is what lets the inverse
    supersede pass safely touch a redundant season-PACK torrent: a season with
    NO single keepers may simply have nothing imported at all, in which case
    the pack could be the ONLY copy and must never be touched.

    Same history+episode fetch pattern as _sonarr_keeper_pack_info (latest per
    episodeId from /api/v3/history/series eventType=3, then /api/v3/episode for
    episodeId -> seasonNumber). Blank downloadIds are skipped. Returns set() on
    any failure — fail safe: no evidence of single keepers means the caller
    selects nothing."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id, 'eventType': 3},
            timeout=15,
        )
        r.raise_for_status()
        latest = {}  # episodeId -> (date_str, downloadId_lower)
        for e in r.json():
            did = (e.get('downloadId') or '').lower()
            if not did:
                continue
            ep_id = e.get('episodeId')
            date = e.get('date') or ''
            prev = latest.get(ep_id)
            if prev is None or date >= prev[0]:
                latest[ep_id] = (date, did)
        if not latest:
            return set()
        # Count episodes each keeper downloadId currently holds; a single holds exactly 1.
        ep_count = {}
        for _, did in latest.values():
            ep_count[did] = ep_count.get(did, 0) + 1
        single_dids = {did for did, n in ep_count.items() if n == 1}
        if not single_dids:
            return set()
        # Map episodeId -> seasonNumber to name the single-kept seasons.
        er = requests.get(
            f'{SONARR_URL}/api/v3/episode',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id},
            timeout=20,
        )
        er.raise_for_status()
        ep_season = {ep.get('id'): ep.get('seasonNumber') for ep in er.json()}
        seasons = set()
        for ep_id, (_, did) in latest.items():
            if did in single_dids:
                season = ep_season.get(ep_id)
                if season is not None:
                    seasons.add(int(season))
        return seasons
    except Exception as e:
        log.warning(f'Sonarr keeper-single lookup failed for series {series_id}: {e}')
        return set()

def _sonarr_latest_keeper_by_episode_key(series_id):
    """Map each episode of this series to the downloadId Sonarr most recently
    imported for it — { "S04E03": "<downloadId lowercased>", ... }.

    Same "latest per episodeId" logic as _sonarr_keeper_pack_info (GET
    /api/v3/history/series with eventType=3, keep the row with max `date` per
    episodeId, downloadId lowercased, blank downloadIds skipped), but keyed by
    SxxExx instead of downloadId. This is what lets dedup_via_sonarr's
    per-episode pass pick a keeper when an episode was imported twice (an
    upgrade): the flat set from _sonarr_series_imported_download_ids contains
    BOTH downloads and can't choose between them, but the latest import is
    unambiguous.

    Returns {} on any failure — fail safe: the caller falls back to its
    existing history/filename keeper logic."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id, 'eventType': 3},
            timeout=15,
        )
        r.raise_for_status()
        latest = {}  # episodeId -> (date_str, downloadId_lower)
        for e in r.json():
            did = (e.get('downloadId') or '').lower()
            if not did:
                continue
            ep_id = e.get('episodeId')
            date = e.get('date') or ''
            prev = latest.get(ep_id)
            if prev is None or date >= prev[0]:
                latest[ep_id] = (date, did)
        if not latest:
            return {}
        er = requests.get(
            f'{SONARR_URL}/api/v3/episode',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id},
            timeout=20,
        )
        er.raise_for_status()
        out = {}
        for ep in er.json():
            did = latest.get(ep.get('id'))
            if not did:
                continue
            season = ep.get('seasonNumber')
            number = ep.get('episodeNumber')
            if season is None or number is None:
                continue
            out[f'S{int(season):02d}E{int(number):02d}'] = did[1]
        return out
    except Exception as e:
        log.warning(f'Sonarr latest-keeper lookup failed for series {series_id}: {e}')
        return {}

def _sonarr_latest_source_title_by_episode_key(series_id):
    """Map each episode of this series to the sourceTitle Sonarr most recently
    imported for it — { "S10E08": "<sourceTitle>", ... }.

    Mirror of _sonarr_latest_keeper_by_episode_key with ONE difference: keep
    the newest import's `sourceTitle` per episode REGARDLESS of a blank
    downloadId. That is the whole point — Sonarr history events carry
    sourceTitle (the raw release name) even when downloadId is blank, and
    those rows are exactly the ones _sonarr_latest_keeper_by_episode_key
    drops (confirmed live: MAFS UK S10E08). Same "latest per episodeId"
    logic: GET /api/v3/history/series with eventType=3, keep the row with max
    `date` per episodeId, then map episodeId → SxxExx via /api/v3/episode.

    Returns {} on any failure — fail safe: the caller falls back to its
    existing history/filename keeper logic."""
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id, 'eventType': 3},
            timeout=15,
        )
        r.raise_for_status()
        latest = {}  # episodeId -> (date_str, sourceTitle)
        for e in r.json():
            st = e.get('sourceTitle')
            if not isinstance(st, str) or not st:
                continue
            ep_id = e.get('episodeId')
            date = e.get('date') or ''
            prev = latest.get(ep_id)
            if prev is None or date >= prev[0]:
                latest[ep_id] = (date, st)
        if not latest:
            return {}
        er = requests.get(
            f'{SONARR_URL}/api/v3/episode',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'seriesId': series_id},
            timeout=20,
        )
        er.raise_for_status()
        out = {}
        for ep in er.json():
            st = latest.get(ep.get('id'))
            if not st:
                continue
            season = ep.get('seasonNumber')
            number = ep.get('episodeNumber')
            if season is None or number is None:
                continue
            out[f'S{int(season):02d}E{int(number):02d}'] = st[1]
        return out
    except Exception as e:
        log.warning(f'Sonarr latest-sourceTitle lookup failed for series {series_id}: {e}')
        return {}

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
                # Still-downloading torrents can't be "superseded" — there's
                # nothing to compare against yet, and Radarr's import history
                # only ever points at the OLD file for a movie that already
                # hasFile. Without this, an in-flight upgrade grab gets
                # mislabeled superseded (and starved) before it can ever
                # finish and become the real keeper. Confirmed live 2026-08-03:
                # Avengers Infinity War + Man of Steel upgrade downloads both
                # got tagged superseded mid-transfer by this pass.
                if (info.get('progress') or 0) < 99.0:
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
                    supersede_torrent(h)
                relabeled += 1
        log.info(f'Radarr dedup complete{" (DRY RUN)" if dry_run else ""}: {"would relabel" if dry_run else "relabeled"} {relabeled} superseded torrent(s)')
        if relabeled and not dry_run:
            record_activity('dedup', f'Radarr dedup: relabeled {relabeled} duplicate torrent(s) as superseded')
    except Exception as e:
        log.error(f'Radarr dedup failed: {e}')

def select_singles_superseded_by_pack(torrent_names, keeper_ids, keeper_pack_seasons):
    """Pure decision for the season-pack ⇄ singles supersede case.

    A season pack (e.g. "The.Agency.2024.S02.1080p...-Riyadh") carries no
    SxxExx token, so it never joins an episode group in dedup_via_sonarr's
    per-episode logic. Without this, the individual-episode singles the pack
    replaced (which DO carry SxxExx) each land alone in their own group of
    length 1 and are never superseded — they keep seeding forever alongside
    the pack that made them redundant.

    Args:
        torrent_names: {hash: release_name} for the series' sonarr-labeled
            torrents already filtered to progress >= 99% and title-matched.
        keeper_ids: set of lowercase downloadIds that are the CURRENT keeper for
            at least one episode (latest DownloadFolderImported per episode,
            from _sonarr_keeper_pack_info). A single whose OWN hash is in here is
            the live file for its episode and is spared. NOTE: must be the
            latest-per-episode set, NOT the flat historical set from
            _sonarr_series_imported_download_ids — an OLD single a pack replaced
            is still in the flat set and using it here would spare exactly the
            singles we must supersede.
        keeper_pack_seasons: set of season numbers whose CURRENT keeper is a
            season pack, from _sonarr_keeper_pack_info. Derived from Sonarr's
            import history (not from a Deluge torrent) so a pack that has already
            finished importing still counts even when its own torrent is no
            longer in the sonarr-labeled Deluge set — the real The Agency case.

    Returns the list of single-episode hashes that a keeper season pack covers
    and that are NOT themselves the current keeper — safe to soft-supersede.
    Strictly scoped:

      * Only seasons whose live keeper is a pack (keeper_pack_seasons) are ever
        considered; a season Sonarr still keeps via singles yields nothing.
      * Only individual-episode torrents (EPISODE_RE present) are ever selected;
        packs are never returned, so pack-vs-pack is untouched.
      * A single that is itself the current keeper for its episode is never
        selected — the REPACK caveat (a repack single imported AFTER the pack is
        the keeper for its episode and survives).
      * A season with only singles (no keeper pack) yields nothing — current
        behavior for that season is preserved exactly.
    """
    seasons = set(keeper_pack_seasons)
    if not seasons:
        return []
    keepers = {(d or '').lower() for d in keeper_ids}
    selected = []
    for h, name in torrent_names.items():
        name = name or ''
        em = EPISODE_RE.search(name)
        if not em:
            continue  # only individual-episode torrents are candidates
        season = int(em.group(0)[1:3])
        if season not in seasons:
            continue  # no covering keeper pack for this episode's season
        if h.lower() in keepers:
            continue  # this single is itself the current keeper -> never touch
        selected.append(h)
    return selected

def select_pack_superseded_by_singles(torrent_names, keeper_ids, keeper_single_seasons):
    """Pure decision for the INVERSE season-pack ⇄ singles supersede case.

    When Sonarr's current keepers for a season are the SINGLES (each episode
    kept by its own single-episode import) but a season-PACK torrent is still
    seeding in the sonarr-labeled Deluge set, that pack is redundant — it seeds
    forever because it carries no SxxExx token and so never joins an episode
    group. This selects those packs for soft-supersede.

    Args:
        torrent_names: {hash: release_name} for the series' sonarr-labeled
            torrents already filtered to progress >= 99% and title-matched.
        keeper_ids: set of lowercase downloadIds that are the CURRENT keeper for
            at least one episode (from _sonarr_keeper_pack_info). A pack whose
            OWN hash is in here is itself the live file and must be spared.
        keeper_single_seasons: set of season numbers with POSITIVE evidence of
            >= 1 single-episode current keeper, from
            _sonarr_keeper_single_seasons. This is the safety key: a season
            merely ABSENT from keeper_pack_seasons could mean nothing was
            imported for it at all (the pack might be the only copy), so we
            never touch a pack without positive single-keeper evidence.

    Returns the list of redundant season-PACK hashes — safe to soft-supersede.
    Strictly scoped:

      * A candidate is a pack iff SEASON_RE matches AND EPISODE_RE does NOT;
        individual-episode torrents are NEVER returned, so singles-vs-singles
        is untouched (that's the per-episode pass's job).
      * Only packs in seasons present in keeper_single_seasons are ever
        selected — a pack in any other season is never touched.
      * A pack that is itself the current keeper for its episodes is never
        selected.
      * Empty keeper_single_seasons -> [] (no evidence, no action).
    """
    seasons = set(keeper_single_seasons)
    if not seasons:
        return []
    keepers = {(d or '').lower() for d in keeper_ids}
    selected = []
    for h, name in torrent_names.items():
        name = name or ''
        sm = SEASON_RE.search(name)
        if not sm:
            continue  # no season token -> can't be a season pack
        if EPISODE_RE.search(name):
            continue  # individual-episode torrents are never selected here
        season = int(sm.group(1))
        if season not in seasons:
            continue  # no single-keeper evidence for this season -> may be the only copy
        if h.lower() in keepers:
            continue  # this pack is itself the current keeper -> never touch
        selected.append(h)
    return selected

def select_episode_dupe_losers(ep_key, hashes, latest_keeper_map):
    """Given the SxxExx key, the list of torrent hashes matched to that episode,
    and {ep_key: latest_keeper_downloadId_lower}, return the hashes to supersede
    (everything that is NOT the latest-import keeper). Returns None to signal
    'cannot decide from latest-import history, fall back' when: no entry for
    ep_key, OR the latest keeper downloadId is not present exactly once among
    `hashes`. NEVER returns the keeper hash. Case-insensitive on hashes."""
    keeper_did = (latest_keeper_map or {}).get(ep_key)
    if not keeper_did:
        return None
    keeper_hashes = [h for h in hashes if h.lower() == keeper_did]
    if len(keeper_hashes) != 1:
        return None
    keeper = keeper_hashes[0]
    return [h for h in hashes if h != keeper]


def select_episode_keeper_by_source_title(hashes, torrent_names, source_title):
    """Given the candidate torrent hashes for one episode, {hash: release_name},
    and the newest import's sourceTitle for that episode (may be None), return
    the SINGLE hash whose name — extension-stripped via _strip_release_ext and
    lowercased — equals the same treatment of `source_title`.

    Returns None (fall back, never guess) when: `source_title` is falsy or not
    a string; zero hashes match; or MORE than one hash matches. Case-insensitive.
    Never raises on a non-string/None sourceTitle or a missing name entry."""
    if not isinstance(source_title, str) or not source_title:
        return None
    target = _strip_release_ext(source_title.lower())
    names = torrent_names or {}
    matches = [h for h in hashes if _strip_release_ext((names.get(h) or '').lower()) == target]
    if len(matches) != 1:
        return None
    return matches[0]


def dedup_via_sonarr(dry_run=False):
    """Sonarr → Deluge dedup pass. When dry_run is True nothing is mutated
    (no supersede_torrent, no label/move, no record_activity) — it only logs
    and RETURNS a report list of the season-pack ⇄ singles decisions it would
    make: one dict per would-be-superseded single, plus the keeper pack that
    covers it. Live-safe read-only smoketest entry."""
    report = []  # season-pack pass decisions, populated in dry_run for review
    log.info(f'Running Sonarr → Deluge dedup pass{" (DRY RUN)" if dry_run else ""}...')
    if not SONARR_API_KEY:
        log.info('  no SONARR_API_KEY, skip')
        return report
    try:
        deluge_login()
        ensure_label_exists()
        torrents = get_all_torrents()
        sonarr_torrents = {h: i for h, i in torrents.items() if i.get('label') in ('sonarr', SONARR_UPG_LABEL)}
        if not sonarr_torrents:
            log.info('  no sonarr-labeled torrents to check')
            return report
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
            # Match torrents against this series, then group by which
            # EPISODE each one is for (parsed straight from the release
            # name via EPISODE_RE) — not by fuzzy-matching against Sonarr's
            # tracked episodefile.relativePath. Sonarr renames imported
            # files to its own naming scheme, which frequently doesn't
            # fuzzy-match the raw release name at all, so grouping by
            # tracked-path match silently drops real duplicates before
            # they're ever compared (confirmed live 2026-07-30: House of
            # the Dragon S03E06 — none of 3 duplicate torrents matched
            # episodefile.relativePath, so the old series-wide check AND an
            # earlier per-tracked-path-grouped attempt both silently no-opped).
            # Excludes in-flight torrents (progress < 99%) — same reasoning
            # as the Radarr pass above: an upgrade still downloading can't be
            # a duplicate of the file already on disk, and history/filename
            # matching can never identify it as the keeper before it imports.
            matched_hashes = [
                h for h, info in sonarr_torrents.items()
                if torrent_matches_any_title(info.get('name', ''), title_variants)
                and (info.get('progress') or 0) >= 99.0
            ]
            if not matched_hashes:
                continue
            # Sonarr's own import history (downloadIds it actually used) — the
            # flat, append-only keeper set. Used by the per-episode pass below,
            # exactly as before.
            imported_ids = _sonarr_series_imported_download_ids(series_id)
            latest_keeper_map = _sonarr_latest_keeper_by_episode_key(series_id)
            # sourceTitle of the newest import per episode — fallback keeper
            # signal for episodes whose kept import has a blank downloadId
            # (both downloadId methods above drop those rows).
            source_title_map = _sonarr_latest_source_title_by_episode_key(series_id)
            # Season-pack ⇄ singles pass. A season pack has no SxxExx token, so
            # it never joins an episode group below; without this the singles it
            # replaced keep seeding forever. Coverage is decided from Sonarr's
            # import history (keeper_pack_seasons = seasons whose CURRENT keeper
            # is a pack), NOT from a pack torrent being present in Deluge — the
            # real The Agency case had the pack imported and gone from the
            # sonarr-labeled set while the 10 singles kept seeding. A single that
            # is itself the current keeper (repack imported after the pack) is
            # spared. Seasons Sonarr still keeps via singles are left untouched.
            keeper_ids, keeper_pack_seasons = _sonarr_keeper_pack_info(series_id)
            matched_names = {h: sonarr_torrents[h].get('name', '') for h in matched_hashes}
            superseded_by_pack = set(
                select_singles_superseded_by_pack(matched_names, keeper_ids, keeper_pack_seasons)
            )
            for h in superseded_by_pack:
                name = sonarr_torrents[h].get('name', '') or ''
                em = EPISODE_RE.search(name)
                season = int(em.group(0)[1:3]) if em else None
                action = 'WOULD relabel' if dry_run else 'relabeling'
                log.info(f'  {action} superseded (season pack covers it): "{name}" (series {series_id}: {series.get("title")}, S{season:02d})')
                report.append({
                    'series_id': series_id,
                    'series': series.get('title'),
                    'season': season,
                    'single_hash': h,
                    'single_name': name,
                })
                if not dry_run:
                    supersede_torrent(h)
                relabeled += 1
            # Inverse case (Make That Movie S01): Sonarr's current keepers for a
            # season are the SINGLES but a season-PACK torrent is still seeding in
            # the sonarr-labeled set — that pack is redundant and would seed
            # forever. Keyed on POSITIVE evidence of single keepers (a season
            # merely absent from keeper_pack_seasons could have nothing imported
            # at all, where the pack might be the only copy), so a pack in any
            # other season is never touched.
            keeper_single_seasons = _sonarr_keeper_single_seasons(series_id)
            superseded_redundant_packs = set(
                select_pack_superseded_by_singles(matched_names, keeper_ids, keeper_single_seasons)
            )
            for h in superseded_redundant_packs:
                name = sonarr_torrents[h].get('name', '') or ''
                sm = SEASON_RE.search(name)
                season = int(sm.group(1)) if sm else None
                action = 'WOULD relabel' if dry_run else 'relabeling'
                log.info(f'  {action} superseded (singles are the keepers): "{name}" (series {series_id}: {series.get("title")}, S{season:02d})')
                report.append({
                    'series_id': series_id,
                    'series': series.get('title'),
                    'season': season,
                    'pack_hash': h,
                    'pack_name': name,
                })
                if not dry_run:
                    supersede_torrent(h)
                relabeled += 1
            # Skip these packs in the per-episode grouping below (they carry no
            # SxxExx token anyway, but keep the skip set explicit).
            superseded_by_pack.update(superseded_redundant_packs)
            by_episode = {}
            for h in matched_hashes:
                if h in superseded_by_pack:
                    continue  # already handled by the season-pack pass
                name = sonarr_torrents[h].get('name', '')
                m = EPISODE_RE.search(name)
                if not m:
                    continue  # can't tell which episode this is, leave it alone
                by_episode.setdefault(m.group(0).upper(), []).append(h)
            multi = {ep: hashes for ep, hashes in by_episode.items() if len(hashes) > 1}
            if not multi:
                continue
            # Keeper identification, in order of confidence:
            #  1. Sonarr's own import history (downloadId) — unambiguous,
            #     Sonarr told us exactly which download it actually used.
            #  2. Exact filename match against a tracked episodefile path —
            #     fallback for series where history lookup fails/is thin.
            # If neither identifies exactly one keeper, skip and warn —
            # same safety-first philosophy as before, just per-episode now.
            # (imported_ids already fetched above for the season-pack pass.)
            for ep, hashes in multi.items():
                losers = select_episode_dupe_losers(ep, hashes, latest_keeper_map)
                if losers is not None:
                    keeper = next(h for h in hashes if h not in losers)
                    log.info(f'  {ep} (series {series_id}: {series.get("title")}): keeper via sonarr-latest-history = {keeper[:12]}')
                    for h in losers:
                        action = 'WOULD relabel' if dry_run else 'relabeling'
                        log.info(f'  {action} superseded: "{sonarr_torrents[h].get("name")}" (series {series_id}: {series.get("title")}, {ep})')
                        if not dry_run:
                            supersede_torrent(h)
                        relabeled += 1
                    continue
                keeper = None
                keeper_source = None
                history_keepers = [h for h in hashes if h.lower() in imported_ids]
                if len(history_keepers) == 1:
                    keeper, keeper_source = history_keepers[0], 'sonarr-history'
                else:
                    exact = [h for h in hashes if any(
                        _strip_release_ext(sonarr_torrents[h].get('name', '').lower()) == _strip_release_ext(p)
                        for p in tracked_paths
                    )]
                    if len(exact) == 1:
                        keeper, keeper_source = exact[0], 'filename-exact'
                if keeper is None:
                    # Fallback for episodes whose kept import has a blank
                    # downloadId (both history methods above drop those rows):
                    # match the newest import's sourceTitle against the
                    # candidate release names. Only fires on exactly one match;
                    # otherwise we still skip rather than guess.
                    st_keeper = select_episode_keeper_by_source_title(
                        hashes, matched_names, source_title_map.get(ep)
                    )
                    if st_keeper is not None:
                        keeper, keeper_source = st_keeper, 'sourceTitle'
                if keeper is None:
                    log.warning(f'  skip {ep} (series {series_id}: {series.get("title")}): {len(hashes)} torrents matched, no single keeper identified via history or filename — not relabeling anything')
                    continue
                log.info(f'  {ep} (series {series_id}: {series.get("title")}): keeper via {keeper_source} = {keeper[:12]}')
                for h in hashes:
                    if h == keeper:
                        continue
                    action = 'WOULD relabel' if dry_run else 'relabeling'
                    log.info(f'  {action} superseded: "{sonarr_torrents[h].get("name")}" (series {series_id}: {series.get("title")}, {ep})')
                    if not dry_run:
                        supersede_torrent(h)
                    relabeled += 1
        log.info(f'Sonarr dedup complete{" (DRY RUN)" if dry_run else ""}: {"would relabel" if dry_run else "relabeled"} {relabeled} superseded torrent(s)')
        if relabeled and not dry_run:
            record_activity('dedup', f'Sonarr dedup: relabeled {relabeled} duplicate torrent(s) as superseded')
    except Exception as e:
        log.error(f'Sonarr dedup failed: {e}')
    return report


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
                record_activity('cleanup', f'Removed rar-torrent "{state[h].get("name")}" (aged {age/86400:.1f}d past the {SEED_DAYS}-day window)')
                remove_torrent(h)
                state.pop(h, None)
                removed += 1
        _save_unpack_state(state)
        log.info(f'Unpacked-torrent cleanup complete: removed {removed}')
        if removed:
            record_activity('cleanup', f'Removed {removed} unpacked RAR torrent(s) past the 21-day window')
    except Exception as e:
        log.error(f'Unpacked-torrent cleanup failed: {e}')


def _load_seed_state():
    try:
        with open(SEED_STATE_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def _save_seed_state(state):
    try:
        with open(SEED_STATE_PATH, 'w') as f:
            _json.dump(state, f)
    except Exception as e:
        log.warning(f'[seed-tracking] failed to persist state: {e}')

def cleanup_stalled_seeds(dry_run=False):
    """Weekly review of every labeled torrent currently seeding (unlabeled/
    unmanaged torrents are skipped -- this app doesn't know what they are).
    This explicitly includes LIBRARY_SEED_LABEL: those are the intended
    target -- old library-seed torrents sitting quiet for months where the
    seeded file IS the live Plex media. If a torrent has seeded at least
    STALL_SEED_MIN_DAYS and uploaded less than STALL_UPLOAD_THRESHOLD_BYTES
    since the last weekly check, AND the swarm already has more than
    STALL_MIN_SWARM_SEEDS other seeds (tracker's total_seeds), it's dead
    weight -- unseed it. First time a torrent is seen it only gets a
    baseline recorded (no prior week to compare against, so no removal). If
    total_seeds is unavailable (tracker hasn't scraped yet, reported as
    -1/missing), the swarm size is unknown and the torrent is NOT removed —
    missing data means "don't know", never "safe to remove".

    This only unseeds (remove_data=False) -- Deluge stops tracking/seeding
    the torrent but the file(s) on disk are left completely untouched. For
    library-seed torrents the file is the live media, so it must survive.

    dry_run=True reports what WOULD be removed without removing anything
    and without touching the persisted state file (so a preview run
    doesn't consume/reset the real week-over-week baseline). Returns a
    list of candidate dicts for the caller (e.g. the preview endpoint)."""
    log.info(f'Running weekly stalled-seed review{" (DRY RUN)" if dry_run else ""}...')
    candidates = []
    try:
        deluge_login()
        torrents = get_all_torrents()
        state = _load_seed_state()
        removed = 0
        for h, info in torrents.items():
            if not (info.get('label') or '').strip():
                continue  # unlabeled/unmanaged torrent -- not ours to touch
            if info.get('state') in ('Queued', 'Downloading', 'Checking', 'Allocating'):
                continue
            uploaded = info.get('total_uploaded', 0)
            prev = state.get(h)
            if not dry_run:
                state[h] = {'total_uploaded': uploaded}
            if prev is None:
                continue
            if info.get('seeding_time', 0) < STALL_SEED_MIN_DAYS * 86400:
                continue
            gained = uploaded - prev.get('total_uploaded', 0)
            if gained >= STALL_UPLOAD_THRESHOLD_BYTES:
                continue
            swarm_seeds = info.get('total_seeds')
            # libtorrent reports -1 (or the key may be absent) when the
            # tracker hasn't scraped yet -- unknown, not zero. Fail safe.
            if swarm_seeds is None or swarm_seeds < 0 or swarm_seeds <= STALL_MIN_SWARM_SEEDS:
                log.info(f'[seed-tracking] skipping stalled candidate (swarm seeds unknown/low, '
                         f'safety gate): {info.get("name")} (total_seeds={swarm_seeds})')
                continue
            action = 'WOULD unseed' if dry_run else 'unseeding (files kept on disk)'
            log.info(f'[seed-tracking] {action} stalled seed: {info.get("name")} '
                     f'(+{gained/1e6:.1f}MB uploaded since last week\'s check, '
                     f'{swarm_seeds} other swarm seeds)')
            candidates.append({
                'hash': h,
                'name': info.get('name'),
                'label': info.get('label'),
                'seeding_days': round(info.get('seeding_time', 0) / 86400, 1),
                'uploaded_since_last_check_mb': round(gained / 1e6, 1),
                'swarm_seeds': swarm_seeds,
            })
            if not dry_run:
                remove_torrent(h, remove_data=False)
                state.pop(h, None)
            removed += 1
        if not dry_run:
            for h in list(state.keys()):
                if h not in torrents:
                    state.pop(h, None)
            _save_seed_state(state)
        log.info(f'Stalled-seed review complete{" (DRY RUN)" if dry_run else ""}: '
                 f'{"would remove" if dry_run else "removed"} {removed}')
        if removed and not dry_run:
            record_activity('cleanup', f'Unseeded {removed} stalled torrent(s), files left on disk (≥{STALL_SEED_MIN_DAYS}d seeded, negligible upload since last week, >{STALL_MIN_SWARM_SEEDS} other swarm seeds)')
    except Exception as e:
        log.error(f'Stalled-seed review failed: {e}')
    return candidates

def queue_dupe_cleanup_scheduler():
    """Once a week, remove duplicate Radarr/Sonarr downloads of the same
    movie or episodes, keeping the best one (ties broken by keeping whichever
    was queued first). Added 2026-08-22 after investigating repeated re-grabs
    from monthly_upgrade_cycle's bulk upgrade searches -- those are expected
    (the search is designed to keep chasing better releases over time), but
    the brief multi-entry window they create in the queue was only ever
    cleaned up on-demand by cleanup_radarr_queue_dupes(), never on a
    schedule of its own."""
    while True:
        time.sleep(7 * 24 * 3600)  # weekly
        cleanup_radarr_queue_dupes()
        cleanup_sonarr_queue_dupes()


def stalled_seed_scheduler():
    if not STALL_AUTOMATION_ENABLED:
        log.info('Stalled-seed automation disabled (STALL_AUTOMATION_ENABLED=false) — '
                 'use GET /run-stalled-seeds to preview, or set the env var to enable weekly auto-removal')
        return
    while True:
        time.sleep(STALL_CHECK_INTERVAL)
        cleanup_stalled_seeds()


# Both queue-dupe passes below run from three places now: the daily
# cleanup_scheduler, the weekly queue_dupe_cleanup_scheduler, and -- new --
# dedupe_grabbed_release() on the Grab webhook's own thread. Two Grab
# webhooks for the same movie land seconds apart (that is the whole reason
# this feature exists), so two passes can otherwise interleave: each reads
# the candidate set before the other writes, and if their progress readings
# straddle a score tie they can pick DIFFERENT keepers and remove both
# copies. Serializing the passes makes each one read a settled world. A
# grab-time caller blocking behind a scheduled sweep costs nothing -- it is
# already a detached daemon thread and the webhook has been answered.
_queue_dupe_lock = threading.RLock()


def _serialized(fn):
    """Run fn under _queue_dupe_lock. Reentrant, so a pass may call another."""
    def wrapper(*args, **kwargs):
        with _queue_dupe_lock:
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _radarr_grab_identity(download_id):
    """(movieId, customFormatScore) for a torrent hash, from Radarr's grab
    history. Returns (None, 0) when Radarr has no grab on record.

    Needed because a throttled torrent is NOT in Radarr's queue (see the
    blind-spot note on cleanup_radarr_queue_dupes), so its movieId has to
    come from history instead of from a queue record."""
    if not (RADARR_API_KEY and download_id):
        return None, 0
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/history',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'downloadId': download_id.upper(), 'pageSize': 50},
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get('records') or []
    except Exception as e:
        log.warning(f'Radarr queue dedupe: history lookup for {download_id[:8]} failed: {e}')
        return None, 0
    grabs = [rec for rec in records if rec.get('eventType') == 'grabbed']
    if not grabs:
        return None, 0
    grabs.sort(key=lambda rec: rec.get('date') or '')
    latest = grabs[-1]
    try:
        score = int((latest.get('data') or {}).get('customFormatScore') or 0)
    except (TypeError, ValueError):
        score = 0
    return latest.get('movieId'), score


def _dupe_candidate_sort_key(c):
    """Keep the highest custom-format score. On a tie prefer whichever has
    actually pulled bytes (throwing away a part-finished download wastes the
    bandwidth already spent), then whichever was grabbed first -- queue ids
    are Radarr's auto-incrementing primary keys, and torrents with no queue
    record at all sort last."""
    return (-c['score'], -c['progress'], c['order'])


@_serialized
def cleanup_radarr_queue_dupes(movie_id=None, dry_run=False):
    """Remove duplicate downloads of the same movie, keeping the highest
    scoring one.

    Pass movie_id to restrict the comparison to a single film. That is the
    grab-time entry point (see dedupe_grabbed_release): a duplicate then
    lives for seconds instead of up to a day, and the second copy is
    dropped before it has pulled anything. Scoping narrows what is
    COMPARED, not what is read -- a lane torrent's movieId is only
    knowable from its grab history, so the candidate set is still gathered
    the same way; nothing outside the named movie's group is touched.
    Returns the number of entries removed.

    Radarr's own /api/v3/queue is NOT a complete view of what Radarr has
    grabbed, and that blind spot is what let the duplicate grabs pile up:
    Radarr's Deluge client only reports torrents carrying its configured
    category label (Deluge.GetItems -> GetTorrentsByLabel(MovieCategory)),
    so the moment this service relabels a throttled upgrade from 'radarr' to
    RADARR_UPG_LABEL -- which handle_grab does within seconds of the Grab
    webhook -- the torrent drops out of the download-client item list and
    its queue record disappears. Every throttled upgrade is therefore
    invisible here, so grouping queue records alone always found one entry
    per movie and removed nothing, exactly for the population this pass
    exists to police. (Same blind spot defeats Radarr's own
    QueueSpecification, which is why a second release for the same movie
    gets grabbed in the first place.)

    So the candidate set is the queue PLUS the in-flight torrents sitting in
    the throttled lane, whose movieId comes from Radarr's grab history.
    Losers that still have a queue record are removed through the queue API
    as before; losers that don't are relabeled SUPERSEDED_LABEL, handing
    them to the existing cleanup_superseded / queued_superseded_targets
    path rather than deleting data from under the tracker."""
    scope = f' (movie {movie_id})' if movie_id is not None else ''
    log.info(f'Running Radarr queue duplicate cleanup{scope}...')
    if not RADARR_API_KEY:
        log.info('  no RADARR_API_KEY, skip')
        return 0
    try:
        r = requests.get(
            f'{RADARR_URL}/api/v3/queue',
            headers={'X-Api-Key': RADARR_API_KEY},
            params={'pageSize': 500, 'includeUnknownMovieItems': False},
            timeout=15
        )
        r.raise_for_status()
        records = r.json().get('records', [])
    except Exception as e:
        log.error(f'Radarr queue cleanup failed: {e}')
        return 0

    # Group queue items by movieId
    by_movie = {}
    queue_hashes = set()
    for item in records:
        item_movie_id = item.get('movieId')
        if not item_movie_id:
            continue
        dl = (item.get('downloadId') or '').lower()
        if dl:
            queue_hashes.add(dl)
        size = item.get('size') or 0
        sizeleft = item.get('sizeleft')
        if sizeleft is None:
            sizeleft = size
        progress = (1 - (sizeleft / size)) * 100 if size else 0
        by_movie.setdefault(item_movie_id, []).append({
            'source': 'queue',
            'queue_id': item.get('id'),
            'hash': dl,
            'title': item.get('title'),
            'score': item.get('customFormatScore') or 0,
            'progress': progress,
            'order': item.get('id') or 0,
        })

    # Fold in the throttled lane, which the queue can't see.
    try:
        deluge_login()
        torrents = get_all_torrents()
    except Exception as e:
        log.warning(f'Radarr queue dedupe: Deluge unreachable, queue-only pass: {e}')
        torrents = {}
    for h, info in torrents.items():
        if info.get('label') != RADARR_UPG_LABEL:
            continue
        if h.lower() in queue_hashes:
            continue
        # Only in-flight downloads compete for bandwidth. A finished one is
        # the import path's business, and relabeling something that's
        # seeding would risk a hit-and-run.
        progress = info.get('progress') or 0
        if progress >= 99.0:
            continue
        lane_movie_id, score = _radarr_grab_identity(h)
        if not lane_movie_id:
            continue
        by_movie.setdefault(lane_movie_id, []).append({
            'source': 'throttled',
            'queue_id': None,
            'hash': h.lower(),
            'title': info.get('name'),
            'score': score,
            'progress': progress,
            'order': float('inf'),
        })

    if movie_id is not None:
        by_movie = {k: v for k, v in by_movie.items() if k == movie_id}

    removed = 0
    report_list = []
    for group_movie_id, items in by_movie.items():
        if len(items) <= 1:
            continue
        items.sort(key=_dupe_candidate_sort_key)
        best = items[0]
        log.info(f'Radarr queue: movie {group_movie_id} has {len(items)} entries, keeping "{best["title"]}" (score: {best["score"]})')
        for item in items[1:]:
            # Per-item try/except: one failed removal (a queue record that
            # vanished between fetch and delete, a Deluge hiccup) must not
            # abort the whole pass and leave every later movie duplicated.
            try:
                if dry_run:
                    if item['source'] == 'queue':
                        log.info(f'[dry-run] Radarr queue: would remove duplicate "{item["title"]}" (score: {item["score"]})')
                    else:
                        log.info(f'[dry-run] Radarr queue: would supersede throttled duplicate "{item["title"]}" (score: {item["score"]})')
                elif item['source'] == 'queue':
                    log.info(f'Radarr queue: removing duplicate "{item["title"]}" (score: {item["score"]})')
                    del_r = requests.delete(
                        f'{RADARR_URL}/api/v3/queue/{item["queue_id"]}',
                        headers={'X-Api-Key': RADARR_API_KEY},
                        params={'removeFromClient': True, 'blocklist': False},
                        timeout=15
                    )
                    del_r.raise_for_status()
                else:
                    log.info(f'Radarr queue: superseding throttled duplicate "{item["title"]}" (score: {item["score"]})')
                    supersede_torrent(item['hash'])
                removed += 1
            except Exception as e:
                log.warning(f'Radarr queue dedupe: could not drop "{item["title"]}": {e}')
        if dry_run:
            report_list.append({
                'movie_id': group_movie_id,
                'keep': {'title': best['title'], 'score': best['score'], 'source': best['source']},
                'would_drop': [
                    {'title': item['title'], 'score': item['score'], 'source': item['source'], 'hash': item.get('hash')}
                    for item in items[1:]
                ],
            })
    log.info(f'Radarr queue cleanup complete{scope}{" (DRY RUN)" if dry_run else ""}: '
             f'{"would remove" if dry_run else "removed"} {removed} duplicate entries')
    if removed and not dry_run:
        record_activity('dedup', f'Radarr queue: removed {removed} duplicate queue entr(y/ies)')
    return report_list if dry_run else removed


def _sonarr_grab_identity(download_id):
    """(seriesId, frozenset(episodeIds), customFormatScore) for a torrent
    hash, from Sonarr's grab history. Returns (None, frozenset(), 0) when
    Sonarr has no grab on record.

    Needed because a throttled torrent is NOT in Sonarr's queue (see the
    blind-spot note on cleanup_sonarr_queue_dupes), so its identity has to
    come from history instead of from a queue record.

    Sonarr's history is per-episode: one 'grabbed' record is written for
    EVERY episode a release covers, all sharing the one downloadId. So a
    season pack's identity is the union of those records' episodeIds --
    that union is exactly the set of episodes this torrent will deliver,
    which is what makes two grabs duplicates of each other."""
    if not (SONARR_API_KEY and download_id):
        return None, frozenset(), 0
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/history',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'downloadId': download_id.upper(), 'pageSize': 200},
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get('records') or []
    except Exception as e:
        log.warning(f'Sonarr queue dedupe: history lookup for {download_id[:8]} failed: {e}')
        return None, frozenset(), 0
    grabs = [rec for rec in records if rec.get('eventType') == 'grabbed']
    if not grabs:
        return None, frozenset(), 0
    grabs.sort(key=lambda rec: rec.get('date') or '')
    episodes = {rec.get('episodeId') for rec in grabs if rec.get('episodeId')}
    latest = grabs[-1]
    try:
        score = int((latest.get('data') or {}).get('customFormatScore') or 0)
    except (TypeError, ValueError):
        score = 0
    return latest.get('seriesId'), frozenset(episodes), score


def _sonarr_dupe_candidate_sort_key(c):
    """Keeper preference within one series, best first.

    COVERAGE comes before score, which is the one real departure from the
    Radarr key. A movie grab is all-or-nothing, but a TV grab covers a set
    of episodes, and dropping a 10-episode season pack because one
    single-episode grab inside it scores higher would leave nine episodes
    with nothing in flight and force a fresh search. Keeping the pack only
    costs a later per-episode upgrade, which Sonarr does on its own. After
    that it is the Radarr key: highest custom-format score, then whichever
    has actually pulled bytes (throwing away a part-finished download
    wastes the bandwidth already spent), then whichever was grabbed first
    -- queue ids are Sonarr's auto-incrementing primary keys, and torrents
    with no queue record at all sort last."""
    return (-len(c['episodes']), -c['score'], -c['progress'], c['order'])


@_serialized
def cleanup_sonarr_queue_dupes(series_id=None, dry_run=False):
    """Remove duplicate downloads of the same episodes, keeping the best
    covering release.

    Pass series_id to restrict the comparison to a single series. That is
    the grab-time entry point (see dedupe_grabbed_release). Note the scope
    is the SERIES, not the episode set: whether the release just grabbed is
    redundant can only be decided against every other in-flight release of
    that series -- a season pack already downloading may cover it. Inside
    the group the identity and subset rules below are unchanged, which is
    what keeps grab-time dedupe from doing what naive seriesId grouping
    does (delete a legitimate second episode). Scoping narrows what is
    COMPARED, not what is read. Returns the number of downloads removed.

    Sonarr's /api/v3/queue is NOT a complete view of what Sonarr has
    grabbed, and that blind spot is what lets duplicate grabs pile up:
    Sonarr's Deluge client only reports torrents carrying its configured
    category label (Deluge.GetItems -> GetTorrentsByLabel(TvCategory)), so
    the moment this service relabels a throttled upgrade from 'sonarr' to
    SONARR_UPG_LABEL -- which handle_grab does within seconds of the Grab
    webhook -- the torrent drops out of the download-client item list and
    its queue record disappears. Every throttled upgrade is therefore
    invisible here. (Same blind spot defeats Sonarr's own
    QueueSpecification, which is why a second release for the same episode
    gets grabbed in the first place.) This is the Sonarr counterpart of
    cleanup_radarr_queue_dupes, which had no Sonarr equivalent at all.

    Identity is where the two services genuinely differ. A movie is one
    movieId; a TV grab is a SET of episodes -- one episode, a multi-episode
    file, or a whole season pack -- so seriesId alone is not an identity at
    all (two different episodes of one series are not duplicates). Two
    grabs are duplicates only where their episode sets actually overlap,
    and a candidate is only dropped when its episode set is a SUBSET of a
    keeper's, i.e. the keeper delivers everything the loser would. Packs
    that merely straddle each other (S01E01-05 vs S01E04-10) are both
    kept: neither is redundant and dropping either loses episodes. So is a
    candidate covered only by the union of two separate keepers -- its
    episodes would then depend on two downloads both succeeding.

    Sonarr also writes one queue record PER EPISODE, so a season pack is N
    records sharing a downloadId; those are folded back into one candidate
    before anything is compared, and removed together through the queue
    bulk endpoint.

    Losers that still have a queue record are removed through the queue
    API; losers that don't are relabeled SUPERSEDED_LABEL, handing them to
    the existing cleanup_superseded / queued_superseded_targets path
    rather than deleting data from under the tracker."""
    scope = f' (series {series_id})' if series_id is not None else ''
    log.info(f'Running Sonarr queue duplicate cleanup{scope}...')
    if not SONARR_API_KEY:
        log.info('  no SONARR_API_KEY, skip')
        return 0
    try:
        r = requests.get(
            f'{SONARR_URL}/api/v3/queue',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'pageSize': 1000, 'includeUnknownSeriesItems': False},
            timeout=15
        )
        r.raise_for_status()
        records = r.json().get('records', [])
    except Exception as e:
        log.error(f'Sonarr queue cleanup failed: {e}')
        return 0

    # Fold the per-episode queue records back into one candidate per
    # download. Keyed by downloadId where there is one; a record without a
    # downloadId can't be grouped with anything, so it stands alone under
    # its own queue id.
    by_download = {}
    queue_hashes = set()
    for item in records:
        item_series_id = item.get('seriesId')
        if not item_series_id:
            continue
        dl = (item.get('downloadId') or '').lower()
        if dl:
            queue_hashes.add(dl)
        size = item.get('size') or 0
        sizeleft = item.get('sizeleft')
        if sizeleft is None:
            sizeleft = size
        progress = (1 - (sizeleft / size)) * 100 if size else 0
        key = dl or f'queue:{item.get("id")}'
        c = by_download.get(key)
        if c is None:
            c = by_download[key] = {
                'source': 'queue',
                'queue_ids': [],
                'hash': dl,
                'title': item.get('title'),
                'series_id': item_series_id,
                'episodes': set(),
                'score': 0,
                'progress': progress,
                'order': float('inf'),
            }
        if item.get('id') is not None:
            c['queue_ids'].append(item['id'])
            c['order'] = min(c['order'], item['id'])
        # episodeId is the v3 field; tolerate an episodeIds list too.
        if item.get('episodeId'):
            c['episodes'].add(item['episodeId'])
        for ep_id in (item.get('episodeIds') or []):
            if ep_id:
                c['episodes'].add(ep_id)
        c['score'] = max(c['score'], item.get('customFormatScore') or 0)
        c['progress'] = max(c['progress'], progress)

    by_series = {}
    for c in by_download.values():
        # No episodes resolved means no identity, so no way to tell whether
        # this duplicates anything. Leave it alone.
        if c['episodes']:
            by_series.setdefault(c['series_id'], []).append(c)

    # Fold in the throttled lane, which the queue can't see.
    try:
        deluge_login()
        torrents = get_all_torrents()
    except Exception as e:
        log.warning(f'Sonarr queue dedupe: Deluge unreachable, queue-only pass: {e}')
        torrents = {}
    for h, info in torrents.items():
        if info.get('label') != SONARR_UPG_LABEL:
            continue
        if h.lower() in queue_hashes:
            continue
        # Only in-flight downloads compete for bandwidth. A finished one is
        # the import path's business, and relabeling something that's
        # seeding would risk a hit-and-run.
        progress = info.get('progress') or 0
        if progress >= 99.0:
            continue
        lane_series_id, episodes, score = _sonarr_grab_identity(h)
        if not (lane_series_id and episodes):
            continue
        by_series.setdefault(lane_series_id, []).append({
            'source': 'throttled',
            'queue_ids': [],
            'hash': h.lower(),
            'title': info.get('name'),
            'series_id': lane_series_id,
            'episodes': set(episodes),
            'score': score,
            'progress': progress,
            'order': float('inf'),
        })

    if series_id is not None:
        by_series = {k: v for k, v in by_series.items() if k == series_id}

    removed = 0
    report_list = []
    for group_series_id, items in by_series.items():
        if len(items) <= 1:
            continue
        items.sort(key=_sonarr_dupe_candidate_sort_key)
        keepers, losers = [], []
        for c in items:
            covered_by = next((k for k in keepers if c['episodes'] <= k['episodes']), None)
            if covered_by is None:
                keepers.append(c)
            else:
                losers.append((c, covered_by))
        if not losers:
            continue
        log.info(f'Sonarr queue: series {group_series_id} has {len(items)} in-flight release(s), '
                 f'keeping {len(keepers)} ({", ".join(repr(k["title"]) for k in keepers)})')
        for item, keeper in losers:
            # Per-item try/except: one failed removal (a queue record that
            # vanished between fetch and delete, a Deluge hiccup) must not
            # abort the whole pass and leave every later series duplicated.
            try:
                eps = len(item['episodes'])
                if dry_run:
                    if item['source'] == 'queue':
                        log.info(f'[dry-run] Sonarr queue: would remove duplicate "{item["title"]}" '
                                 f'({eps} ep(s), score: {item["score"]}) — covered by "{keeper["title"]}"')
                    else:
                        log.info(f'[dry-run] Sonarr queue: would supersede throttled duplicate "{item["title"]}" '
                                 f'({eps} ep(s), score: {item["score"]}) — covered by "{keeper["title"]}"')
                elif item['source'] == 'queue':
                    log.info(f'Sonarr queue: removing duplicate "{item["title"]}" '
                             f'({eps} ep(s), score: {item["score"]}) — covered by "{keeper["title"]}"')
                    del_r = requests.delete(
                        f'{SONARR_URL}/api/v3/queue/bulk',
                        headers={'X-Api-Key': SONARR_API_KEY},
                        params={'removeFromClient': True, 'blocklist': False},
                        json={'ids': item['queue_ids']},
                        timeout=15
                    )
                    del_r.raise_for_status()
                else:
                    log.info(f'Sonarr queue: superseding throttled duplicate "{item["title"]}" '
                             f'({eps} ep(s), score: {item["score"]}) — covered by "{keeper["title"]}"')
                    supersede_torrent(item['hash'])
                removed += 1
            except Exception as e:
                log.warning(f'Sonarr queue dedupe: could not drop "{item["title"]}": {e}')
        if dry_run:
            report_list.append({
                'series_id': group_series_id,
                'keep': [{'title': k['title'], 'score': k['score'], 'episodes': len(k['episodes'])} for k in keepers],
                'would_drop': [
                    {'title': item['title'], 'score': item['score'], 'episodes': len(item['episodes']),
                     'source': item['source'], 'hash': item.get('hash'), 'covered_by': keeper['title']}
                    for item, keeper in losers
                ],
            })
    log.info(f'Sonarr queue cleanup complete{scope}{" (DRY RUN)" if dry_run else ""}: '
             f'{"would remove" if dry_run else "removed"} {removed} duplicate entries')
    if removed and not dry_run:
        record_activity('dedup', f'Sonarr queue: removed {removed} duplicate download(s)')
    return report_list if dry_run else removed


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

# Newly-added torrents can sit for ~15min before Deluge's own passive retry
# gets them talking to the tracker (comm errors, tracker throttling, or the
# torrent genuinely hasn't registered with the tracker yet). Deluge exposes
# `core.force_reannounce` to trigger an immediate re-announce -- this poller
# finds recently-added torrents stuck in an error/no-peer state and forces
# one, rather than waiting out Deluge's own passive retry interval.
TRACKER_STALL_INTERVAL = int(os.environ.get('TRACKER_STALL_INTERVAL', '60'))
TRACKER_STALL_WINDOW = int(os.environ.get('TRACKER_STALL_WINDOW', '900'))  # only act within 15min of add
TRACKER_STALL_MIN_GAP = 90  # don't re-force the same torrent more than once per this many seconds
_last_reannounce = {}  # torrent hash -> unix timestamp of last forced reannounce
_last_reannounce_lock = threading.Lock()

def check_tracker_stalls():
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={
            'method': 'core.get_torrents_status',
            'params': [{}, ['name', 'state', 'time_added', 'tracker_status', 'num_peers']],
            'id': 90,
        },
        timeout=10,
    )
    resp.raise_for_status()
    torrents = resp.json().get('result') or {}
    now = time.time()
    to_reannounce = []
    for torrent_hash, info in torrents.items():
        age = now - info.get('time_added', now)
        if age > TRACKER_STALL_WINDOW:
            continue
        tracker_status = (info.get('tracker_status') or '').lower()
        stalled = (
            info.get('state') == 'Error'
            or 'error' in tracker_status
            or (info.get('num_peers', 0) == 0 and 'announce ok' not in tracker_status and tracker_status != '')
        )
        if not stalled:
            continue
        with _last_reannounce_lock:
            last = _last_reannounce.get(torrent_hash, 0)
            if now - last < TRACKER_STALL_MIN_GAP:
                continue
            _last_reannounce[torrent_hash] = now
        to_reannounce.append((torrent_hash, info.get('name', torrent_hash)))

    if to_reannounce:
        session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'core.force_reannounce', 'params': [[h for h, _ in to_reannounce]], 'id': 91},
            timeout=10,
        ).raise_for_status()
        for _, name in to_reannounce:
            log.info(f'[trackerStall] forced reannounce: {name}')

    # Prune old entries so the dict doesn't grow forever.
    with _last_reannounce_lock:
        cutoff = now - TRACKER_STALL_WINDOW
        for h in [h for h, ts in _last_reannounce.items() if ts < cutoff]:
            del _last_reannounce[h]

def tracker_stall_scheduler():
    time.sleep(30)
    while True:
        try:
            deluge_login()
            check_tracker_stalls()
        except Exception as e:
            log.error(f'[trackerStall] scheduler tick failed: {e}')
        time.sleep(TRACKER_STALL_INTERVAL)

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
        new_release_group = (episode_file.get('releaseGroup') or '').lower() or None
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
        new_release_group = (movie_file.get('releaseGroup') or '').lower() or None
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
            # A repack/proper is only a true immediate replacement (safe to
            # delete outright) when it's from the SAME release group as the
            # torrent it's replacing — a repack from a different group is a
            # different release, not a guaranteed superset, so it goes
            # through the normal supersede path (soft, reversible, cleaned
            # up by cleanup_superseded after SEED_DAYS) like any other
            # quality upgrade. Group is unknown → supersede, never delete;
            # see extract_release_group / new_release_group.
            old_release_group = extract_release_group(name)
            same_group = bool(
                proper_repack and new_release_group and old_release_group
                and new_release_group == old_release_group
            )
            # Same group alone is NOT enough to delete outright. A torrent the
            # tracker still knows about can still take a hit-and-run, and this
            # branch used to remove torrents hours into their seed window --
            # four confirmed HnRs on 2026-08-25 (Fate of the Furious, Sully,
            # Rogue Nation, Disclosure Day), all deleted between 10 and 46
            # hours in. Before the seed-time gate, SEED_DAYS guarded only the
            # soft path; it now guards this one too.
            # Unregistered is necessary but NOT sufficient: a private tracker
            # will unregister a superseded torrent the moment the repack is
            # posted while still enforcing its minimum seed time (The Diplomat
            # S03E02, deleted same-day 2026-09-01 -> HnR). All three conditions
            # -- same group, unregistered, AND seed obligation met -- are
            # required; see should_hard_delete_on_upgrade.
            if should_hard_delete_on_upgrade(info, same_group):
                log.info(f'{source}: immediately deleting {torrent_hash} - {name} '
                         f'(proper/repack, same group "{new_release_group}", tracker: unregistered)')
                record_activity('supersede', f'{source}: deleted "{name}" (replaced by same-group PROPER/REPACK, unregistered at tracker)')
                remove_torrent(torrent_hash)
            else:
                if same_group:
                    reason = 'proper/repack, same group but not safe to hard-delete yet (registered, or seed time not yet met) — seeding on to avoid a hit-and-run'
                elif proper_repack:
                    reason = 'proper/repack, different or unknown group'
                else:
                    reason = 'quality upgrade'
                log.info(f'{source}: superseding {torrent_hash} - {name} ({reason})')
                record_activity('supersede', f'{source}: superseded "{name}" after upgrade import')
                supersede_torrent(torrent_hash)

    # Event-driven version of /purge-unstarted-superseded: an upgrade import
    # is exactly when stale duplicates pile up, so sweep the superseded
    # torrents that are merely Queued (not seeding, not transferring) right
    # now instead of waiting for the daily cleanup_scheduler tick. Re-fetch
    # status: the ones we just touched above are 'Moving', not 'Queued', so
    # they're correctly out of scope until they settle.
    try:
        stale = queued_superseded_targets(get_all_torrents())
        if stale:
            removed, failed = purge_queued_superseded(stale)
            log.info(f'{source}: post-import purge removed {removed} queued superseded torrent(s)')
            if removed:
                record_activity('cleanup', f'{source}: removed {removed} queued superseded torrent(s) after upgrade import')
            if failed:
                log.warning(f'{source}: post-import purge failures: {failed}')
    except Exception as e:
        log.warning(f'{source}: post-import queued-superseded purge failed: {e}')


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

# How long the grab-time dedupe will wait for the *arr to have written its
# grab history for the download we were just told about. Six tries five
# seconds apart = ~25s of waiting worst case, on a detached daemon thread.
GRAB_DEDUPE_TRIES = int(os.environ.get('GRAB_DEDUPE_TRIES', 6))
GRAB_DEDUPE_DELAY = float(os.environ.get('GRAB_DEDUPE_DELAY', 5))


def dedupe_grabbed_release(source, download_id):
    """Dedupe the throttled lane NOW, scoped to the one movie/series just
    grabbed, instead of waiting for the daily/weekly sweep.

    Why this exists: the *arr relabels a throttled upgrade out of its own
    download-client category (see cleanup_radarr_queue_dupes), which blinds
    the *arr's own QueueSpecification -- so it happily grabs a SECOND
    release of the same film. The scheduled passes do catch that, but a
    duplicate could sit there burning bandwidth for up to a day first. Run
    at grab time the second copy is dropped before it has really started.

    Called only after this grab is confirmed to be sitting in the throttled
    lane, which is what makes it observable to the pass:
      * the torrent exists in Deluge and carries the -upgrade label, so the
        pass's lane scan sees it;
      * its queue record is on its way out (the relabel just happened), and
        while it is still stale-visible the pass's queue_hashes check keeps
        it from being counted twice.
    The remaining race is the *arr's own history: the Grab notification can
    beat the 'grabbed' record being queryable by downloadId, and without
    that record the torrent has no identity and the pass would silently
    skip it. So we POLL for the identity rather than sleeping a guessed
    interval, and if it never resolves we do nothing at all -- an
    unresolvable identity is never grounds for a removal, and the scheduled
    passes remain the backstop for this and for every grab that happened
    while the service was down or whose webhook was missed.

    Never raises: this runs on the webhook thread and must not be able to
    turn a dedupe hiccup into a failed throttle."""
    try:
        if source == 'Radarr' and not RADARR_API_KEY:
            return 0
        if source == 'Sonarr' and not SONARR_API_KEY:
            return 0
        scope = None
        for attempt in range(1, GRAB_DEDUPE_TRIES + 1):
            if source == 'Radarr':
                scope, _ = _radarr_grab_identity(download_id)
            else:
                series_id, episodes, _ = _sonarr_grab_identity(download_id)
                # A seriesId with no episodes resolved is not an identity:
                # the pass compares episode SETS, so it would ignore this
                # torrent anyway. Keep waiting for the per-episode records.
                scope = series_id if episodes else None
            if scope:
                break
            if attempt < GRAB_DEDUPE_TRIES:
                log.info(f'{source}: grab-time dedupe: no grab history for {download_id[:8]} yet '
                         f'(try {attempt}/{GRAB_DEDUPE_TRIES}), waiting')
                time.sleep(GRAB_DEDUPE_DELAY)
        if not scope:
            log.warning(f'{source}: grab-time dedupe: could not resolve {download_id[:8]} to a '
                        f'movie/series, leaving it for the scheduled pass')
            return 0
        if source == 'Radarr':
            removed = cleanup_radarr_queue_dupes(movie_id=scope) or 0
        else:
            removed = cleanup_sonarr_queue_dupes(series_id=scope) or 0
        if removed:
            log.info(f'{source}: grab-time dedupe dropped {removed} duplicate download(s)')
        return removed
    except Exception as e:
        log.error(f'{source}: grab-time dedupe failed for {download_id[:8]}: {e}')
        return 0


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
            # Already in the lane, so still worth a scoped dedupe: a re-fired
            # webhook is a free second chance at a grab whose first webhook
            # was missed or arrived while the service was down.
            dedupe_grabbed_release(source, download_id)
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
        # Only now, with the torrent confirmed in the throttled lane, is it
        # visible to the dedupe pass at all -- so this is the earliest point
        # the check can be both immediate and correct. Never raises.
        dedupe_grabbed_release(source, download_id)
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
        batches = [movie_ids[n:n + BULK_SEARCH_BATCH]
                   for n in range(0, len(movie_ids), BULK_SEARCH_BATCH)]
        log.info(f'Radarr bulk search: {len(movie_ids)} movies in {len(batches)} '
                 f'batches of {BULK_SEARCH_BATCH}, {BULK_SEARCH_DELAY}s apart')
        for n, batch in enumerate(batches, 1):
            try:
                r = requests.post(
                    f'{RADARR_URL}/api/v3/command',
                    headers={'X-Api-Key': RADARR_API_KEY},
                    json={'name': 'MoviesSearch', 'movieIds': batch},
                    timeout=30
                )
                r.raise_for_status()
                log.info(f'Radarr bulk search batch {n}/{len(batches)} '
                         f'({len(batch)} movies) queued: id {r.json().get("id")}')
            except Exception as e:
                log.error(f'Radarr bulk search batch {n}/{len(batches)} failed: {e}')
            if n < len(batches):
                time.sleep(BULK_SEARCH_DELAY)
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

def sonarr_bulk_search():
    """Sonarr counterpart of radarr_bulk_search: search every monitored
    series so missing episodes and cutoff-unmet upgrades both get picked up.

    Radarr's MoviesSearch takes a *list* of movieIds in one command; Sonarr's
    SeriesSearch takes a single seriesId (confirmed against Sonarr's
    SeriesSearchCommand: `public int SeriesId`), so a "batch" here is N
    separate commands fired back-to-back before the pacing sleep. One series
    search fans out to every monitored episode in that series, so it's far
    heavier per unit than one movie -- hence a much smaller default batch
    than BULK_SEARCH_BATCH. Same tracker-announce rate-limit reasoning as
    the Radarr comment above.
    """
    log.info('Running monthly Sonarr bulk search for missing/upgrades...')
    try:
        series_r = requests.get(
            f'{SONARR_URL}/api/v3/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=30
        )
        series_r.raise_for_status()
        series_ids = [s['id'] for s in series_r.json() if s.get('monitored')]
        if not series_ids:
            log.warning('Sonarr bulk search: no monitored series found, skipping')
            return
        batches = [series_ids[n:n + SONARR_BULK_SEARCH_BATCH]
                   for n in range(0, len(series_ids), SONARR_BULK_SEARCH_BATCH)]
        log.info(f'Sonarr bulk search: {len(series_ids)} series in {len(batches)} '
                 f'batches of {SONARR_BULK_SEARCH_BATCH}, {BULK_SEARCH_DELAY}s apart')
        for n, batch in enumerate(batches, 1):
            for series_id in batch:
                try:
                    r = requests.post(
                        f'{SONARR_URL}/api/v3/command',
                        headers={'X-Api-Key': SONARR_API_KEY},
                        json={'name': 'SeriesSearch', 'seriesId': series_id},
                        timeout=30
                    )
                    r.raise_for_status()
                except Exception as e:
                    log.error(f'Sonarr SeriesSearch for series {series_id} failed: {e}')
            log.info(f'Sonarr bulk search batch {n}/{len(batches)} ({len(batch)} series) queued')
            if n < len(batches):
                time.sleep(BULK_SEARCH_DELAY)
    except Exception as e:
        log.error(f'Sonarr bulk search failed: {e}')

def relabel_sonarr_upgrades():
    """Sonarr counterpart of relabel_radarr_upgrades: any 'sonarr'-labeled
    torrent in Deluge whose queue record points at an episode that already
    has a file is an upgrade -- throttle it into the sonarr-upgrade lane and
    push it to the bottom of the queue so it can't starve new releases.

    Radarr can answer 'does this already have a file' from one bulk /movie
    fetch; Sonarr's per-episode hasFile needs a per-episode lookup, so this
    queries only the episodes actually sitting in the queue (small set) via
    the same /api/v3/episode/{id} call is_upgrade_sonarr already uses.
    """
    log.info('Relabeling Sonarr upgrade torrents...')
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return
        sonarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'sonarr'}
        if not sonarr_torrents:
            log.info('No sonarr-labeled torrents to check')
            return
        q = requests.get(
            f'{SONARR_URL}/api/v3/queue',
            headers={'X-Api-Key': SONARR_API_KEY},
            params={'pageSize': 500},
            timeout=15
        )
        q.raise_for_status()
        download_to_episode = {}
        for rec in q.json().get('records', []):
            dl = rec.get('downloadId')
            ep = rec.get('episodeId')
            if dl and ep:
                download_to_episode.setdefault(dl.lower(), ep)
        relabeled_hashes = []
        for torrent_hash, info in sonarr_torrents.items():
            episode_id = download_to_episode.get(torrent_hash.lower())
            if not episode_id:
                continue
            try:
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
    except Exception as e:
        log.error(f'Sonarr upgrade relabeling failed: {e}')

def verify_and_fix_labels(services=('radarr', 'sonarr')):
    """Final safety pass, distinct from relabel_radarr_upgrades/relabel_sonarr_upgrades:
    those two only ever look at torrents ALREADY labeled 'radarr'/'sonarr' and decide
    whether to promote them to the upgrade label. This instead checks EVERY current
    torrent against Radarr/Sonarr's queue by hash and computes what its label should
    be from scratch -- catching two gaps those functions can't: a torrent whose
    grab-time label from Radarr/Sonarr's own Deluge integration never landed at all
    (blank label), and a torrent that only appeared in Deluge after the normal
    relabel pass already ran (a real risk on a fast manual run via skip_waits=1,
    where the wait before relabeling is cut from 5min to 30s).
    """
    log.info(f'Verifying torrent labels for {services}...')
    fixed = []
    try:
        deluge_login()
        torrents = get_all_torrents()
        if not torrents:
            return fixed

        if 'radarr' in services:
            try:
                movies_r = requests.get(f'{RADARR_URL}/api/v3/movie', headers={'X-Api-Key': RADARR_API_KEY}, timeout=15)
                movies_r.raise_for_status()
                movies = {m['id']: m for m in movies_r.json()}
                q = requests.get(f'{RADARR_URL}/api/v3/queue', headers={'X-Api-Key': RADARR_API_KEY}, params={'pageSize': 500}, timeout=15)
                q.raise_for_status()
                download_to_movie = {rec['downloadId'].lower(): rec.get('movieId') for rec in q.json().get('records', []) if rec.get('downloadId')}
                for torrent_hash, info in torrents.items():
                    movie_id = download_to_movie.get(torrent_hash.lower())
                    if not movie_id:
                        continue
                    movie = movies.get(movie_id)
                    if not movie:
                        continue
                    correct_label = RADARR_UPG_LABEL if movie.get('hasFile') else 'radarr'
                    current_label = info.get('label', '')
                    if current_label != correct_label:
                        log.info(f'Verify pass: fixing label on "{info.get("name")}" ({current_label!r} -> {correct_label!r})')
                        set_torrent_label(torrent_hash, correct_label)
                        fixed.append({'name': info.get('name'), 'from': current_label, 'to': correct_label})
            except Exception as e:
                log.error(f'Radarr label verification failed: {e}')

        if 'sonarr' in services:
            try:
                q = requests.get(f'{SONARR_URL}/api/v3/queue', headers={'X-Api-Key': SONARR_API_KEY}, params={'pageSize': 500}, timeout=15)
                q.raise_for_status()
                download_to_episode = {}
                for rec in q.json().get('records', []):
                    dl = rec.get('downloadId')
                    ep = rec.get('episodeId')
                    if dl and ep:
                        download_to_episode.setdefault(dl.lower(), ep)
                for torrent_hash, info in torrents.items():
                    episode_id = download_to_episode.get(torrent_hash.lower())
                    if not episode_id:
                        continue
                    try:
                        er = requests.get(f'{SONARR_URL}/api/v3/episode/{episode_id}', headers={'X-Api-Key': SONARR_API_KEY}, timeout=10)
                        er.raise_for_status()
                        has_file = er.json().get('hasFile', False)
                    except Exception as e:
                        log.warning(f'Sonarr episode {episode_id} lookup failed during verify: {e}')
                        continue
                    correct_label = SONARR_UPG_LABEL if has_file else 'sonarr'
                    current_label = info.get('label', '')
                    if current_label != correct_label:
                        log.info(f'Verify pass: fixing label on "{info.get("name")}" ({current_label!r} -> {correct_label!r})')
                        set_torrent_label(torrent_hash, correct_label)
                        fixed.append({'name': info.get('name'), 'from': current_label, 'to': correct_label})
            except Exception as e:
                log.error(f'Sonarr label verification failed: {e}')
    except Exception as e:
        log.error(f'Label verification pass failed: {e}')
    log.info(f'Label verification complete: fixed {len(fixed)} torrent(s)')
    return fixed

def purge_stalled_upgrade_torrents(label=RADARR_UPG_LABEL):
    """Remove <label> torrents that haven't downloaded more than 5MB.
    Defaults to the Radarr upgrade lane; the Sonarr monthly cycle passes
    SONARR_UPG_LABEL."""
    log.info(f'Purging stalled {label} torrents...')
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
            if i.get('label') == label:
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
    On the 1st of each month, Radarr first and then the identical Sonarr
    cycle (Sonarr was silently missing a lot of episodes because nothing
    ever bulk-searched it):
    1. Purge stalled <service>-upgrade torrents
    2. Wait 30 minutes
    3. Trigger bulk search
    4. Wait 5 minutes
    5. Relabel new upgrade torrents to the throttled lane, queue them last
    """
    import datetime
    last_run_month = None
    while True:
        now = datetime.datetime.now()
        if now.day == 1 and now.month != last_run_month:
            last_run_month = now.month
            log.info('Monthly upgrade cycle starting')
            monthly_upgrade_cycle('radarr')
            # Sonarr runs after Radarr rather than in parallel so the two
            # bulk searches don't stack announces on the same tracker.
            monthly_upgrade_cycle('sonarr')
            log.info('Monthly upgrade cycle complete')
        time.sleep(3600)  # check every hour


def monthly_upgrade_cycle(service, wait_before_search=1800, wait_before_relabel=300):
    """One service's monthly purge → bulk search → relabel pass."""
    if service == 'sonarr':
        label, bulk_search, relabel = SONARR_UPG_LABEL, sonarr_bulk_search, relabel_sonarr_upgrades
    else:
        label, bulk_search, relabel = RADARR_UPG_LABEL, radarr_bulk_search, relabel_radarr_upgrades
    log.info(f'{service}: monthly cycle — purging stalled {label} torrents...')
    purge_stalled_upgrade_torrents(label)
    log.info(f'{service}: waiting {wait_before_search}s before bulk search...')
    time.sleep(wait_before_search)
    bulk_search()
    log.info(f'{service}: waiting {wait_before_relabel}s before relabeling upgrades...')
    time.sleep(wait_before_relabel)
    relabel()
    log.info(f'{service}: monthly cycle complete')


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
                        supersede_torrent(h)
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

# Read-only preview of the queue-duplicate cleanup passes: runs both
# passes in dry-run mode and reports what WOULD be removed/superseded
# without deleting or relabeling anything. ?service=radarr|sonarr|both
# (default both).
@app.route('/queue-dupe-scan', methods=['GET'])
def queue_dupe_scan():
    service = (request.args.get('service') or 'both').lower()
    result = {'ok': True, 'service': service}
    if service in ('both', 'radarr'):
        result['radarr'] = cleanup_radarr_queue_dupes(dry_run=True)
    if service in ('both', 'sonarr'):
        result['sonarr'] = cleanup_sonarr_queue_dupes(dry_run=True)
    return jsonify(result), 200

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
# (SEASON_RE is defined near EPISODE_RE at the top of the file.)

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

# Surgical fix for a single mislabeled torrent — e.g. an in-flight upgrade
# grab the (now-fixed) dedup pass wrongly tagged superseded before it
# finished downloading. Unlike /revert-superseded (which flips EVERY
# superseded torrent back and can't tell good relabels from bad ones), this
# targets one hash. Body OR query: hash=<hash>, label=<label> (default
# 'radarr-upgrade').
@app.route('/torrent-relabel', methods=['POST'])
def torrent_relabel():
    torrent_hash = (request.args.get('hash') or '').lower().strip()
    label = request.args.get('label', '').strip()
    if not torrent_hash or not label:
        body = request.get_json(silent=True) or {}
        torrent_hash = torrent_hash or (body.get('hash') or '').lower().strip()
        label = label or (body.get('label') or RADARR_UPG_LABEL).strip()
    if not torrent_hash:
        return jsonify({'ok': False, 'error': 'hash required (query or body)'}), 400
    try:
        deluge_login()
        ensure_label_exists_named(label)
        set_torrent_label(torrent_hash, label)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'hash': torrent_hash, 'label': label}), 200

# Manual trigger for the dedup pass — use to verify the fixed matcher
# behaves before waiting for the 24h scheduled tick.
@app.route('/run-dedup', methods=['POST'])
def run_dedup():
    dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes')
    threading.Thread(target=dedup_via_radarr, args=(dry_run,), daemon=True).start()
    threading.Thread(target=dedup_via_sonarr, args=(dry_run,), daemon=True).start()
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

# Manual trigger for cleanup_stalled_seeds. GET (or POST without ?apply=1)
# previews candidates without removing anything or touching the saved
# upload-baseline state. ?apply=1 runs it for real. Note: a preview run
# only finds candidates once a real (non-dry) run has recorded at least
# one prior week's baseline — the first-ever run for a torrent always
# just baselines it, dry or not.
@app.route('/run-stalled-seeds', methods=['GET', 'POST'])
def run_stalled_seeds():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    candidates = cleanup_stalled_seeds(dry_run=not apply)
    return jsonify({
        'ok': True,
        'dry_run': not apply,
        'count': len(candidates),
        'candidates': candidates,
    }), 200

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

# Same purpose as /incomplete-orphans, for the Complete directory instead.
# Complete is riskier than Incomplete: it also holds the long-term library
# folder(s) Radarr/Sonarr point at directly, not just in-flight downloads --
# a naive top-level scan would flag those folders themselves as "orphans"
# (nothing in Deluge is literally named "radarr" or "sonarr") and rmtree the
# entire library. COMPLETE_PROTECTED_NAMES (comma-separated top-level names,
# case-insensitive) must be set and non-empty before ?apply=1 is allowed to
# delete anything -- dry-run (default) still works without it configured, so
# you can see what's actually at the top level of Complete and its sizes
# before deciding what to protect. Any entry matching a protected name is
# always kept, regardless of tracked status.
@app.route('/complete-orphans', methods=['GET', 'POST'])
def complete_orphans():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    downloads_root = os.environ.get('DOWNLOADS_MOUNT', '/data/Downloads')
    complete_dir = os.path.join(downloads_root, 'Complete')
    if not os.path.isdir(complete_dir):
        return jsonify({'ok': False, 'error': f'{complete_dir} not a directory'}), 400
    protected_raw = os.environ.get('COMPLETE_PROTECTED_NAMES', '')
    protected = {p.strip().lower() for p in protected_raw.split(',') if p.strip()}
    if apply and not protected:
        return jsonify({
            'ok': False,
            'error': (
                'COMPLETE_PROTECTED_NAMES is not set -- refusing to apply deletions on Complete '
                'until the long-term library folder name(s) are explicitly protected. Run a '
                'dry-run first (no ?apply) to see the top-level names, set COMPLETE_PROTECTED_NAMES '
                '(comma-separated, e.g. "radarr,sonarr") in docker-compose.yml, then retry.'
            ),
        }), 400
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'save_path', 'files']],
                'id': 97,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'deluge fetch failed: {e}'}), 500
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
    protected_hit = []
    for entry in sorted(os.listdir(complete_dir)):
        full = os.path.join(complete_dir, entry)
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
        if entry.lower() in protected:
            rec['reason'] = 'protected (long-term library folder)'
            protected_hit.append(rec)
            continue
        if entry in tracked_names:
            kept.append(rec)
            continue
        # Untracked alone isn't enough to call something safe to delete --
        # a file mid-import (not yet picked up by Radarr/Sonarr) would also
        # look untracked for a brief window. Gate on age too, same SEED_DAYS
        # threshold already used elsewhere in this file for "safe to clean
        # up" decisions -- only something untracked AND stale for that long
        # is treated as a real orphan eligible for deletion.
        try:
            age_days = (time.time() - os.path.getmtime(full)) / 86400
        except Exception:
            age_days = 0
        rec['age_days'] = round(age_days, 1)
        if age_days < SEED_DAYS:
            rec['reason'] = f'untracked but only {age_days:.1f}d old (< {SEED_DAYS}d threshold) -- not deleted'
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
        'complete_dir': complete_dir,
        'protected_names': sorted(protected),
        'min_age_days': SEED_DAYS,
        'counts': {'orphans': len(orphans), 'tracked': len(kept), 'protected': len(protected_hit)},
        'orphans': orphans,
        'tracked': kept,
        'protected': protected_hit,
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
        record_activity('cleanup', f'Manually purged "{info.get("name")}" via /torrent-purge (label: {info.get("label")})')
        result['result'] = 'removed torrent + files'
    except Exception as e:
        result['result'] = f'FAILED: {e}'
        return jsonify({'ok': False, 'apply': True, 'target': result}), 500
    return jsonify({'ok': True, 'apply': True, 'target': result}), 200

# Bulk-remove superseded torrents sitting in Deluge's own 'Queued' state —
# waiting behind the active-torrent limit, not transferring, nothing to
# lose by going now (Deluge's queue can hold a torrent here at any
# progress %, not just 0, so this checks state rather than progress).
# Unlike cleanup_superseded (which waits SEED_DAYS before sweeping), these
# aren't seeding anything either. Dry-run default, apply=1 to remove.
@app.route('/purge-unstarted-superseded', methods=['POST', 'GET'])
def purge_unstarted_superseded():
    apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
    try:
        deluge_login()
        torrents = get_all_torrents()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    targets = queued_superseded_targets(torrents)
    if not apply:
        return jsonify({'ok': True, 'apply': False, 'count': len(targets), 'targets': targets,
                         'note': 'dry-run — pass ?apply=1 to remove these torrents + files'}), 200
    removed, failed = purge_queued_superseded(targets)
    return jsonify({'ok': True, 'apply': True, 'removed': removed, 'failed': failed}), 200

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
                'params': [{}, ['name', 'label', 'save_path', 'seeding_time', 'total_size', 'progress', 'state']],
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
            'progress': info.get('progress'),
            'state': info.get('state'),
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
# normal 30/5-minute waits by default; ?skip_waits=1 replaces them with
# a short interval so you can watch the whole pipeline end-to-end.
# ?service=radarr|sonarr|both (default both, radarr first).
@app.route('/run-monthly-upgrade', methods=['POST'])
def run_monthly_upgrade():
    skip_waits = request.args.get('skip_waits', '').lower() in ('1', 'true', 'yes')
    service = (request.args.get('service') or 'both').lower()
    if service not in ('radarr', 'sonarr', 'both'):
        return jsonify({'ok': False, 'error': 'service must be radarr, sonarr or both'}), 400
    services = ['radarr', 'sonarr'] if service == 'both' else [service]
    waits = (10, 30) if skip_waits else (1800, 300)
    def _cycle():
        log.info(f'Manual monthly upgrade cycle starting ({service}, skip_waits={skip_waits})')
        for svc in services:
            monthly_upgrade_cycle(svc, *waits)
        log.info('Running final label verification pass before marking cycle complete...')
        fixed = verify_and_fix_labels(services)
        log.info(f'Manual monthly upgrade cycle complete (verification fixed {len(fixed)} torrent(s))')
    threading.Thread(target=_cycle, daemon=True).start()
    return jsonify({
        'ok': True,
        'service': service,
        'skip_waits': skip_waits,
        'message': 'monthly upgrade cycle started; watch container logs',
    }), 200

# Standalone label-verification pass -- can be run any time independent of a
# full monthly-upgrade cycle, e.g. to catch stragglers after a fast manual
# run. Synchronous (a handful of quick API calls), so the response itself
# reports what was fixed rather than needing to check container logs.
@app.route('/verify-labels', methods=['GET', 'POST'])
def verify_labels_route():
    service = (request.args.get('service') or 'both').lower()
    if service not in ('radarr', 'sonarr', 'both'):
        return jsonify({'ok': False, 'error': 'service must be radarr, sonarr or both'}), 400
    services = ('radarr', 'sonarr') if service == 'both' else (service,)
    fixed = verify_and_fix_labels(services)
    return jsonify({'ok': True, 'service': service, 'fixed_count': len(fixed), 'fixed': fixed}), 200

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

# ── Deluge error scan + relink ───────────────────────────────────────────────
# A power outage leaves torrents in Deluge's 'Error' state: libtorrent hit a
# file error on resume (data half-written, or the file it expects is gone).
# Two distinct causes need two distinct fixes, and telling them apart matters
# because the fix for one is destructive if applied to the other:
#
#   a) data is still at save_path, libtorrent just gave up on it
#      → force_recheck. Re-hashes in place, moves nothing. Safe, idempotent.
#   b) data genuinely isn't at save_path but exists elsewhere under the
#      downloads tree → the save_path has to be re-pointed. Deluge's only
#      primitive for that is core.move_storage, which calls libtorrent with
#      move_flags=2 (dont_replace): files present at the destination win and
#      are kept, source files that do exist are MOVED (and the source copy
#      deleted). That's real I/O against real data, so it is opt-in.
#      core.set_torrent_options({'download_location': ...}) is NOT an
#      alternative — verified against Deluge's torrent.py, its setter only
#      stores the option and never touches existing data, so the torrent
#      would stay broken while looking reconfigured.
#
# Whether the data is at save_path is answered by Deluge itself via
# core.get_path_size (returns -1 for a nonexistent path) rather than by
# os.path.exists here, because this container does not necessarily have
# Deluge's download tree mounted.
#
# The *search* for a moved file needs local mounts, and the two containers do
# NOT agree on paths. Confirmed live against this Deluge instance: torrent
# save_paths are a mix of /data/Downloads/{Incomplete,Complete/*,Just4Seeding}
# and, for a large number of library-seeding torrents, /data/Media/TV Shows/…
# and /data/Media/Adult. Deluge's /data/Media/TV Shows is this container's
# /media/tv, so every path crossing the boundary has to be translated —
# DELUGE_PATH_MAP does that in both directions. Anything Deluge can see but
# this container has no mount for (e.g. /data/Media/Adult) simply isn't
# searchable and gets reported as missing rather than silently mishandled.
#
# Because library-seeding torrents seed the library files themselves, a
# relink is capable of MOVING LIBRARY MEDIA — which is the main reason
# relinking is opt-in and rechecking is not.

DELUGE_REPAIR_INTERVAL = int(os.environ.get('DELUGE_REPAIR_INTERVAL', '21600'))  # 6h
# Off by default: rechecks are automatic, relocations are reported and wait
# for a human. Set DELUGE_RELINK=1 to let the scheduler move storage too.
DELUGE_RELINK = os.environ.get('DELUGE_RELINK', '').lower() in ('1', 'true', 'yes')
DOWNLOADS_MOUNT = os.environ.get('DOWNLOADS_MOUNT', '/data/Downloads')
# deluge_path:local_path pairs, comma-separated.
DELUGE_PATH_MAP = [
    tuple(pair.split(':', 1))
    for pair in os.environ.get(
        'DELUGE_PATH_MAP',
        f'/data/Downloads:{DOWNLOADS_MOUNT},'
        '/data/Media/Movies:/media/movies,'
        '/data/Media/TV Shows:/media/tv,'
        '/data/Media/Music:/media/music',
    ).split(',')
    if ':' in pair
]

def _deluge_to_local(p):
    return _map_prefix(p, DELUGE_PATH_MAP)

def _local_to_deluge(p):
    return _map_prefix(p, [(local, deluge) for deluge, local in DELUGE_PATH_MAP])

def deluge_force_recheck(torrent_hash):
    """core.force_recheck — re-verifies pieces against whatever is on disk.
    Deluge's own force_recheck resumes the torrent afterwards, so no
    separate resume call is needed."""
    resp = session.post(
        f'{DELUGE_URL}/json',
        json={'method': 'core.force_recheck', 'params': [[torrent_hash]], 'id': 61},
        timeout=30,
    )
    resp.raise_for_status()
    log.info(f'Forced recheck of {torrent_hash}')

def _deluge_path_exists(path):
    """True/False from Deluge's own filesystem view; None if we couldn't ask.
    core.get_path_size returns -1 when the path is inaccessible — which per
    Deluge's own docstring means non-existent OR insufficient privileges, so a
    permissions problem reads as 'data missing' here and sends the torrent
    down the search branch. Harmless while relinking is opt-in; worth
    remembering if a torrent is reported missing whose data is plainly there."""
    try:
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={'method': 'core.get_path_size', 'params': [path], 'id': 62},
            timeout=30,
        )
        resp.raise_for_status()
        size = resp.json().get('result')
        return None if size is None else size >= 0
    except Exception as e:
        log.warning(f'get_path_size({path}) failed: {e}')
        return None

def _search_roots():
    """Local (this container's) roots to search, derived from the mounts we
    actually have. Deluge paths we can't see are skipped."""
    return [local for _, local in DELUGE_PATH_MAP if os.path.isdir(local)]

def _find_torrent_data(torrent_name, roots=None):
    """Locate a torrent's data by name. Returns a LOCAL path, or None.

    Exact stem match first (Deluge names the file/folder after the torrent,
    so this is the real case). The fuzzy fallback is gated on a long name
    because _torrent_name_matches_file is substring-both-ways and a short
    name would happily match a whole season folder.
    ponytail: linear os.walk of the mounted roots, fine at homelab scale and
    only runs for torrents already in Error; index it if that stops holding."""
    stem = _strip_release_ext(torrent_name.lower())
    fuzzy_hit = None
    for root in (roots if roots is not None else _search_roots()):
        for dirpath, dirnames, filenames in os.walk(root):
            for entry in list(dirnames) + list(filenames):
                if _strip_release_ext(entry.lower()) == stem:
                    return os.path.join(dirpath, entry)
                if (fuzzy_hit is None and len(stem) >= 20
                        and _torrent_name_matches_file(torrent_name, entry)):
                    fuzzy_hit = os.path.join(dirpath, entry)
    return fuzzy_hit

# A torrent whose data is genuinely gone stays broken until a human deals
# with it, and the scheduler re-sees it every 6 hours. Report each
# (hash, outcome) to the digest once per process so the review queue doesn't
# fill up with the same handful of entries four times a day.
_reported_deluge_errors = set()

def _report_once(key, category, summary, dry_run=False):
    if dry_run or key in _reported_deluge_errors:
        return  # a dry-run inspection shouldn't post to the review queue
    _reported_deluge_errors.add(key)
    record_activity(category, summary)

def deluge_error_repair(dry_run=False, relink=None):
    """Scan Deluge for errored torrents and self-heal what's safe to heal."""
    if relink is None:
        relink = DELUGE_RELINK
    result = {'ok': True, 'dry_run': dry_run, 'relink': relink,
              'rechecked': [], 'relink_candidates': [], 'missing': [], 'errors': []}
    try:
        deluge_login()
        resp = session.post(
            f'{DELUGE_URL}/json',
            json={
                'method': 'core.get_torrents_status',
                'params': [{}, ['name', 'state', 'message', 'save_path', 'label', 'progress']],
                'id': 60,
            },
            timeout=30,
        )
        resp.raise_for_status()
        torrents = resp.json().get('result') or {}
    except Exception as e:
        log.error(f'Deluge error scan failed: {e}')
        return {'ok': False, 'error': str(e)}

    for h, info in torrents.items():
        # Deluge sets state='Error' for torrent/file errors and puts the
        # detail in 'message'. Tracker problems surface in tracker_status
        # instead, so they don't land here.
        if info.get('state') != 'Error':
            continue
        name = info.get('name') or ''
        save_path = info.get('save_path') or ''
        entry = {'hash': h, 'name': name, 'save_path': save_path,
                 'message': info.get('message'), 'label': info.get('label')}
        present = _deluge_path_exists(os.path.join(save_path, name)) if save_path and name else None
        if present:
            entry['action'] = 'recheck' if not dry_run else 'would recheck'
            if not dry_run:
                try:
                    deluge_force_recheck(h)
                except Exception as e:
                    entry['action'] = f'recheck FAILED: {e}'
                    result['errors'].append(entry)
                    continue
            result['rechecked'].append(entry)
            continue
        if present is None:
            entry['action'] = 'skipped — could not determine whether data exists'
            result['errors'].append(entry)
            continue
        roots = _search_roots()
        found_local = _find_torrent_data(name, roots)
        if not found_local:
            entry['action'] = f'data not found in any searchable root {roots}'
            result['missing'].append(entry)
            _report_once(
                (h, 'missing'), 'deluge-error',
                f'Errored torrent "{name}" — data missing from {save_path} and not found in '
                f'{", ".join(roots) or "(no roots mounted)"} ({info.get("message") or "no message"})',
                dry_run=dry_run,
            )
            continue
        # Everything below crosses the container/Deluge namespace boundary:
        # found_local is this container's path, Deluge needs its own.
        found_deluge = _local_to_deluge(found_local)
        dest = os.path.dirname(found_deluge)
        entry['found_at'] = found_local
        entry['found_at_deluge'] = found_deluge
        entry['relink_dest'] = dest
        # Never hand Deluge a path it can't see. This re-verifies the
        # translation against Deluge's own filesystem instead of trusting
        # DELUGE_PATH_MAP blindly — a wrong map here would move_storage a
        # torrent to a bogus location.
        if _deluge_path_exists(found_deluge) is not True:
            entry['action'] = (f'found at {found_local} but Deluge cannot see {found_deluge} — '
                               f'check DELUGE_PATH_MAP; not relinking')
            result['errors'].append(entry)
            continue
        if relink and not dry_run:
            try:
                # Re-point only. The recheck is left to the next pass: the
                # move is asynchronous (Deluge goes to state 'Moving'), and
                # by the next tick get_path_size will see the data at the new
                # save_path and take the safe recheck branch above.
                move_torrent_storage(h, dest)
                entry['action'] = f'relinked to {dest}; recheck on next pass'
                record_activity('deluge-error', f'Relinked errored torrent "{name}" to {dest}')
            except Exception as e:
                entry['action'] = f'relink FAILED: {e}'
                result['errors'].append(entry)
                continue
        else:
            entry['action'] = f'relink candidate (set DELUGE_RELINK=1 or POST ?relink=1) → {dest}'
            _report_once((h, 'relink-candidate'), 'deluge-error',
                         f'Errored torrent "{name}" — data found at {found_local}, relink not applied',
                         dry_run=dry_run)
        result['relink_candidates'].append(entry)

    counts = {k: len(v) for k, v in result.items() if isinstance(v, list)}
    result['counts'] = counts
    if any(counts.values()):
        log.info(f'Deluge error repair: {counts}')
    return result

# Manual trigger for the error scan (mirrors /run-dedup). Runs inline so you
# get the report back; dry_run=1 to look without rechecking, relink=1 to also
# move storage for torrents whose data was found elsewhere.
@app.route('/run-deluge-repair', methods=['POST', 'GET'])
def run_deluge_repair():
    dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes')
    # Absent → inherit DELUGE_RELINK; present → explicit, so ?relink=0 can
    # force a no-relink run even once the env default is turned on.
    raw_relink = request.args.get('relink')
    relink = None if raw_relink is None else raw_relink.lower() in ('1', 'true', 'yes')
    return jsonify(deluge_error_repair(dry_run=dry_run, relink=relink)), 200


# ── Duplicate media folder scan ──────────────────────────────────────────────
# When a new torrent lands for content that already has a library folder, the
# import can end up beside the existing folder instead of replacing it,
# leaving `Movie Name (2020)` next to `Movie Name (2020) (1)`. Detection only:
# which copy is the keeper depends on what the arrs think they're tracking, and
# deleting the wrong one loses the library file, so this reports and stops.

MEDIA_ROOTS = [p.strip() for p in os.environ.get('MEDIA_ROOTS', '/media/movies,/media/tv').split(',') if p.strip()]
# Trailing collision suffixes only. `\(\d{1,3}\)` deliberately stops at 3
# digits so a 4-digit year — `Movie Name (2020)` — is never stripped and two
# different years of the same title don't collapse into one false dupe.
_DUP_SUFFIX_RE = re.compile(r'(?:\s*\(\d{1,3}\)|\s*-\s*copy|_\d{1,2})$', re.IGNORECASE)

def _dup_folder_key(name):
    return re.sub(r'[^a-z0-9]+', '', _DUP_SUFFIX_RE.sub('', name.strip()).lower())

def find_duplicate_media_folders():
    """Sibling folders under the media roots whose names differ only by a
    collision suffix. Sizes are computed only for actual hits — the scan
    itself is scandir-only, which is what makes it cheap enough to ride the
    6-hourly scheduler."""
    parents = []
    for root in MEDIA_ROOTS:
        if not os.path.isdir(root):
            log.debug(f'dupe-folder scan: {root} not mounted, skipping')
            continue
        parents.append(root)
        # One level down too, so `Series/Season 01 (1)` is caught as well.
        try:
            parents.extend(e.path for e in os.scandir(root) if e.is_dir())
        except OSError as e:
            log.warning(f'dupe-folder scan: {root}: {e}')
    findings = []
    for parent in parents:
        groups = {}
        try:
            for e in os.scandir(parent):
                if e.is_dir():
                    groups.setdefault(_dup_folder_key(e.name), []).append(e.path)
        except OSError:
            continue
        for key, paths in groups.items():
            if len(paths) > 1 and key:
                findings.append({
                    'key': key,
                    'parent': parent,
                    'folders': [
                        {'path': p, 'size_gb': round(_du(p) / (1024 ** 3), 2)}
                        for p in sorted(paths)
                    ],
                })
    return findings

# Report each finding to the digest once per process, so a dupe that the user
# has decided to leave alone doesn't re-post every 6 hours forever.
_reported_dupe_folders = set()

def duplicate_folder_scan():
    try:
        findings = find_duplicate_media_folders()
    except Exception as e:
        log.error(f'Duplicate folder scan failed: {e}')
        return
    new = [f for f in findings if f['key'] not in _reported_dupe_folders]
    for f in new:
        _reported_dupe_folders.add(f['key'])
        detail = ', '.join(f'{os.path.basename(x["path"])} ({x["size_gb"]}GB)' for x in f['folders'])
        record_activity('dupe-folder', f'Duplicate folders in {f["parent"]}: {detail}')
    log.info(f'Duplicate folder scan: {len(findings)} group(s), {len(new)} new')

@app.route('/duplicate-folders', methods=['GET'])
def duplicate_folders():
    findings = find_duplicate_media_folders()
    return jsonify({
        'ok': True,
        'roots': MEDIA_ROOTS,
        'count': len(findings),
        'findings': findings,
        'note': 'detection only — nothing is deleted automatically',
    }), 200


def maintenance_scheduler():
    """Every 6 hours: heal errored torrents, then look for duplicate library
    folders. Shares one thread because the dupe scan is scandir-only (sizes
    are computed for hits only), so it costs nothing to run on the same tick
    as the error scan rather than owning a fifth scheduler."""
    while True:
        try:
            deluge_error_repair()
        except Exception as e:
            log.error(f'Deluge error repair failed: {e}')
        try:
            duplicate_folder_scan()
        except Exception as e:
            log.error(f'Duplicate folder scan failed: {e}')
        time.sleep(DELUGE_REPAIR_INTERVAL)


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
    t6 = threading.Thread(target=maintenance_scheduler, daemon=True)
    t6.start()
    t7 = threading.Thread(target=stalled_seed_scheduler, daemon=True)
    t7.start()
    t8 = threading.Thread(target=tracker_stall_scheduler, daemon=True)
    t8.start()
    t9 = threading.Thread(target=activity_log_trim_scheduler, daemon=True)
    t9.start()
    t10 = threading.Thread(target=queue_dupe_cleanup_scheduler, daemon=True)
    t10.start()
    app.run(host='0.0.0.0', port=port, threaded=True)
