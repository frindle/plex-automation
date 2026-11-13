"""Unpack RAR releases that sit in the library once nothing seeds them.

Plex cannot play .rar volumes. Unpackerr handles releases in Downloads (the
*arr imports the extracted file); this covers RAR sets under the library
(library-seed torrents, old packs) after their torrent is removed with the
files kept, plus a scan for sets no torrent owns any more.

Extraction goes into a hidden temp folder next to the set, files are moved
into place only when unar succeeded, and the volumes are deleted last. Any
failure leaves the volumes untouched. Nothing here deletes a volume that a
live torrent still owns: callers pass only unowned sets.
"""
import json
import os
import re
import shutil
import subprocess

UNAR = os.environ.get('UNAR_BIN', 'unar')
LSAR = os.environ.get('LSAR_BIN', 'lsar')
EXTRACT_TIMEOUT = int(os.environ.get('RAR_EXTRACT_TIMEOUT', '3600'))
MAX_ATTEMPTS = 3

_PART = re.compile(r'^(?P<stem>.+)\.part(?P<n>\d+)\.rar$', re.I)
_OLD_RAR = re.compile(r'^(?P<stem>.+)\.rar$', re.I)
_OLD_VOL = re.compile(r'^(?P<stem>.+)\.[rs]\d{2,3}$', re.I)


def rar_sets(paths):
    """Group file paths into RAR sets.
    -> [{'first': path, 'volumes': [paths], 'extras': [.sfv paths]}]
    New style name.partNN.rar (first = lowest part number) and old style
    name.rar + name.r00.. / name.s00.. (first = name.rar). A set without its
    first volume is skipped (incomplete, cannot be extracted)."""
    groups = {}
    sfv = {}
    for p in paths:
        d, base = os.path.split(p)
        m = _PART.match(base)
        if m:
            g = groups.setdefault((d, m.group('stem').lower(), 'part'), {'vols': [], 'first': None, 'n': None})
            g['vols'].append(p)
            n = int(m.group('n'))
            if g['n'] is None or n < g['n']:
                g['n'], g['first'] = n, p
            continue
        m = _OLD_RAR.match(base)
        if m:
            g = groups.setdefault((d, m.group('stem').lower(), 'old'), {'vols': [], 'first': None, 'n': None})
            g['vols'].append(p)
            g['first'] = p
            continue
        m = _OLD_VOL.match(base)
        if m:
            g = groups.setdefault((d, m.group('stem').lower(), 'old'), {'vols': [], 'first': None, 'n': None})
            g['vols'].append(p)
            continue
        if base.lower().endswith('.sfv'):
            sfv.setdefault((d, base[:-4].lower()), []).append(p)
    out = []
    for (d, stem, kind), g in sorted(groups.items()):
        if not g['first'] or (kind == 'part' and g['n'] not in (0, 1)):
            continue
        out.append({'first': g['first'], 'volumes': sorted(g['vols']), 'extras': sfv.get((d, stem), [])})
    return out


def has_rar_volumes(paths):
    """True if any path names a RAR volume (.rar, .partNN.rar, .rNN, .sNN)."""
    for p in paths:
        base = os.path.basename(p or '')
        if _PART.match(base) or _OLD_RAR.match(base) or _OLD_VOL.match(base):
            return True
    return False


def _lsar_entries(first, run):
    """File entries (directories left out) of a RAR set via lsar -j.
    -> {'ok': True, 'entries': [dict, ...]} or {'ok': False, 'error': str}"""
    try:
        r = run([LSAR, '-j', first], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return {'ok': False, 'error': f'lsar failed: {str(e)[:200]}'}
    if r.returncode != 0:
        return {'ok': False, 'error': f'lsar exit {r.returncode}: {(r.stderr or r.stdout or "").strip()[:300]}'}
    try:
        entries = json.loads(r.stdout).get('lsarContents')
    except (ValueError, AttributeError):
        return {'ok': False, 'error': 'lsar output not JSON'}
    if not isinstance(entries, list):
        return {'ok': False, 'error': 'lsar output has no lsarContents'}
    files = [e for e in entries
             if isinstance(e, dict) and isinstance(e.get('XADFileName'), str) and not e.get('XADIsDirectory')]
    return {'ok': True, 'entries': files}


def list_contents(first, run=subprocess.run):
    """Inner file names of a RAR set (directories left out), via lsar -j.
    -> {'ok': True, 'files': [...]} or {'ok': False, 'error': str}"""
    lst = _lsar_entries(first, run)
    if not lst['ok']:
        return lst
    return {'ok': True, 'files': [e['XADFileName'] for e in lst['entries']]}


def _indexes_for(first, names, run):
    """lsar indexes of the inner files `names`. unar reads file arguments as
    wildcard patterns, so a name with [ ] * ? would not match itself; asking by
    index (unar -indexes) extracts exactly these files.
    -> {'ok': True, 'indexes': [str, ...]} or {'ok': False, 'error': str}"""
    lst = _lsar_entries(first, run)
    if not lst['ok']:
        return lst
    by_name = {}
    for e in lst['entries']:
        if isinstance(e.get('XADIndex'), int):
            by_name.setdefault(e['XADFileName'], e['XADIndex'])
    missing = [n for n in names if n not in by_name]
    if missing:
        return {'ok': False, 'error': f'not in the archive: {missing[0][:200]}'}
    return {'ok': True, 'indexes': [str(by_name[n]) for n in names]}


def delete_set(rset, remove=os.remove):
    """Delete a set's volumes and .sfv files, first volume last.
    -> {'ok': bool, 'removed': [...], 'error': str}
    Stops at the first failure; the files already removed are listed."""
    removed = []
    first = rset.get('first')
    # The first volume goes last, so a retry after a partial failure can
    # still list the set's contents.
    order = [v for v in rset.get('volumes') or [] if v != first] + list(rset.get('extras') or [])
    if first:
        order.append(first)
    for v in order:
        try:
            remove(v)
        except FileNotFoundError:
            continue
        except OSError as e:
            return {'ok': False, 'removed': removed, 'error': f'could not delete {v}: {e}'}
        removed.append(v)
    return {'ok': True, 'removed': removed}


def _walk_files(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith('.')]
        for n in files:
            yield os.path.join(d, n)


def extract(rset, run=subprocess.run, disk_usage=shutil.disk_usage, keep_volumes=False, only=None):
    """Extract one set in place. -> {'ok': bool, 'files': [...], 'error': str}
    Volumes are deleted only after every extracted file is moved into place.
    keep_volumes=True (a torrent still seeds the set): `only` must list the
    inner file names to extract, and the volumes and .sfv are never deleted.
    Default behaviour (keep_volumes=False) is unchanged."""
    if keep_volumes and not only:
        return {'ok': False, 'error': 'keep_volumes needs the inner files to extract'}
    first = rset['first']
    d = os.path.dirname(first)
    vols = rset['volumes']
    try:
        if not all(os.path.isfile(v) for v in vols):
            return {'ok': False, 'error': 'volume missing'}
        need = sum(os.path.getsize(v) for v in vols)
        if disk_usage(d).free < need * 1.1:
            return {'ok': False, 'error': f'not enough free space ({need / 1e9:.1f} GB needed)'}
    except OSError as e:
        return {'ok': False, 'error': f'stat failed: {e}'}
    pick = []
    if only:
        ix = _indexes_for(first, list(only), run)
        if not ix['ok']:
            return {'ok': False, 'error': ix['error']}
        pick = ['-indexes']
    tmp = os.path.join(d, '.unrar-' + os.path.basename(first))
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(tmp)
        r = run([UNAR, '-q', '-no-directory', '-force-overwrite'] + pick + ['-output-directory', tmp, first]
                + (ix['indexes'] if only else []),
                capture_output=True, text=True, timeout=EXTRACT_TIMEOUT)
        if r.returncode != 0:
            return {'ok': False, 'error': f'unar exit {r.returncode}: {(r.stderr or r.stdout or "").strip()[:300]}'}
        got = [p for p in _walk_files(tmp) if os.path.getsize(p) > 0]
        if not got:
            return {'ok': False, 'error': 'nothing extracted'}
        moves = []
        for p in got:
            dest = os.path.join(d, os.path.relpath(p, tmp))
            if os.path.exists(dest):
                return {'ok': False, 'error': f'target exists: {dest}'}
            moves.append((p, dest))
        for src, dest in moves:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(src, dest)
    except (OSError, subprocess.SubprocessError) as e:
        return {'ok': False, 'error': str(e)[:300]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not keep_volumes:
        for v in vols + list(rset.get('extras') or []):
            try:
                os.remove(v)
            except OSError:
                pass
    return {'ok': True, 'files': [dest for _, dest in moves]}


def load_queue(path):
    try:
        with open(path) as f:
            q = json.load(f)
        return q if isinstance(q, list) else []
    except (OSError, ValueError):
        return []


def save_queue(path, q):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(q, f)
    os.replace(tmp, path)


def enqueue(q, rset, source, now):
    if any(e.get('first') == rset['first'] for e in q):
        return False
    q.append(dict(rset, source=source, added=now, attempts=0))
    return True
