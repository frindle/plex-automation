"""Add the glacier hold root's two folders as extra folders of the Plex TV and Movie libraries.

usage: PLEX_TOKEN=... python plex_hold_folders.py --plex-url URL --tv SECTION_ID --movie SECTION_ID
           [--root "/data/Glacier Hold"] [--apply]

Dry run by default: reads /library/sections and prints what would be added, as counts. --apply adds the
folders: PUT /library/sections/{id} with the section's existing locations plus the new one, as repeated
`location` parameters (the form python-plexapi uses for LibrarySection.edit). The two folders must exist
on disk first (`<root>/TV Shows`, `<root>/Movies`). Existing locations are never removed. Run once per
deploy; the arr-webhook never changes Plex libraries itself. Prints counts only, no paths or titles.
"""
import argparse
import os
import sys

import requests


def locations(section):
    return [loc.get('path') for loc in (section.get('Location') or []) if loc.get('path')]


def plan(sections, wanted):
    """wanted = {section_id: folder}. Returns {section_id: full location list, None when already there,
    or 'missing' when the section does not exist}."""
    by_id = {str(s.get('key')): s for s in sections}
    out = {}
    for sid, folder in wanted.items():
        sec = by_id.get(str(sid))
        if sec is None:
            out[sid] = 'missing'
            continue
        have = locations(sec)
        out[sid] = None if folder in have else have + [folder]
    return out


def main(argv=None, get=requests.get, put=requests.put, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--plex-url', required=True)
    ap.add_argument('--tv', required=True, help='section id of the TV library')
    ap.add_argument('--movie', required=True, help='section id of the Movie library')
    ap.add_argument('--root', default='/data/Glacier Hold')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args(argv)
    token = env.get('PLEX_TOKEN', '')
    if not token:
        print('PLEX_TOKEN not set')
        return 2
    headers = {'Accept': 'application/json', 'X-Plex-Token': token}
    r = get(f'{args.plex_url}/library/sections', headers=headers, timeout=30)
    r.raise_for_status()
    sections = ((r.json() or {}).get('MediaContainer') or {}).get('Directory') or []
    root = args.root.rstrip('/')
    wanted = {args.tv: root + '/TV Shows', args.movie: root + '/Movies'}
    todo = plan(sections, wanted)
    print('sections_missing=%d' % sum(1 for v in todo.values() if v == 'missing'))
    print('already_present=%d' % sum(1 for v in todo.values() if v is None))
    adds = {k: v for k, v in todo.items() if isinstance(v, list)}
    print('to_add=%d' % len(adds))
    if not args.apply:
        print('dry run: nothing changed')
        return 0
    failed = 0
    for sid, locs in adds.items():
        resp = put(f'{args.plex_url}/library/sections/{sid}', headers=headers, params={'location': locs}, timeout=30)
        if getattr(resp, 'status_code', 200) >= 300:
            failed += 1
    print('added=%d' % (len(adds) - failed))
    print('failed=%d' % failed)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
