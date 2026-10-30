"""Read-only audit of torrents that seed from the Plex library.

Flags (never touches) torrents that are in Error / report missing data, and
torrents whose save_path is under /data/Media while still carrying a Sonarr or
Radarr label (an arr upgrade can delete those library files out from under
the torrent). A run sends at most ONE summary push, and only for torrents not
already in the caller's `reported` set; each is then recorded so it is never
announced again.
"""
import posixpath

MEDIA_ROOT = '/data/Media'
ARR_LABEL_PREFIXES = ('sonarr', 'radarr')
MISSING_DATA = 'missing or invalid torrent data'
FIELDS = ['name', 'state', 'message', 'save_path', 'label']
SUMMARY_NAMES = 10


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


def summarize(entries, limit=SUMMARY_NAMES):
    counts = {'error': 0, 'arr-label-in-library': 0}
    for f in entries:
        for r in f['reasons']:
            counts[r] += 1
    lines = [f'error {counts["error"]}, arr-label-in-library {counts["arr-label-in-library"]}']
    lines += [f'- {f["name"]}: {", ".join(f["reasons"])}' for f in entries[:limit]]
    if len(entries) > limit:
        lines.append(f'... and {len(entries) - limit} more')
    return '\n'.join(lines)


def library_seed_audit(torrents, notify, reported, dry_run=False):
    result = {'flagged': [], 'notified': [], 'counts': {'error': 0, 'arr-label-in-library': 0}}
    new = []
    for h in sorted(torrents):
        t = torrents[h] or {}
        reasons = audit_reasons(t)
        if not reasons:
            continue
        entry = {'hash': h, 'name': t.get('name', ''), 'label': t.get('label', ''),
                 'save_path': t.get('save_path', ''), 'state': t.get('state', ''),
                 'reasons': reasons}
        result['flagged'].append(entry)
        for r in reasons:
            result['counts'][r] += 1
        if not dry_run and h not in reported:
            new.append(entry)
    if new:
        reported.update(e['hash'] for e in new)
        notify(f'Library seed audit: {len(new)} newly flagged', summarize(new))
        result['notified'] = [e['hash'] for e in new]
    return result
