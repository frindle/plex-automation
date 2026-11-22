"""The move list: what to put on the cloud Plex before the home server goes away. Read-only on Plex.

Per user (the owner, plus every shared/managed user that holds a server token):
  * auto: the next AUTO_NEXT unwatched episodes of every show the user watched in the last AUTO_DAYS days,
    counted after the last episode they watched, plus movies in progress;
  * hand-picked: everything in the user's Plex playlist named PLAYLIST (episodes, movies, seasons or whole shows).
De-duplicated by file path, so one file wanted by several users counts once.

Everything here is pure: the caller passes `get(path, params, token)` (the local Plex server, JSON) and
`tv_get(url, token)` (plex.tv, XML text). Tokens are never returned or logged.
"""
import xml.etree.ElementTree as ET

AUTO_DAYS = 30
AUTO_NEXT = 3
PLAYLIST = 'Move'
GB = 1000 ** 3


def _container(resp):
    return (resp or {}).get('MediaContainer') or {}


def _metadata(resp):
    c = _container(resp)
    out = list(c.get('Metadata') or [])
    for h in c.get('Hub') or []:  # hubs (continue watching) nest their items one level down
        out += h.get('Metadata') or []
    return out


def _part(item):
    """(file, size_bytes) of an item's first media part, or (None, 0)."""
    for m in item.get('Media') or []:
        for p in m.get('Part') or []:
            return p.get('file'), int(p.get('size') or 0)
    return None, 0


def _entry(item, reason, user):
    f, size = _part(item)
    t = item.get('type')
    if t == 'episode':
        title = '%s S%02dE%02d' % (item.get('grandparentTitle') or '?', int(item.get('parentIndex') or 0),
                                   int(item.get('index') or 0))
    else:
        title = item.get('title') or '?'
        if item.get('year'):
            title += ' (%s)' % item['year']
    return {'key': f or ('rk:%s' % item.get('ratingKey')), 'file': f, 'title': title, 'type': t,
            'size': size, 'reasons': {reason}, 'users': {user}}


def users_from_shared_servers(xml_text):
    """plex.tv shared_servers XML -> [{'label', 'token'}]. Entries without an access token are skipped."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for s in root.iter('SharedServer'):
        tok = s.get('accessToken')
        if tok:
            out.append({'label': s.get('username') or ('user-%s' % s.get('userID')), 'token': tok})
    return out


def list_users(get, tv_get, owner_token):
    """The owner first, then every shared/managed user of this server that has a server token."""
    users = [{'label': 'owner', 'token': owner_token}]
    try:
        machine = _container(get('/identity', None, owner_token)).get('machineIdentifier')
        if machine:
            users += users_from_shared_servers(tv_get('https://plex.tv/api/servers/%s/shared_servers' % machine, owner_token))
    except Exception:
        pass  # the owner's own list still works
    seen, uniq = set(), []
    for u in users:
        if u['token'] not in seen:
            seen.add(u['token'])
            uniq.append(u)
    return uniq


def _leaves(get, rating_key, token, kind='allLeaves'):
    return _metadata(get('/library/metadata/%s/%s' % (rating_key, kind), None, token))


def next_unwatched(episodes, n=AUTO_NEXT):
    """The first n unwatched (or part-watched) episodes after the last watched one, in air order."""
    eps = sorted(episodes, key=lambda e: (int(e.get('parentIndex') or 0), int(e.get('index') or 0)))
    last = -1
    for i, e in enumerate(eps):
        if int(e.get('viewCount') or 0) > 0:
            last = i
    return [e for e in eps[last + 1:] if int(e.get('viewCount') or 0) == 0][:n]


def show_id(row):
    """The show's ratingKey for an episode row. Play history rows carry grandparentKey ("/library/metadata/<id>"),
    not always grandparentRatingKey, so fall back to the key's last part."""
    v = row.get('grandparentRatingKey')
    if v:
        return str(v)
    k = str(row.get('grandparentKey') or '')
    if k.startswith('/library/metadata/'):
        return k.rstrip('/').rsplit('/', 1)[-1] or None
    return None


def auto_items(get, token, user, now, days=AUTO_DAYS, nxt=AUTO_NEXT):
    out = []
    since = int(now - days * 86400)
    params = {'viewedAt>': since, 'sort': 'viewedAt:desc'}
    if user == 'owner':
        params['accountID'] = 1
    shows = {}
    for h in _metadata(get('/status/sessions/history/all', params, token)):
        sid = show_id(h) if h.get('type') == 'episode' else None
        if sid:
            shows.setdefault(sid, True)
    for rk in shows:
        for e in next_unwatched(_leaves(get, rk, token), nxt):
            out.append(_entry(e, 'auto', user))
    for m in _metadata(get('/hubs/continueWatching', None, token)) or []:
        if m.get('type') == 'movie' and int(m.get('viewOffset') or 0) > 0:
            out.append(_entry(m, 'auto', user))
    return out


def playlist_items(get, token, user, name=PLAYLIST):
    out = []
    for pl in _metadata(get('/playlists', {'playlistType': 'video'}, token)):
        if (pl.get('title') or '').strip().lower() != name.lower():
            continue
        for it in _metadata(get('/playlists/%s/items' % pl.get('ratingKey'), None, token)):
            t = it.get('type')
            if t in ('episode', 'movie'):
                out.append(_entry(it, 'playlist', user))
            elif t == 'show':
                out += [_entry(e, 'playlist', user) for e in _leaves(get, it.get('ratingKey'), token)]
            elif t == 'season':
                out += [_entry(e, 'playlist', user) for e in _leaves(get, it.get('ratingKey'), token, 'children')]
    return out


def build(get, tv_get, owner_token, now, days=AUTO_DAYS, nxt=AUTO_NEXT, playlist=PLAYLIST):
    """-> {'items': [...], 'total_bytes', 'total_gb', 'count', 'users', 'errors'}; sorted by title."""
    merged, errors = {}, 0
    users = list_users(get, tv_get, owner_token)
    for u in users:
        for fn in (lambda: auto_items(get, u['token'], u['label'], now, days, nxt),
                   lambda: playlist_items(get, u['token'], u['label'], playlist)):
            try:
                found = fn()
            except Exception:
                errors += 1
                continue
            for e in found:
                cur = merged.get(e['key'])
                if cur is None:
                    merged[e['key']] = e
                else:
                    cur['reasons'] |= e['reasons']
                    cur['users'] |= e['users']
                    cur['size'] = max(cur['size'], e['size'])
    items = []
    for e in sorted(merged.values(), key=lambda x: (x['title'].lower(), x['key'])):
        items.append({'title': e['title'], 'type': e['type'], 'file': e['file'], 'size_bytes': e['size'],
                      'size_gb': round(e['size'] / GB, 2), 'reasons': sorted(e['reasons']), 'users': sorted(e['users'])})
    total = sum(i['size_bytes'] for i in items)
    return {'items': items, 'count': len(items), 'total_bytes': total, 'total_gb': round(total / GB, 2),
            'users': len(users), 'errors': errors}
