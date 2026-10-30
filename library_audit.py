"""Read-only audit of torrents that seed from the Plex library.

Flags (never touches) torrents that are in Error / report missing data, and
torrents whose save_path is under /data/Media while still carrying a Sonarr or
Radarr label (an arr upgrade can delete those library files out from under
the torrent). Each flagged torrent is pushed at most once per `reported` set.
"""
import posixpath

MEDIA_ROOT = '/data/Media'
ARR_LABEL_PREFIXES = ('sonarr', 'radarr')
MISSING_DATA = 'missing or invalid torrent data'
FIELDS = ['name', 'state', 'message', 'save_path', 'label']


def fetch_torrents(rpc):
    return rpc('core.get_torrents_status', [{}, FIELDS]) or {}


def under_media(path):
    if not path:
        return False
    p = posixpath.normpath(path).lower()
    root = MEDIA_ROOT.lower()
    return p == root or p.startswith(root + '/')


def is_arr_label(label):
    return (label or '').lower().startswith(ARR_LABEL_PREFIXES)


def audit_reasons(t):
    reasons = []
    if t.get('state') == 'Error' or MISSING_DATA in (t.get('message') or '').lower():
        reasons.append('error')
    if under_media(t.get('save_path')) and is_arr_label(t.get('label')):
        reasons.append('arr-label-in-library')
    return reasons


def library_seed_audit(torrents, notify, reported, dry_run=False):
    result = {'flagged': [], 'notified': [], 'counts': {'error': 0, 'arr-label-in-library': 0}}
    for h in sorted(torrents):
        t = torrents[h] or {}
        reasons = audit_reasons(t)
        if not reasons:
            continue
        name, save_path = t.get('name', ''), t.get('save_path', '')
        result['flagged'].append({'hash': h, 'name': name, 'label': t.get('label', ''),
                                  'save_path': save_path, 'state': t.get('state', ''),
                                  'reasons': reasons})
        for r in reasons:
            result['counts'][r] += 1
        if dry_run or h in reported:
            continue
        reported.add(h)
        notify(f'Library seed audit: {name}', f'{", ".join(reasons)} at {save_path}')
        result['notified'].append(h)
    return result
