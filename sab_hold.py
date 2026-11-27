"""SABnzbd side of the watch hold (watch_hold.py): a Usenet upgrade is held like a Deluge download, in its own category.

The watch hold keeps an upgrade from being imported while someone streams the item it replaces. A Usenet
download is not paused to do that (a paused job never finishes, so there would be no new file to map a resume
point onto): the *arr's SABnzbd client adds every job Paused, watch_hold.classify_usenet looks each one up in
the *arr's queue, and an upgrade is moved to the hold category with `change_cat` (which only works on a queued
job) before it is resumed. It downloads to completion in the hold category, where the *arr cannot see it. The
release is an import through the *arr's own scan command (arr-webhook.py), after which the SABnzbd history entry
and its leftover files are deleted. A job already in SABnzbd's history when first seen is only counted
(`usenet_late`).

This module only talks to SABnzbd and maps its rows onto the shape watch_hold already reads from Deluge
({label, is_finished, progress}). Logs carry nzo ids and counts only; the API key is never logged and is
not part of any exception text.
"""
from __future__ import annotations

import os
import posixpath

VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.wmv')
ID_PREFIX = 'sabnzbd_nzo_'
HOLD_LABEL = 'watch-hold'   # the hold category; same as watch_hold.HOLD_LABEL
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

    def resume(self, h):
        self._call('queue', name='resume', value=self.original_id(h))

    def change_cat(self, h, cat):
        """Move a queued job to another category (SABnzbd refuses once it is in the history)."""
        self._call('change_cat', value=self.original_id(h), value2=cat)

    def delete_history(self, h):
        """Remove a job from the history together with its leftover files."""
        self._call('history', name='delete', value=self.original_id(h), del_files=1)


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
        status = str(s.get('status') or '')
        out[h] = {'label': str(s.get('cat') or ''), 'is_finished': False, 'progress': pct, 'usenet': True,
                  'status': status, 'paused': status.lower() == 'paused'}
    for s in sab.history():
        h = str(s.get('nzo_id') or '').lower()
        if not h:
            continue
        sab._ids[h] = s['nzo_id']
        status = str(s.get('status') or '').lower()
        out[h] = {'label': str(s.get('category') or ''), 'is_finished': status == 'completed',
                  'progress': 100.0 if status == 'completed' else 0.0, 'usenet': True, 'in_history': True, 'status': status,
                  'failed': status == 'failed', 'storage': s.get('storage') or ''}
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


def to_arr(path, complete, visible):
    """`path` (as SABnzbd wrote it) as the *arrs see it: the complete folder's prefix swapped for the *arrs' view of it.
    Unchanged when both are the same folder, unset, or the path is outside the complete folder."""
    complete, visible = (complete or '').rstrip('/'), (visible or '').rstrip('/')
    if complete and visible and complete != visible and (path == complete or path.startswith(complete + '/')):
        return visible + path[len(complete):]
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
