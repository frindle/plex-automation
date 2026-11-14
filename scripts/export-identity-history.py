"""Export Sonarr/Radarr grab history for plex import-identity back-tests. Read-only (GETs only).

Run inside the arr-webhook container, which already has the Sonarr/Radarr URLs and keys in its env:
    docker exec -i arr-webhook python3 - < scripts/export-identity-history.py > identity-history.json
PAGES (env, default 8) pages of 250 grabs per service, newest first, so older grabs that earlier
back-tests never saw are included. Prints no key, URL or file path; writes JSON to stdout.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PAGE = 250


def get(base, key, path, params):
    url = base.rstrip('/') + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'X-Api-Key': key, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def service_rows(name, base, key, kind, pages):
    rows, errors = [], []
    if not (base and key):
        return rows, ['%s: not configured' % name]
    for page in range(1, pages + 1):
        try:
            h = get(base, key, '/api/v3/history', {'page': page, 'pageSize': PAGE, 'sortKey': 'date',
                                                   'sortDirection': 'descending', 'eventType': 1,
                                                   'include' + kind.capitalize(): 'true'})
        except Exception as e:  # the key only travels in a header; keep the error short
            errors.append('%s history page %d: %s' % (name, page, type(e).__name__))
            break
        recs = h.get('records') or []
        for rec in recs:
            it = rec.get(kind) or {}
            rows.append({'service': name, 'source': 'history-grabbed', 'date': rec.get('date'),
                         'release_title': rec.get('sourceTitle'), 'item_title': it.get('title'),
                         'item_year': it.get('year')})
        if len(recs) < PAGE:
            break
    return rows, errors


def main():
    pages = max(1, min(40, int(os.environ.get('PAGES') or 8)))
    out = {'exported': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'pages': pages, 'rows': [], 'errors': []}
    for name, kind in (('Radarr', 'movie'), ('Sonarr', 'series')):
        rows, errs = service_rows(name, os.environ.get(name.upper() + '_URL'),
                                  os.environ.get(name.upper() + '_API_KEY'), kind, pages)
        out['rows'] += rows
        out['errors'] += errs
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write('\n')
    print('rows: %d, errors: %d' % (len(out['rows']), len(out['errors'])), file=sys.stderr)


if __name__ == '__main__':
    main()
