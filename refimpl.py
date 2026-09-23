#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s6-batched-search

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Surgical replacements in arr-webhook.py -- everything else preserved untouched:
  * radarr_bulk_search / sonarr_bulk_search stop chunking the ENTIRE monitored
    catalog by BULK_SEARCH_BATCH / SONARR_BULK_SEARCH_BATCH and instead search
    one bounded pass (at most UPGRADE_BATCH_SIZE items) driven by a persisted
    cursor, sorted newest-year-first via sort_ids_by_year_desc.
  * a small synchronous /run-bulk-search route so the behaviour is drivable
    through the real Flask app in an integration fixture.

Each function body is located with a regex (from its `def` line to the next
top-level `def`) rather than embedded verbatim, so docstring quoting in the
replacement text cannot collide with this file's string delimiters.
"""
import pathlib
import re
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

RADARR_NEW = '''def radarr_bulk_search():
    """Trigger a bounded yearly-upgrade pass over monitored Radarr movies.

    Instead of searching the ENTIRE catalog in one go (which makes Radarr push
    every accepted release to Deluge back-to-back and trips private-tracker
    announce rate limits), each pass searches at most UPGRADE_BATCH_SIZE
    movies: the monitored list is sorted newest-year-first via
    sort_ids_by_year_desc, advance_upgrade_cursor picks this pass's slice from
    the persisted cursor (wrapping to index 0 after the end), and the new
    cursor plus a fresh last_run timestamp are persisted back through
    _save_upgrade_state so successive passes walk the whole catalog over time.
    """
    log.info('Running monthly Radarr bulk search for upgrades...')
    try:
        state = _load_upgrade_state()
        entry = state.get('radarr', {}) or {}
        movies_r = requests.get(
            f'{RADARR_URL}/api/v3/movie',
            headers={'X-Api-Key': RADARR_API_KEY},
            timeout=15
        )
        movies_r.raise_for_status()
        monitored = [m for m in movies_r.json() if m.get('monitored')]
        if not monitored:
            log.warning('Radarr bulk search: no monitored movies found, skipping')
            return
        ordered = sort_ids_by_year_desc(monitored)
        movie_ids = [m['id'] for m in ordered]
        indices, next_cursor = advance_upgrade_cursor(entry.get('cursor', 0), len(movie_ids))
        batch = [movie_ids[i] for i in indices]
        log.info(f'Radarr bulk search: {len(batch)} of {len(movie_ids)} movies this pass '
                 f'(cursor -> {next_cursor})')
        try:
            r = requests.post(
                f'{RADARR_URL}/api/v3/command',
                headers={'X-Api-Key': RADARR_API_KEY},
                json={'name': 'MoviesSearch', 'movieIds': batch},
                timeout=30
            )
            r.raise_for_status()
            log.info(f'Radarr bulk search pass ({len(batch)} movies) queued: id {r.json().get("id")}')
        except Exception as e:
            log.error(f'Radarr bulk search pass failed: {e}')
        state['radarr'] = {'cursor': next_cursor, 'last_run': datetime.now(timezone.utc).isoformat()}
        _save_upgrade_state(state)
    except Exception as e:
        log.error(f'Radarr bulk search failed: {e}')

'''

SONARR_NEW = '''def sonarr_bulk_search():
    """Sonarr counterpart of radarr_bulk_search: a bounded yearly-upgrade pass.

    Each pass searches at most UPGRADE_BATCH_SIZE monitored series (sorted
    newest-first via sort_ids_by_year_desc, sliced by advance_upgrade_cursor
    from the persisted cursor) instead of the entire catalog. Sonarr's
    SeriesSearch takes a single seriesId (confirmed against Sonarr's
    SeriesSearchCommand: `public int SeriesId`), so each item in this pass is
    one separate command fired back-to-back -- same per-item call as before,
    just bounded to this pass's slice. The new cursor and last_run timestamp
    are persisted through _save_upgrade_state under the 'sonarr' key.
    """
    log.info('Running monthly Sonarr bulk search for missing/upgrades...')
    try:
        state = _load_upgrade_state()
        entry = state.get('sonarr', {}) or {}
        series_r = requests.get(
            f'{SONARR_URL}/api/v3/series',
            headers={'X-Api-Key': SONARR_API_KEY},
            timeout=30
        )
        series_r.raise_for_status()
        monitored = [s for s in series_r.json() if s.get('monitored')]
        if not monitored:
            log.warning('Sonarr bulk search: no monitored series found, skipping')
            return
        ordered = sort_ids_by_year_desc(monitored)
        series_ids = [s['id'] for s in ordered]
        indices, next_cursor = advance_upgrade_cursor(entry.get('cursor', 0), len(series_ids))
        batch = [series_ids[i] for i in indices]
        log.info(f'Sonarr bulk search: {len(batch)} of {len(series_ids)} series this pass '
                 f'(cursor -> {next_cursor})')
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
        state['sonarr'] = {'cursor': next_cursor, 'last_run': datetime.now(timezone.utc).isoformat()}
        _save_upgrade_state(state)
    except Exception as e:
        log.error(f'Sonarr bulk search failed: {e}')

'''


def _replace_function(text, name, new_body):
    """Replace the top-level function `name` (from its def line to just before
    the next top-level def/anchor) with new_body."""
    m = re.search(r"^def " + name + r"\(\):\n.*?(?=^def |\Z)", text, re.M | re.S)
    assert m, "refimpl anchor not found -- did the target change? (def %s)" % name
    return text[:m.start()] + new_body + text[m.end():]


t = _replace_function(t, 'radarr_bulk_search', RADARR_NEW)
t = _replace_function(t, 'sonarr_bulk_search', SONARR_NEW)

# --- synchronous /run-bulk-search route --------------------------------------
ROUTE_ANCHOR = "# Manual trigger for the monthly upgrade cycle."
assert ROUTE_ANCHOR in t, "refimpl anchor not found -- did the target change? (route anchor)"
ROUTE_NEW = '''# Synchronous single-pass bulk search -- one bounded yearly-upgrade pass per
# service (at most UPGRADE_BATCH_SIZE items from the persisted cursor), so it
# is safe to run inline and report what was searched in the response body.
@app.route('/run-bulk-search', methods=['POST'])
def run_bulk_search_route():
    service = (request.args.get('service') or 'radarr').lower()
    if service not in ('radarr', 'sonarr'):
        return jsonify({'ok': False, 'error': 'service must be radarr or sonarr'}), 400
    state_before = _load_upgrade_state().get(service) or {}
    cursor_before = state_before.get('cursor', 0)
    if service == 'radarr':
        radarr_bulk_search()
    else:
        sonarr_bulk_search()
    entry_after = _load_upgrade_state().get(service) or {}
    return jsonify({
        'ok': True,
        'service': service,
        'cursor_before': cursor_before,
        'cursor_after': entry_after.get('cursor'),
        'last_run': entry_after.get('last_run'),
    }), 200

''' + ROUTE_ANCHOR
t = t.replace(ROUTE_ANCHOR, ROUTE_NEW, 1)

# --- timezone import ----------------------------------------------------------
IMPORT_OLD = "from datetime import datetime, timedelta"
assert IMPORT_OLD in t, "refimpl anchor not found -- did the target change? (import)"
t = t.replace(IMPORT_OLD, "from datetime import datetime, timedelta, timezone", 1)

p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(t)
print("refimpl applied")
