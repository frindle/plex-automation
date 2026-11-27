"""SABnzbd side of the watch hold (watch_hold.py): a Usenet upgrade is held like a Deluge download.

The watch hold keeps an upgrade from being imported while someone streams the item it replaces. For a
torrent it moves the download to a hold label the *arr does not read, and puts the label back on release.
A SABnzbd download is held by pausing it: mode=queue&name=pause, resumed on release. Its category is
never touched (the *arr keeps its own category, and SABnzbd's change_cat only works on queued jobs, so a
category swap would also leave the release unable to put it back). A paused job stops downloading, so it
cannot finish and be imported; the *arr sees it as paused. rows() reports a paused job under the hold
label, so watch_hold treats it like a Deluge download wearing the hold label.

Only a queued download can be paused. One that completed before the hold was wanted (in SABnzbd's history)
is imported as usual (the `usenet_late` count in the log). With the cap at 750 Mbit/s an episode can finish
within a pass, so the hold is taken as soon as the download is tracked and someone streams the item or a
resume point has to be mapped, not at the half-way mark that torrents use.

This module only talks to SABnzbd and maps its rows onto the shape watch_hold already reads from Deluge
({label, is_finished, progress}). Logs carry nzo ids and counts only; the API key is never logged and is
not part of any exception text.
"""
from __future__ import annotations

import os
import posixpath

VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.wmv')
ID_PREFIX = 'sabnzbd_nzo_'
HOLD_LABEL = 'watch-hold'   # what a paused job is reported as; same as watch_hold.HOLD_LABEL
_TIMEOUT = 20


class SabError(Exception):
    """A SABnzbd call failed. The message never carries the URL or key."""


def is_usenet_id(h) -> bool:
    return str(h or '').lower().startswith(ID_PREFIX)


class Sab:
    def __init__(self, url, key, http=None):
        self.url = (url or '').rstrip('/')
        self._key = key or ''
        self._ids = {}   # lowercase nzo id -> the id as SABnzbd spelled it (the *arr's downloadId is lowercased here)
        if http is None:
            import requests
            http = requests
        self._http = http

    def __repr__(self):
        return 'Sab(<url>, <key>)'

    @property
    def configured(self):
        return bool(self.url and self._key)

    def _call(self, mode, **params):
        q = {'mode': mode, 'output': 'json', 'apikey': self._key, **params}
        try:
            r = self._http.get(f'{self.url}/api', params=q, timeout=_TIMEOUT)
            r.raise_for_status()
            doc = r.json()
        except Exception as e:  # noqa: BLE001
            raise SabError(f'{mode} failed: {type(e).__name__}') from None
        if isinstance(doc, dict) and doc.get('status') is False:
            raise SabError(f'{mode} refused')
        return doc

    def queue(self):
        return ((self._call('queue') or {}).get('queue') or {}).get('slots') or []

    def history(self, limit=200):
        return ((self._call('history', limit=limit) or {}).get('history') or {}).get('slots') or []

    def original_id(self, h):
        h = str(h)
        return self._ids.get(h.lower()) or ('SABnzbd_nzo_' + h[len(ID_PREFIX):])

    def pause(self, h):
        self._call('queue', name='pause', value=self.original_id(h))

    def resume(self, h):
        self._call('queue', name='resume', value=self.original_id(h))


def rows(sab: Sab):
    """{lowercase nzo id: {'label', 'is_finished', 'progress', 'usenet': True, ...}} for the queue and the
    history. The id is the *arr's downloadId. A row also in the history wins over its queue row."""
    out = {}
    for s in sab.queue():
        h = str(s.get('nzo_id') or '').lower()
        if not h:
            continue
        sab._ids[h] = s['nzo_id']
        try:
            pct = float(s.get('percentage') or 0)
        except (TypeError, ValueError):
            pct = 0.0
        paused = str(s.get('status') or '').lower() == 'paused'
        out[h] = {'label': HOLD_LABEL if paused else str(s.get('cat') or ''), 'is_finished': False, 'progress': pct, 'usenet': True,
                  'status': str(s.get('status') or '')}
    for s in sab.history():
        h = str(s.get('nzo_id') or '').lower()
        if not h:
            continue
        sab._ids[h] = s['nzo_id']
        status = str(s.get('status') or '').lower()
        out[h] = {'label': str(s.get('category') or ''), 'is_finished': status == 'completed',
                  'progress': 100.0 if status == 'completed' else 0.0, 'usenet': True, 'in_history': True, 'status': status,
                  'storage': s.get('storage') or ''}
    return out


def parse_path_map(spec):
    """'sab path=local path,...' to a list of (from, to) pairs, longest source first."""
    pairs = []
    for part in (spec or '').split(','):
        if '=' in part:
            a, b = part.split('=', 1)
            if a.strip() and b.strip():
                pairs.append((a.strip().rstrip('/'), b.strip().rstrip('/')))
    return sorted(pairs, key=lambda p: -len(p[0]))


def to_local(path, pairs):
    for a, b in pairs:
        if path == a or path.startswith(a + '/'):
            return b + path[len(a):]
    return path


def biggest_video(sab: Sab, h, pairs, walk=os.walk, getsize=os.path.getsize):
    """The completed download's biggest video file, as the path the *arr sees it, or None."""
    row = rows(sab).get(h.lower())
    if not row or not row.get('is_finished') or not row.get('storage'):
        return None
    root = to_local(row['storage'], pairs)
    best, size = None, -1
    if os.path.isfile(root):
        return root if root.lower().endswith(VIDEO_EXTS) else None
    for d, _dirs, files in walk(root):
        for f in files:
            if f.lower().endswith(VIDEO_EXTS):
                p = posixpath.join(d, f)
                try:
                    sz = getsize(p)
                except OSError:
                    continue
                if sz > size:
                    best, size = p, sz
    return best
