"""Sonarr/Radarr health automation. Three jobs, all from the arrs' own APIs:

1. Outages. An indexer, the download client or a root folder failing in Sonarr/Radarr health, an arr not
   answering, or Deluge not answering us, for OUTAGE_ALERT_MIN minutes sends one push. Recovery after a
   push sends one more. Short blips (Deluge restarts, the nightly appdata backup) never push.
2. Removed from TheTVDB/TMDb. A series or movie newly named by RemovedSeriesCheck/RemovedMovieCheck gets
   one push. Whatever is already flagged the first time an arr is read is recorded silently.
3. Broken library files: paths in the arrs' "Unable to parse media info from file" errors. A file whose
   arr record still has no media info is rescanned (read-only), then looked at again on a later pass at
   least CONFIRM_GAP later. Still broken means confirmed. Shadow mode (the default) only reports what it
   would do. With apply on: a source torrent still in Deluge gets one recheck first; otherwise, or when the
   recheck didn't fix it, the file is deleted through the arr and the episode/movie searched. At most
   MAX_REPLACE_PER_PASS per pass, only for monitored items, and nothing at all when more than MASS_LIMIT
   files look broken at once (that is a mount or ffprobe problem, not that many bad files).

The caller supplies an `io` object (see ArrHealthIO in arr-webhook.py) and a state dict it persists.
"""
import re

OUTAGE_ALERT_MIN = 30
OUTAGE_STALE_SEC = 1800          # a condition not seen for this long starts its clock over
CONFIRM_GAP = 3600               # a broken file is confirmed only on a pass at least this much later
COPY_GRACE = 3600                # a file modified this recently may still be copying: leave it alone
MASS_LIMIT = 5
MAX_REPLACE_PER_PASS = 3
LOG_PAGE_SIZE = 500

OUTAGE_SOURCES = {
    'IndexerStatusCheck', 'IndexerLongTermStatusCheck', 'IndexerRssCheck', 'IndexerSearchCheck',
    'DownloadClientCheck', 'DownloadClientStatusCheck', 'RootFolderCheck',
}
REMOVED_SOURCES = {'RemovedSeriesCheck': 'TheTVDB', 'RemovedMovieCheck': 'TMDb'}
_REMOVED_RE = re.compile(r'([^,()]+?)\s*\((tvdbid|tmdbid)\s*(\d+)\)', re.I)
_MEDIAINFO_RE = re.compile(
    r'Unable to parse media info from file:\s*(/.+?\.(?:mkv|mp4|m4v|avi|ts|m2ts|wmv|mov|webm|mpe?g))(?=:|\s*$)', re.I)


def _mins(sec):
    return int(sec // 60)


def _label(key):
    who, _, what = key.partition(':')
    names = {
        'unreachable': f'{who} not answering', 'direct': 'Deluge not answering',
        'IndexerStatusCheck': f'{who} indexers failing', 'IndexerLongTermStatusCheck': f'{who} indexers failing',
        'IndexerRssCheck': f'{who} has no working RSS indexer', 'IndexerSearchCheck': f'{who} has no working search indexer',
        'DownloadClientCheck': f'{who} can\'t reach the download client',
        'DownloadClientStatusCheck': f'{who} download client failing', 'RootFolderCheck': f'{who} root folder missing',
    }
    return names.get(what, f'{who} {what}')


def advise_facts(event, who, what, minutes, active, site=''):
    """arr_health model facts: event kind, service, check name and minutes only. No titles, hosts or URLs."""
    return {'event': event, 'service': who.lower(), 'condition': what, 'minutes_down': minutes,
            'outage_alert_min': OUTAGE_ALERT_MIN, 'active_outages': active, 'site': site, 'rules_action': 'notify'}


def _advise(io, facts):
    """Hand the facts of a push that is about to go out to the caller's optional `advise` (log-only)."""
    fn = getattr(io, 'advise', None)
    if fn is None:
        return
    try:
        fn(facts)
    except Exception:  # noqa: BLE001
        pass


# ── 1. outages ────────────────────────────────────────────────────────────────

def health_conditions(who, items):
    """{key: message} for the health items that mean something is down."""
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        src = it.get('source') or ''
        if src in OUTAGE_SOURCES and it.get('type') in ('error', 'warning'):
            out[f'{who}:{src}'] = it.get('message') or src
    return out


def outage_tick(io, state, now):
    """Read health from each arr (and Deluge), push sustained outages and their recoveries.
    Returns the health items per arr so the caller can run removed_tick on the same read."""
    current, health = {}, {}
    for who in io.services:
        try:
            items = io.get(who, '/api/v3/health')
        except Exception as e:  # noqa: BLE001
            current[f'{who}:unreachable'] = f'{who} is not answering ({type(e).__name__})'
            continue
        health[who] = items
        current.update(health_conditions(who, items))
    try:
        deluge_up = io.deluge_ok()
    except Exception:  # noqa: BLE001
        deluge_up = False
    if not deluge_up:
        current['Deluge:direct'] = 'Deluge is not answering plex-automation'

    outages = state.setdefault('outages', {})
    for key, msg in current.items():
        o = outages.get(key)
        if o is None or now - o.get('last_seen', 0) > OUTAGE_STALE_SEC:
            o = outages[key] = {'since': now, 'alerted': False}
        o['last_seen'], o['message'] = now, msg
        if not o['alerted'] and now - o['since'] >= OUTAGE_ALERT_MIN * 60:
            who, _, what = key.partition(':')
            _advise(io, advise_facts('outage', who, what, _mins(now - o['since']), len(current)))
            io.push(_label(key), f'{msg}\nDown for {_mins(now - o["since"])} min.')
            io.record('arr-health', f'{_label(key)} for {_mins(now - o["since"])} min: {msg}')
            o['alerted'] = True
    for key in [k for k in outages if k not in current]:
        o = outages.pop(key)
        if o.get('alerted'):
            took = _mins(now - o['since'])
            who, _, what = key.partition(':')
            _advise(io, advise_facts('recovered', who, what, took, len(current)))
            io.push(f'{_label(key)}: recovered', f'Back after about {took} min.')
            io.record('arr-health', f'{_label(key)}: recovered after about {took} min')
    return health


# ── 2. removed from TheTVDB / TMDb ────────────────────────────────────────────

def removed_entries(items):
    """[(source_site, 'tvdbid'|'tmdbid', id, title)] named by the removed-series/movie health checks."""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get('source') not in REMOVED_SOURCES:
            continue
        site = REMOVED_SOURCES[it['source']]
        for m in _REMOVED_RE.finditer(it.get('message') or ''):
            title = re.sub(r'^\s*(Series|Movie)\s+', '', m.group(1)).strip()
            out.append((site, m.group(2).lower(), m.group(3), title))
    return out


def removed_tick(io, state, who, items):
    rem = state.setdefault('removed', {'baselined': [], 'seen': []})
    seen = set(rem['seen'])
    entries = removed_entries(items)
    first = who not in rem['baselined']
    for site, kind, ident, title in entries:
        key = f'{who}:{kind}:{ident}'
        if key in seen:
            continue
        seen.add(key)
        if first:
            continue
        _advise(io, advise_facts('removed', who, kind, 0, 0, site))
        io.push(f'{who}: removed from {site}',
                f'{title} ({kind} {ident}) no longer exists on {site}, so {who} can\'t refresh it. '
                f'Its files stay. It may have moved to a new {kind}.')
        io.record('arr-health', f'{who}: {title} ({kind} {ident}) was removed from {site}')
    if first:
        rem['baselined'].append(who)
    rem['seen'] = sorted(seen)


# ── 3. broken library files ───────────────────────────────────────────────────

def mediainfo_error_paths(records):
    """{path: newest error time} from arr log records."""
    out = {}
    for r in records or []:
        m = _MEDIAINFO_RE.search((r or {}).get('message') or '')
        if m:
            p = m.group(1)
            t = r.get('time') or ''
            if t > out.get(p, ''):
                out[p] = t
    return out


def has_mediainfo(f):
    mi = (f or {}).get('mediaInfo') or {}
    return bool(mi.get('videoCodec'))


def _under(path, root):
    root = (root or '').rstrip('/')
    return bool(root) and (path == root or path.startswith(root + '/'))


class _Lookup:
    """One pass's view of the arr libraries, fetched only when a path needs it."""

    def __init__(self, io):
        self.io, self._owners = io, {}

    def _owner_list(self, who):
        if who not in self._owners:
            self._owners[who] = self.io.get(who, '/api/v3/series' if who == 'Sonarr' else '/api/v3/movie') or []
        return self._owners[who]

    def find(self, who, path):
        """The file's record as {'who','owner_id','file','title','episode_ids','monitored'}, or None if the
        arr no longer has a file at that path (it was replaced or removed)."""
        owners = [o for o in self._owner_list(who) if _under(path, o.get('path'))]
        if not owners:
            return None
        owner = max(owners, key=lambda o: len(o.get('path') or ''))
        oid = owner.get('id')
        if who == 'Sonarr':
            files = self.io.get(who, '/api/v3/episodefile', {'seriesId': oid}) or []
        else:
            files = self.io.get(who, '/api/v3/moviefile', {'movieId': oid}) or []
        f = next((x for x in files if x.get('path') == path), None)
        if f is None:
            return None
        rec = {'who': who, 'owner_id': oid, 'file': f, 'title': owner.get('title') or '?',
               'episode_ids': [], 'monitored': bool(owner.get('monitored'))}
        if who == 'Sonarr':
            eps = self.io.get(who, '/api/v3/episode', {'seriesId': oid, 'episodeFileId': f.get('id')}) or []
            eps = [e for e in eps if e.get('episodeFileId') == f.get('id')]
            rec['episode_ids'] = [e['id'] for e in eps if e.get('id') is not None]
            rec['monitored'] = rec['monitored'] and any(e.get('monitored') for e in eps)
            if eps:
                e = eps[0]
                rec['title'] += f" S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}"
        return rec


def _source_hash(io, rec, path):
    """Deluge hash of the download the file was imported from, or None."""
    who, oid = rec['who'], rec['owner_id']
    try:
        if who == 'Sonarr':
            events = io.get(who, '/api/v3/history/series', {'seriesId': oid, 'eventType': 'downloadFolderImported'})
            events = [e for e in events or [] if e.get('episodeId') in rec['episode_ids']]
        else:
            events = io.get(who, '/api/v3/history/movie', {'movieId': oid, 'eventType': 'downloadFolderImported'}) or []
    except Exception:  # noqa: BLE001
        return None
    events = sorted((e for e in events if isinstance(e, dict)), key=lambda e: e.get('date') or '', reverse=True)
    exact = [e for e in events if ((e.get('data') or {}).get('importedPath') or '') == path]
    for e in exact or events:
        h = (e.get('downloadId') or '').lower()
        if h:
            return h
    return None


def _rescan(io, rec):
    if rec['who'] == 'Sonarr':
        io.post('Sonarr', '/api/v3/command', {'name': 'RescanSeries', 'seriesId': rec['owner_id']})
    else:
        io.post('Radarr', '/api/v3/command', {'name': 'RescanMovie', 'movieId': rec['owner_id']})


def _replace(io, rec):
    """Delete the broken file through the arr (its recycle bin applies) and search for a new copy."""
    fid = rec['file']['id']
    if rec['who'] == 'Sonarr':
        io.delete('Sonarr', f'/api/v3/episodefile/{fid}')
        io.post('Sonarr', '/api/v3/command', {'name': 'EpisodeSearch', 'episodeIds': rec['episode_ids']})
    else:
        io.delete('Radarr', f'/api/v3/moviefile/{fid}')
        io.post('Radarr', '/api/v3/command', {'name': 'MoviesSearch', 'movieIds': [rec['owner_id']]})


def broken_files_pass(io, state, now, apply=False):
    """One pass over the arrs' media-info errors. Returns the report rows (also kept in state)."""
    broken = state.setdefault('broken', {})
    fine = state.setdefault('fine', {})   # path -> newest error already dealt with (readable, replaced or gone)
    lookup = _Lookup(io)
    seen_paths = set()
    for who in io.services:
        try:
            recs = (io.get(who, '/api/v3/log', {'page': 1, 'pageSize': LOG_PAGE_SIZE, 'sortKey': 'time',
                                                'sortDirection': 'descending', 'level': 'error'}) or {}).get('records')
        except Exception as e:  # noqa: BLE001
            io.log(f'[arr-health] {who} log read failed ({type(e).__name__}); its files wait for the next pass')
            seen_paths.update(p for p, b in broken.items() if b.get('who') == who)
            continue
        for path, err_time in mediainfo_error_paths(recs).items():
            seen_paths.add(path)
            if path not in broken and fine.get(path) == err_time:
                continue   # already found readable after this error; no new error since
            if path not in broken:
                broken[path] = {'who': who, 'first_error': err_time, 'state': 'new'}
            broken[path]['last_error'] = err_time
    state['fine'] = fine = {p: t for p, t in fine.items() if p in seen_paths}
    # Entries no longer in either log and never confirmed are dropped; confirmed ones are kept until fixed.
    for path in [p for p, b in broken.items() if p not in seen_paths and b['state'] in ('new', 'copying')]:
        broken.pop(path)

    actionable = []
    for path, b in list(broken.items()):
        try:
            rec = lookup.find(b['who'], path)
        except Exception as e:  # noqa: BLE001
            b['note'] = f'lookup failed ({type(e).__name__}); retry next pass'
            continue
        if rec is None:
            if b['state'] not in ('new', 'copying'):
                io.record('arr-health', f'Broken file gone from {b["who"]} (replaced or removed): {path}')
            if b.get('last_error'):
                fine[path] = b['last_error']
            broken.pop(path)
            continue
        b['title'] = rec['title']
        if has_mediainfo(rec['file']):
            if b['state'] not in ('new', 'copying'):
                io.record('arr-health', f'{rec["title"]}: file reads fine again after {b["state"]}')
            if b.get('last_error'):
                fine[path] = b['last_error']
            broken.pop(path)
            continue
        mtime = io.file_mtime(path)
        if mtime is not None and now - mtime < COPY_GRACE:
            b.update(state='copying', note='modified in the last hour; may still be copying')
            continue
        if b['state'] in ('new', 'copying'):
            try:
                _rescan(io, rec)
                b['note'] = f'rescan asked of {rec["who"]}; confirm on a later pass'
            except Exception as e:  # noqa: BLE001
                b['note'] = f'rescan failed ({type(e).__name__})'
            b.update(state='rescanned', since=now)
            continue
        if now - b.get('since', now) < CONFIRM_GAP:
            continue
        if b['state'] == 'rescanned':
            b['state'] = 'confirmed'
            io.record('arr-health', f'Broken file confirmed in {rec["who"]}: {rec["title"]} ({path.rsplit("/", 1)[-1]})')
        actionable.append((path, b, rec))

    mass = len(actionable) > MASS_LIMIT
    if mass and not state.get('mass_alerted'):
        io.push('Many broken files at once', f'{len(actionable)} library files have no media info. That looks like '
                f'a mount or ffprobe problem, so nothing was changed. See /digest.')
        io.record('arr-health', f'{len(actionable)} broken files at once; changed nothing')
    state['mass_alerted'] = mass

    replaced = 0
    for path, b, rec in actionable:
        h = None if b.get('rechecked') else _source_hash(io, rec, path)
        in_deluge = False
        if h:
            try:
                in_deluge = bool(io.deluge_has(h))
            except Exception:  # noqa: BLE001
                in_deluge = False
        if in_deluge:
            b['would'] = 'recheck the source torrent in Deluge'
        elif not rec['monitored']:
            b['would'] = 'nothing: not monitored, so no search would follow a delete'
        else:
            b['would'] = f'delete the file in {rec["who"]} and search for a new copy'
        if not apply or mass:
            b['outcome'] = 'held: too many at once' if mass else 'dry run'
            continue
        if not rec['monitored'] and not in_deluge:
            b['outcome'] = 'left alone (not monitored)'
            continue
        try:
            if in_deluge:
                io.deluge_recheck(h)
                b.update(rechecked=True, state='rechecked', since=now, outcome='rechecked torrent')
                io.record('arr-health', f'{rec["title"]}: broken file, rechecking its torrent in Deluge')
            elif replaced >= MAX_REPLACE_PER_PASS:
                b['outcome'] = f'waiting: {MAX_REPLACE_PER_PASS} per pass'
            else:
                _replace(io, rec)
                replaced += 1
                if b.get('last_error'):
                    fine[path] = b['last_error']
                broken.pop(path)
                io.record('arr-health', f'{rec["title"]}: deleted broken file {path.rsplit("/", 1)[-1]} and searched')
        except Exception as e:  # noqa: BLE001
            b['outcome'] = f'error: {type(e).__name__}'
    state['broken_pass'] = {'ts': now, 'apply': bool(apply), 'mass': mass}
    return [dict(b, path=p) for p, b in broken.items()]


# ── /digest ───────────────────────────────────────────────────────────────────

def digest_html(state, fmt_ts=str):
    esc = lambda v: str('' if v is None else v).replace('&', '&amp;').replace('<', '&lt;')  # noqa: E731
    parts = []
    outs = state.get('outages') or {}
    if outs:
        rows = ''.join(f"<tr><td>{esc(_label(k))}</td><td>{esc(fmt_ts(o.get('since')))}</td>"
                       f"<td>{'pushed' if o.get('alerted') else 'watching'}</td><td>{esc(o.get('message'))}</td></tr>"
                       for k, o in sorted(outs.items()))
        parts.append(f"<h2>Down right now: {len(outs)}</h2><table>{rows}</table>")
    bp = state.get('broken_pass')
    broken = state.get('broken') or {}
    if bp:
        head = ('apply' if bp.get('apply') else 'dry run, nothing changed') + f" - last pass {esc(fmt_ts(bp.get('ts')))}"
        rows = ''.join(
            f"<tr><td>{esc(b.get('who'))}</td><td>{esc(b.get('title') or p.rsplit('/', 1)[-1])}</td>"
            f"<td>{esc(b.get('state'))}</td><td>{esc(b.get('outcome') or b.get('note') or '')}</td>"
            f"<td>{esc(b.get('would') or '')}</td></tr>" for p, b in sorted(broken.items()))
        parts.append(f"<h2>Broken library files: {len(broken)}</h2><div class='sub'>{head}</div>"
                     f"<table><tr><td><b>arr</b></td><td><b>item</b></td><td><b>state</b></td><td><b>result</b></td>"
                     f"<td><b>apply would</b></td></tr>{rows or '<tr><td>none</td></tr>'}</table>")
    return ''.join(parts)
