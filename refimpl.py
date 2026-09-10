#!/usr/bin/env python3
"""Reference impl for: asian-tv-router. Two insertions: the module-level
`route_series_to_asian` function above the sonarr webhook decorator, and the
`elif event == 'SeriesAdd':` branch that dispatches it."""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

FUNC = '''ASIAN_TV_ROOT = os.environ.get('ASIAN_TV_ROOT', '/data/Media/TV Shows - Asian')
ASIAN_LANGUAGES = {'korean', 'japanese', 'chinese', 'cantonese', 'mandarin'}


def route_series_to_asian(data):
    """On a Sonarr SeriesAdd, move a CJK-language series into the Asian TV root.

    Returns the Asian root when it issues the move, else None. Never raises into
    the webhook thread: any failure is logged and swallowed.
    """
    try:
        series_id = (data.get('series') or {}).get('id')
        if not series_id:
            return None
        r = requests.get(f'{SONARR_URL}/api/v3/series/{series_id}',
                         headers={'X-Api-Key': SONARR_API_KEY}, timeout=10)
        r.raise_for_status()
        series = r.json()
        lang = ((series.get('originalLanguage') or {}).get('name') or '').lower()
        if lang not in ASIAN_LANGUAGES:
            return None
        if series.get('rootFolderPath') == ASIAN_TV_ROOT:
            return None
        requests.put(f'{SONARR_URL}/api/v3/series/editor',
                     headers={'X-Api-Key': SONARR_API_KEY},
                     json={'seriesIds': [series_id],
                           'rootFolderPath': ASIAN_TV_ROOT,
                           'moveFiles': True},
                     timeout=30)
        log.info(f'Asian TV routing: moved series {series_id} ({lang}) to {ASIAN_TV_ROOT}')
        return ASIAN_TV_ROOT
    except Exception as e:
        log.error(f'Asian TV routing failed: {e}')
        return None


'''

DEC = "@app.route('/webhook/sonarr', methods=['POST'])"
assert DEC in t, "decorator anchor not found"
t = t.replace(DEC, FUNC + DEC, 1)

BRANCH_OLD = """            threading.Thread(target=handle_import_relabel, args=(data, 'Sonarr'), daemon=True).start()
    return jsonify({'status': 'ok'}), 200"""
BRANCH_NEW = """            threading.Thread(target=handle_import_relabel, args=(data, 'Sonarr'), daemon=True).start()
    elif event == 'SeriesAdd':
        threading.Thread(target=route_series_to_asian, args=(data,), daemon=True).start()
    return jsonify({'status': 'ok'}), 200"""
assert BRANCH_OLD in t, "branch anchor not found"
t = t.replace(BRANCH_OLD, BRANCH_NEW, 1)

p.write_text(t)
print("refimpl applied")
