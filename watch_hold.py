"""Invisible upgrades: a better copy replaces the one someone is watching, and they do not notice.

What a viewer notices when Sonarr/Radarr upgrade a file under them:
  1. The file goes away under a stream that is playing or paused. The *arr recycles the old file before it
     moves the new one in (see glacier_hold.py), so a running stream breaks or a resume fails.
  2. The resume point survives (Plex keeps it on the item, not the file) but lands in the wrong place when
     the new copy has a longer or shorter intro, recap or studio logo, or runs at another speed (PAL 25 fps
     against 23.976).
  3. The audio track and subtitle choice reset: Plex keeps those per file.

What this module does about each (wired in arr-webhook.py; WATCH_HOLD=off|dry|on, default dry):
  1. Nothing is imported while someone streams the item, or did in the last WATCH_HOLD_QUIET_MIN minutes.
     A download on the *arr's own label is moved to a hold label once it is WATCH_HOLD_AT_PCT done (at once
     when someone is streaming the item), exactly as the glacier hold does: the *arr stops tracking it, its
     Grabbed history row stays, and on release the label goes back and RefreshMonitoredDownloads lets the
     *arr import it as the upgrade it is. A download in the upgrade lane is never on the *arr's label; the
     lane's importer (auto-rescue) asks ready() before it fires the import scan.
  2. Before the import, while both files exist, each user's resume point is mapped onto the new copy by
     matching the sound around it: ffmpeg decodes a short mono window of each file, the loudness envelope
     of the old window is slid along the new copy (normalized cross-correlation, at the speed ratios that
     make sense), and a second window must agree. After the import the mapped point is written for that
     user, only when the user's point has not moved since. Without a confident match nothing is shifted.
  3. Each user's audio and subtitle choice is matched to the new copy's streams (language, commentary or
     not, forced, SDH, then codec and channels) and set for that user, only where Plex did not already
     pick the same stream.

The import also waits for the mapping, but never longer than WATCH_HOLD_MAX_HOURS after the download
finished, and never for resume points older than WATCH_HOLD_STALE_DAYS. After WATCH_HOLD_MAX_HOURS only a
stream that is playing right now holds it.

dry: nothing is relabelled or held, no import waits, nothing is written to Plex. The reads (sessions,
users' resume points and choices, the audio mapping) still run, and the log says what would have happened.

SABnzbd (Usenet) downloads are held the same way through their category (sab_hold.py), as soon as they are
tracked and still queued: SABnzbd cannot recategorise a download that is already in its history, so one that
finished before the hold was wanted imports as usual.

Not covered: a download made by a remote (seedbox) client is not in this Deluge and cannot be held; its
choices are still carried over, its resume points are not mapped. A season pack is held while someone
streams one of its episodes and its choices are carried over, but its resume points are not mapped.
While held, a download is out of the *arr's queue, so the *arr could grab a second copy of the same item
(the blind spot every lane upgrade and glacier hold has).

Logs carry counts and infohashes only: no titles, user names or paths.
"""
import posixpath

try:
    import numpy as np
except ImportError:  # pragma: no cover - the image has numpy; without it nothing is mapped
    np = None

HOLD_LABEL = 'watch-hold'
MODES = ('off', 'dry', 'on')
DEFAULT_MODE = 'dry'
QUIET_MIN = 10
STALE_DAYS = 30
MAX_HOURS = 48
AT_PCT = 50.0
SNAP_MAX_AGE_S = 1800          # a snapshot older than this is retaken before the import
RESUME_MAX_FRAC = 0.95         # past this the item counts as watched, there is nothing to map
MIN_RESUME_MS = 30_000
SHIFT_MIN_MS = 1000            # smaller differences are not written
MOVED_MS = 5000                # a resume point that moved more than this since the snapshot was watched since
WATCHED_STATES = ('downloading', 'importpending')
VIDEO_EXTS = ('.mkv', '.mp4', '.m4v', '.avi', '.ts', '.mov', '.wmv')

# the audio mapping
RATE = 8000                    # Hz, mono, s16le
HOP_S = 0.02                   # one envelope value per 20 ms
WIN_S = 40.0                   # old-copy window around the resume point
CONFIRM_S = 30.0               # the second, independent window
SEARCH_S = 120.0               # new-copy search either side of where the window would be at the same time
WIDE_S = 300.0                 # tried when the narrow search finds nothing convincing
MIN_PEAK = 0.5                 # normalized correlation of the best match
MIN_MARGIN = 0.1               # ...and its lead over the best match more than 0.5 s away
SOLO_PEAK = 0.7                # needed when no second window fits (near the start or end)
AGREE_S = 0.5                  # the two windows must agree on the offset within this
PAL = 25 / (24000 / 1001)      # 25 fps against 23.976: the PAL speed-up


def parse_mode(v):
    v = str(v or '').strip().lower()
    return v if v in MODES else DEFAULT_MODE


class Cfg:
    def __init__(self, mode=DEFAULT_MODE, quiet_min=QUIET_MIN, stale_days=STALE_DAYS, max_hours=MAX_HOURS,
                 at_pct=AT_PCT):
        self.mode = parse_mode(mode)
        self.quiet_s = float(quiet_min) * 60
        self.stale_s = float(stale_days) * 86400
        self.max_s = float(max_hours) * 3600
        self.at_pct = float(at_pct)

    @property
    def acts(self):
        return self.mode == 'on'


class Counts(dict):
    def bump(self, key, n=1):
        self[key] = self.get(key, 0) + n


# ── paths and sessions ────────────────────────────────────────────────────────

def base(p):
    return posixpath.basename(str(p or '').replace('\\', '/')).lower()


def same_file(a, b):
    """Same path, or (across mount prefixes) same file name."""
    return bool(a and b) and (a == b or base(a) == base(b))


def _ident(p):
    return p


def parse_sessions(doc, to_local=None):
    """/status/sessions -> [{'key': ratingKey, 'files': [local paths], 'state': playing|paused|buffering}].
    The user is left out on purpose: nothing here needs to know who."""
    to_local = to_local or _ident
    out = []
    for m in ((doc or {}).get('MediaContainer') or {}).get('Metadata') or []:
        files = [to_local(p['file']) for med in m.get('Media') or [] for p in med.get('Part') or [] if p.get('file')]
        out.append({'key': str(m.get('ratingKey') or ''), 'files': files,
                    'state': str((m.get('Player') or {}).get('state') or '').lower()})
    return out


def session_keys(s):
    keys = {'rk:' + s['key']} if s.get('key') else set()
    return keys | {'f:' + base(f) for f in s.get('files') or [] if f}


def item_keys(item):
    keys = set()
    if item.get('plex'):
        keys.add('rk:' + str(item['plex']))
    if item.get('old'):
        keys.add('f:' + base(item['old']))
    return keys


def entry_keys(entry):
    return {k for it in (entry.get('items') or {}).values() for k in item_keys(it)}


def busy(paths, sessions, seen, now, quiet_s):
    """True while a viewer is on any of `paths` (by file name, like the holds): in a live session, or seen in one
    within `quiet_s` (a pause or a quick stop may resume). `sessions` None means Plex was unreadable: busy."""
    keys = {'f:' + base(p) for p in paths or () if p}
    if not keys:
        return False
    if sessions is None:
        return True
    if any(keys & session_keys(s) for s in sessions):
        return True
    return any(now - (seen or {}).get(k, 0) < quiet_s for k in keys)


def mark_seen(seen, sessions, now):
    for s in sessions or []:
        for k in session_keys(s):
            seen[k] = now


def prune_seen(seen, now, keep_s):
    for k in [k for k, t in seen.items() if now - t > keep_s]:
        seen.pop(k)


def last_seen(entry, seen):
    return max((seen.get(k, 0) for k in entry_keys(entry)), default=0)


def playing(entry, sessions):
    keys = entry_keys(entry)
    return any(s.get('state') == 'playing' and session_keys(s) & keys for s in sessions or [])


def streamed(entry, sessions):
    keys = entry_keys(entry)
    return any(session_keys(s) & keys for s in sessions or [])


# ── one user's view of an item ────────────────────────────────────────────────

def find_part(metas, target, to_local=None):
    """(metadata, media, part) among Plex metadata dicts whose part is the local file `target` (same path, or
    the same file name under another mount prefix). None when none is."""
    to_local = to_local or _ident
    fallback = None
    for m in metas or []:
        for med in m.get('Media') or []:
            for p in med.get('Part') or []:
                f = to_local(p.get('file') or '')
                if f and f == target:
                    return m, med, p
                if fallback is None and same_file(f, target):
                    fallback = (m, med, p)
    return fallback


def streams(part, kind):
    """Plex streams of one kind in a part: 2 audio, 3 subtitles."""
    return [s for s in (part or {}).get('Stream') or [] if s.get('streamType') == kind]


def pick_of(s):
    return {'lang': str(s.get('languageCode') or s.get('languageTag') or '').lower(),
            'codec': str(s.get('codec') or '').lower(), 'channels': int(s.get('channels') or 0),
            'title': str(s.get('title') or '').lower(), 'forced': bool(s.get('forced')),
            'hi': bool(s.get('hearingImpaired')), 'index': s.get('index'), 'default': bool(s.get('default'))}


def user_view(meta, part):
    """What one user has on this file: resume point, when last watched, and the audio and subtitle choice
    worth carrying over. Audio only when the user picked a track that is not the file's default (the
    default of the new copy is the better choice otherwise); subtitles whenever the file has any, as the
    stream picked or 'off'."""
    aud, sub = streams(part, 2), streams(part, 3)
    sel_a = next((s for s in aud if s.get('selected')), None)
    sel_s = next((s for s in sub if s.get('selected')), None)
    audio = pick_of(sel_a) if sel_a is not None and len(aud) > 1 and not sel_a.get('default') else None
    subs = (pick_of(sel_s) if sel_s is not None else 'off') if sub else None
    return {'offset_ms': int(meta.get('viewOffset') or 0), 'dur_ms': int(meta.get('duration') or 0),
            'viewed_at': int(meta.get('lastViewedAt') or 0), 'audio': audio, 'subs': subs}


def needs_map(view, now, stale_s):
    """A resume point worth mapping: past the first half minute, before the end, and recent enough."""
    off, dur = view.get('offset_ms') or 0, view.get('dur_ms') or 0
    if off < MIN_RESUME_MS or (dur and off > RESUME_MAX_FRAC * dur):
        return False
    at = view.get('viewed_at') or 0
    return not at or now - at <= stale_s


def worth_keeping(view):
    return bool(view.get('offset_ms') or view.get('audio') or view.get('subs'))


def align_stream(part):
    """(stream index, language) of the old copy's audio used for the mapping: the default track, else the first."""
    aud = streams(part, 2)
    s = next((x for x in aud if x.get('default')), aud[0] if aud else None)
    if s is None:
        return None
    return {'index': s.get('index'), 'lang': str(s.get('languageCode') or '').lower()}


def match_stream(pick, cands):
    """The stream among `cands` closest to `pick`: same language (unless the pick has none), commentary only
    for a commentary, then forced, SDH, codec, channels, title. None when nothing fits."""
    want_comm = 'commentary' in (pick.get('title') or '')
    best, best_score = None, -1
    for s in cands:
        p = pick_of(s)
        if pick.get('lang') and pick['lang'] not in ('und', '') and p['lang'] != pick['lang']:
            continue
        if ('commentary' in p['title']) != want_comm:
            continue
        score = (8 if p['forced'] == pick.get('forced', False) else 0) + (4 if p['hi'] == pick.get('hi', False) else 0)
        score += (2 if p['codec'] == pick.get('codec') else 0) + (2 if p['channels'] == pick.get('channels') else 0)
        score += 1 if p['title'] and p['title'] == pick.get('title') else 0
        if score > best_score:
            best, best_score = s, score
    return best


def plan_picks(view, part):
    """{'audio': stream id, 'subs': stream id or 0 (off)} to set on the new part for this user; only what
    differs from what Plex already has selected for them."""
    out = {}
    aud, sub = streams(part, 2), streams(part, 3)
    want = view.get('audio')
    if want and len(aud) > 1:
        m = match_stream(want, aud)
        cur = next((s for s in aud if s.get('selected')), None)
        if m is not None and (cur is None or cur.get('id') != m.get('id')):
            out['audio'] = m.get('id')
    sw = view.get('subs')
    cur = next((s for s in sub if s.get('selected')), None)
    if sw == 'off':
        if cur is not None and not cur.get('forced'):   # forced subtitles translate foreign dialogue: keep them
            out['subs'] = 0
    elif sw:
        m = match_stream(sw, sub)
        if m is not None and (cur is None or cur.get('id') != m.get('id')):
            out['subs'] = m.get('id')
    return out


def probe_audio(doc, lang):
    """(stream index, duration s) of the new copy's audio from ffprobe JSON: the language of the old track,
    the default among those, else the default track, else the first."""
    aud = [s for s in (doc or {}).get('streams') or [] if s.get('codec_type') == 'audio']
    if not aud:
        return None, None

    def is_default(s):
        return bool((s.get('disposition') or {}).get('default'))

    same = [s for s in aud if lang and str((s.get('tags') or {}).get('language') or '').lower() == lang]
    pool = same or aud
    s = next((x for x in pool if is_default(x)), pool[0])
    try:
        dur = float(((doc or {}).get('format') or {}).get('duration') or 0) or None
    except (TypeError, ValueError):
        dur = None
    return s.get('index'), dur


# ── the audio mapping ─────────────────────────────────────────────────────────

def envelope(pcm, rate=RATE, hop_s=HOP_S):
    """Log loudness per hop of mono s16le PCM."""
    x = np.frombuffer(pcm or b'', dtype='<i2').astype(np.float64) / 32768.0
    n = max(int(rate * hop_s), 1)
    m = len(x) // n
    if m < 2:
        return np.zeros(0)
    fr = x[:m * n].reshape(m, n)
    return np.log1p(1000.0 * np.sqrt((fr * fr).mean(axis=1)))


def resample(env, r):
    """`env` (old-copy hops) on new-copy hops, for new_t = r * old_t."""
    if r == 1 or len(env) < 2:
        return env
    m = int(len(env) * r)
    return np.interp(np.arange(m) / r, np.arange(len(env)), env)


def ncc(a, b):
    """Normalized cross-correlation of template `a` at every whole position in `b`."""
    n = len(a)
    if n < 10 or len(b) < n:
        return np.zeros(0)
    a0 = a - a.mean()
    sa = np.sqrt((a0 * a0).sum())
    if sa == 0:
        return np.zeros(len(b) - n + 1)
    num = np.correlate(b, a0, 'valid')
    c1 = np.concatenate(([0.0], np.cumsum(b)))
    c2 = np.concatenate(([0.0], np.cumsum(b * b)))
    s, s2 = c1[n:] - c1[:-n], c2[n:] - c2[:-n]
    den = sa * np.sqrt(np.maximum(s2 - s * s / n, 0.0))
    return np.where(den > 1e-9, num / np.where(den > 1e-9, den, 1.0), 0.0)


def best_lag(c, excl):
    """(index, peak, margin) of the best match; margin = lead over the best match more than `excl` away."""
    if len(c) == 0:
        return None, 0.0, 0.0
    k = int(np.argmax(c))
    peak = float(c[k])
    rest = np.concatenate((c[:max(0, k - excl)], c[k + excl + 1:]))
    return k, peak, peak - (float(rest.max()) if len(rest) else 0.0)


def rates(old_dur_s, new_dur_s):
    """Speed ratios worth trying: same speed, the duration ratio when it is a real difference, and the PAL
    speed-up either way when the durations point at it."""
    out = [1.0]
    if old_dur_s and new_dur_s:
        ratio = new_dur_s / old_dur_s
        if 0.002 < abs(ratio - 1) < 0.06:
            out.append(ratio)
        for p in (1 / PAL, PAL):
            if abs(ratio - p) < 0.01:
                out.append(p)
    return out


def _match(tmpl_env, w0, read_new, lo, hi, rs):
    """Best match of the old window (starting at old second w0) in new seconds [lo, hi], over speeds rs.
    Returns (new start of the window, rate, peak, margin) or None."""
    lo = max(0.0, lo)
    if hi - lo < 1:
        return None
    sig = np.diff(envelope(read_new(lo, hi - lo)))
    best = None
    for r in rs:
        tmpl = np.diff(resample(tmpl_env, r))
        k, peak, margin = best_lag(ncc(tmpl, sig), int(AGREE_S / HOP_S))
        if k is None:
            continue
        if best is None or peak > best[2]:
            best = (lo + k * HOP_S, r, peak, margin)
    return best


def map_offset(t_s, read_old, read_new, old_dur_s, new_dur_s):
    """Where second t_s of the old copy is in the new copy, from the sound around it.
    read_old(start_s, dur_s) / read_new(start_s, dur_s) return mono s16le PCM at RATE.
    Returns {'ok', 'new_s', 'peak', 'rate', 'why'}."""
    out = {'ok': False, 'new_s': None, 'peak': 0.0, 'rate': 1.0, 'why': ''}
    if np is None:
        out['why'] = 'no_numpy'
        return out
    w0 = max(0.0, t_s - WIN_S / 2)
    w = min(WIN_S, (old_dur_s or w0 + WIN_S) - w0)
    if w < 10:
        out['why'] = 'edge'
        return out
    try:
        a = envelope(read_old(w0, w))
        if len(a) < 100:
            out['why'] = 'short_read'
            return out
        rs = rates(old_dur_s, new_dur_s)
        hit = None
        for span in (SEARCH_S, WIDE_S):
            lo = min(r * w0 for r in rs) - span
            hi = max(r * (w0 + w) for r in rs) + span
            if new_dur_s:
                hi = min(hi, new_dur_s)
            hit = _match(a, w0, read_new, lo, hi, rs)
            if hit and hit[2] >= MIN_PEAK and hit[3] >= MIN_MARGIN:
                break
        if not hit or hit[2] < MIN_PEAK or hit[3] < MIN_MARGIN:
            out.update(peak=round(hit[2], 3) if hit else 0.0, why='no_match')
            return out
        start_new, r, peak, _margin = hit
        out.update(peak=round(peak, 3), rate=r)
        shift = start_new - r * w0                       # new_t = r * old_t + shift
        t1 = t_s - 150 if t_s - 150 >= 0 else (t_s + 90 if not old_dur_s or t_s + 90 + CONFIRM_S <= old_dur_s else None)
        if t1 is None:
            if peak < SOLO_PEAK:
                out['why'] = 'unconfirmed'
                return out
        else:
            b = envelope(read_old(t1, CONFIRM_S))
            exp = r * t1 + shift
            hit2 = _match(b, t1, read_new, exp - 10, exp + r * CONFIRM_S + 10, [r])
            if not hit2 or hit2[2] < MIN_PEAK or abs((hit2[0] - r * t1) - shift) > AGREE_S:
                out['why'] = 'disagree'
                return out
        new_s = r * t_s + shift
        if new_s < 0 or (new_dur_s and new_s > new_dur_s):
            out['why'] = 'out_of_range'
            return out
        out.update(ok=True, new_s=round(new_s, 3), why='ok')
        return out
    except Exception:  # noqa: BLE001 - a read that fails maps nothing
        out['why'] = 'read_failed'
        return out


# ── tracking, holding, releasing ──────────────────────────────────────────────

def needs_prep(entry, now, stale_s):
    """True while some user's resume point still has to be mapped (never for a season pack or a remote download)."""
    if entry.get('multi') or entry.get('remote'):
        return False
    for it in (entry.get('items') or {}).values():
        maps = it.get('maps') or {}
        for u, v in (it.get('snap') or {}).items():
            if needs_map(v, now, stale_s) and (maps.get(u) or {}).get('old_ms') != v.get('offset_ms'):
                return True
    return False


def dirty(entry, seen, now):
    """The snapshot is older than the last stream on the item, or simply old."""
    snaps = [it.get('snap_at') or 0 for it in (entry.get('items') or {}).values()]
    oldest = min(snaps) if snaps else 0
    return last_seen(entry, seen) > oldest or now - oldest > SNAP_MAX_AGE_S


def ready(entry, seen, sessions, now, cfg):
    """(ok, why): may this download be imported now? Never while it plays, is paused, or was streamed in the
    quiet period: a viewer who presses play again must find the file they were on. Until WATCH_HOLD_MAX_HOURS
    after it finished, also not before a fresh snapshot and not before every resume point is mapped."""
    if playing(entry, sessions):
        return False, 'playing'
    if streamed(entry, sessions) or now - last_seen(entry, seen) < cfg.quiet_s:
        return False, 'recent'
    t0 = entry.get('done_at')
    if t0 is None:                                      # never seen finished: a long cap from the grab
        t0 = (entry.get('since') or now) + 7 * 86400
    if now - t0 >= cfg.max_s:
        return True, 'max_hours'
    if dirty(entry, seen, now):
        return False, 'snapshot'
    if needs_prep(entry, now, cfg.stale_s):
        return False, 'prep'
    return True, 'ok'


def wants_hold(entry, info, seen, sessions, now, cfg):
    """Hold a download on the *arr's own label: someone streams the item (now or in the quiet period), or a
    resume point has to be mapped and the download is far enough along."""
    if streamed(entry, sessions) or now - last_seen(entry, seen) < cfg.quiet_s:
        return not (info.get('usenet') and (info.get('is_finished') or info.get('in_history')))  # not once in SABnzbd's history
    if info.get('usenet'):
        # SABnzbd can recategorise a queued download but not a finished one (see sab_hold.py): hold at once
        return not (info.get('is_finished') or info.get('in_history')) and needs_prep(entry, now, cfg.stale_s)
    done = info.get('is_finished') or (info.get('progress') or 0) >= cfg.at_pct
    return bool(done) and needs_prep(entry, now, cfg.stale_s)


def snapshot_item(io, item, now):
    """Every user's view of the old file, kept only where there is something to carry over."""
    views, align, dur = {}, None, 0
    if not item.get('plex'):
        item['snap'], item['snap_at'] = {}, now
        return item
    for u in io.users():
        meta = io.user_meta(u['token'], item['plex'])
        found = find_part([meta] if meta else [], item['old'], io.to_local)
        if not found:
            continue
        m, _med, p = found
        v = user_view(m, p)
        if align is None:
            align, dur = align_stream(p), v['dur_ms']
        if worth_keeping(v):
            views[u['label']] = v
    item['snap'], item['snap_at'] = views, now
    if align:
        item['align'], item['dur_ms'] = align, dur
    return item


def track(io, tracked, h, who, ids, now, remote=False):
    """Start tracking an upgrade download: its items' old files, their Plex items and every user's view.
    Returns a status: 'tracked', 'not_upgrade' or 'lookup_failed' (worth a retry)."""
    try:
        olds = io.items(who, ids)                       # {item key: {'old': local path, ...lookup info}}
    except Exception:  # noqa: BLE001
        return 'lookup_failed'
    olds = {k: v for k, v in (olds or {}).items() if v.get('old')}
    if not olds:
        return 'not_upgrade'
    items = {}
    for k, info in olds.items():
        try:
            key = io.plex_key(who, info)
        except Exception:  # noqa: BLE001
            key = None
        item = {'old': info['old'], 'plex': key, 'snap': {}, 'snap_at': 0, 'maps': {}, 'who': who,
                'q': {k: v for k, v in info.items() if k in ('title', 'old')}}
        try:
            snapshot_item(io, item, now)
        except Exception:  # noqa: BLE001
            item['snap_at'] = 0                         # retaken on the next pass
        items[k] = item
    tracked[h] = {'who': who, 'since': now, 'done_at': None, 'items': items, 'multi': len(items) > 1,
                  'remote': bool(remote)}
    return 'tracked'


def prep_entry(io, h, entry, now, cfg, c):
    """Map every resume point that needs it onto the finished new copy. One download per call."""
    if not io.can_read_old(entry):
        c.bump('prep_wait_old')
        return False
    new = io.new_file(h)
    if not new:
        c.bump('prep_no_new_file')
        return False
    for it in (entry.get('items') or {}).values():
        todo = {u: v for u, v in (it.get('snap') or {}).items()
                if needs_map(v, now, cfg.stale_s) and ((it.get('maps') or {}).get(u) or {}).get('old_ms') != v['offset_ms']}
        if not todo:
            continue
        align = it.get('align') or {}
        try:
            idx, new_dur = probe_audio(io.probe(new), align.get('lang'))
        except Exception:  # noqa: BLE001
            idx, new_dur = None, None
        if idx is None:
            for u, v in todo.items():
                it.setdefault('maps', {})[u] = {'old_ms': v['offset_ms'], 'new_ms': None, 'ok': False, 'why': 'no_audio'}
            c.bump('mapped_unsure', len(todo))
            continue

        def read_old(s, d, _p=it['old'], _i=align.get('index')):
            return io.read_pcm(_p, _i, s, d)

        def read_new(s, d, _p=new, _i=idx):
            return io.read_pcm(_p, _i, s, d)

        cache = {}
        for u, v in todo.items():
            t = round(v['offset_ms'] / 1000.0, 1)
            if t not in cache:
                cache[t] = map_offset(t, read_old, read_new, (it.get('dur_ms') or 0) / 1000.0 or None, new_dur)
            r = cache[t]
            it.setdefault('maps', {})[u] = {'old_ms': v['offset_ms'], 'ok': r['ok'], 'why': r['why'], 'peak': r['peak'],
                                            'new_ms': int(round(r['new_s'] * 1000)) if r['ok'] else None}
            c.bump('mapped_ok' if r['ok'] else 'mapped_unsure')
    return True


def merge_maps(doc, h, done):
    """Copy the mappings worked out on `done` (a copy of tracked[h] that prep_entry ran on) back into the
    state, for the points that have not moved since the copy was taken. Returns how many were copied."""
    live = (doc.get('tracked') or {}).get(h)
    n = 0
    if not live:
        return n
    for k, it in (done.get('items') or {}).items():
        lit = (live.get('items') or {}).get(k)
        if not lit:
            continue
        for u, m in (it.get('maps') or {}).items():
            v = (lit.get('snap') or {}).get(u)
            if v and v.get('offset_ms') == m.get('old_ms'):
                lit.setdefault('maps', {})[u] = m
                n += 1
    return n


def discover(io, doc, now, torrents, checked, c, base_labels, lane_labels):
    """Track the upgrade downloads not seen yet: rows in the *arrs' queues (downloads on their own label, and
    remote ones), unfinished-import torrents in the upgrade lane, and glacier-held downloads."""
    tracked, held = doc['tracked'], doc['held']
    cands = {}
    for who in io.services():
        try:
            rows = io.queue(who)
        except Exception:  # noqa: BLE001
            c.bump('queue_unreadable')
            continue
        for r in rows:
            h = str(r.get('downloadId') or '').lower()
            if not h or h in tracked or h in checked:
                continue
            if str(r.get('trackedDownloadState') or '').lower() not in WATCHED_STATES:
                continue
            i = r.get('episodeId') if who == 'Sonarr' else r.get('movieId')
            if i is not None:
                cands.setdefault(h, (who, set()))[1].add(i)
    others = {}
    for h, t in (torrents or {}).items():
        if h in tracked or h in checked or h in cands or h in held:
            continue
        lb = (t.get('label') or '').lower()
        if lb in lane_labels or lb == io.glacier_label:
            who = io.who_of(h, lb)
            if who:
                others[h] = who
    for h, who in others.items():
        try:
            ids, imported = io.grab_ids(who, h)
        except Exception:  # noqa: BLE001
            c.bump('lookup_failed')
            continue
        if imported or not ids:
            checked.add(h)
            continue
        cands[h] = (who, set(ids))
    for h, (who, ids) in cands.items():
        remote = h not in (torrents or {})
        status = track(io, tracked, h, who, sorted(ids), now, remote=remote)
        if status == 'lookup_failed':
            c.bump('lookup_failed')
            continue
        if status == 'not_upgrade':
            checked.add(h)
        c.bump(status)
        io.save(doc)


def tick(io, doc, cfg, now, checked, discover_now=True, base_labels=('sonarr', 'radarr'), lane_labels=(), prep=None):
    """One pass: sessions, discovery, snapshots, holds, the mapping (one download), releases. `doc` is the
    state ({'held', 'tracked', 'seen'}) and is mutated; io.save(doc) is called before every step with an
    effect. `prep(h, entry)`, when given, is called instead of mapping in place (the caller maps a copy
    outside its lock, then merge_maps); true means the pass's one mapping is taken. Returns Counts."""
    c = Counts()
    held, tracked, seen = doc.setdefault('held', {}), doc.setdefault('tracked', {}), doc.setdefault('seen', {})
    if cfg.mode == 'off':
        if held:
            release_pass(io, doc, [], now, cfg, c, force=True)
        return c
    try:
        sessions = io.sessions()
    except Exception:  # noqa: BLE001
        c.bump('sessions_unreadable')
        return c                                        # nothing is held or released blind
    mark_seen(seen, sessions, now)
    prune_seen(seen, now, max(cfg.quiet_s * 3, 3600))
    torrents = io.torrents()
    base_labels = {b.lower() for b in base_labels}
    lane_labels = {b.lower() for b in lane_labels}
    if discover_now:
        discover(io, doc, now, torrents, checked, c, base_labels, lane_labels)
    prepped = False
    for h in list(tracked):
        entry = tracked[h]
        info = torrents.get(h)
        if info is None and not entry.get('remote'):
            if h not in held:
                tracked.pop(h)
                c.bump('gone')
            continue
        if entry.get('remote') and now - entry['since'] > cfg.max_s + 86400:
            tracked.pop(h)
            continue
        if info is not None and not entry.get('done_at') and (info.get('is_finished') or (info.get('progress') or 0) >= 99.9):
            entry['done_at'] = now
        quiet = not streamed(entry, sessions) and now - last_seen(entry, seen) >= cfg.quiet_s
        if quiet and dirty(entry, seen, now):
            for it in entry['items'].values():
                try:
                    snapshot_item(io, it, now)
                except Exception:  # noqa: BLE001
                    c.bump('snapshot_failed')
            c.bump('snapshots')
        lb = (info.get('label') or '').lower() if info is not None else ''
        if info is not None and h not in held and lb != HOLD_LABEL \
                and (lb in base_labels or info.get('usenet')) \
                and wants_hold(entry, info, seen, sessions, now, cfg):
            if not cfg.acts:
                c.bump('would_hold')
            else:
                held[h] = {'label': info.get('label') or entry['who'].lower(), 'who': entry['who'], 'since': now}
                io.save(doc)                            # the way back first
                try:
                    io.set_label(h, HOLD_LABEL)
                    c.bump('held')
                except Exception:  # noqa: BLE001
                    held.pop(h, None)
                    c.bump('hold_failed')
                io.save(doc)
        if not prepped and entry.get('done_at') and not entry.get('remote') and needs_prep(entry, now, cfg.stale_s) \
                and not streamed(entry, sessions):
            if prep is not None:
                prepped = bool(prep(h, entry))
            else:
                prepped = prep_entry(io, h, entry, now, cfg, c)
                io.save(doc)
        if entry.get('since') and now - entry['since'] > cfg.max_s + 7 * 86400 and h not in held:
            tracked.pop(h)                              # the import never came back to us
            c.bump('expired')
    if held:
        release_pass(io, doc, sessions, now, cfg, c, force=not cfg.acts)
    return c


def release_pass(io, doc, sessions, now, cfg, c, force=False, verify_tries=3, verify_poll_s=5.0, sleep=None):
    """Put the original label back on every held download that is ready (all of them with force), then nudge
    the *arr. A download gone from Deluge or relabelled by someone else is dropped from state."""
    held, tracked, seen = doc['held'], doc['tracked'], doc['seen']
    torrents = io.torrents()
    released = []
    for h in list(held):
        e = held[h]
        t = torrents.get(h)
        if t is None or (t.get('label') or '') != HOLD_LABEL:
            held.pop(h)
            io.save(doc)
            c.bump('gone' if t is None else 'relabelled_elsewhere')
            continue
        entry = tracked.get(h)
        if entry and not force:
            ok, why = ready(entry, seen, sessions, now, cfg)
            if not ok:
                c.bump('kept_' + why)
                continue
        try:
            io.set_label(h, e.get('label') or '')
        except Exception:  # noqa: BLE001
            c.bump('release_failed')
            continue
        held.pop(h)
        io.save(doc)
        released.append((h, e.get('who')))
        c.bump('released')
    for who in sorted({w for _, w in released if w}):
        try:
            io.refresh(who)
        except Exception:  # noqa: BLE001
            c.bump('refresh_failed')
    if released:
        pending = dict(released)
        for i in range(max(verify_tries, 1)):
            for who in sorted({w for w in pending.values() if w}):
                try:
                    ids = {str(r.get('downloadId') or '').lower() for r in io.queue(who)}
                except Exception:  # noqa: BLE001
                    continue
                for h in [h for h, w in pending.items() if w == who and h in ids]:
                    pending.pop(h)
                    c.bump('rematched')
            if not pending:
                break
            if sleep and i + 1 < verify_tries:
                sleep(verify_poll_s)
        c['unmatched'] = c.get('unmatched', 0) + len(pending)
    return c


def reconcile(io, doc):
    """Startup: drop holds whose torrent is gone or no longer wears the hold label. Returns Counts."""
    c = Counts()
    held = doc.setdefault('held', {})
    if not held:
        return c
    torrents = io.torrents()
    for h in list(held):
        if (torrents.get(h) or {}).get('label') == HOLD_LABEL:
            c.bump('kept')
        else:
            held.pop(h)
            c.bump('dropped')
    io.save(doc)
    return c


def after_import(io, entry, ikeys, new_path, cfg):
    """The *arr imported `new_path` for items `ikeys` of a tracked download (`entry`, a copy: this waits on
    Plex and must not hold any lock): write each user's mapped resume point and carry their audio and
    subtitle choice over to the new file. Returns Counts; the caller drops the items from state."""
    c = Counts()
    items = [(entry.get('items') or {}).get(ik) for ik in ikeys]
    items = [it for it in items if it and it.get('snap')]
    if not items:
        c.bump('nothing_to_carry')
        return c
    users = {u['label']: u['token'] for u in io.users()}
    for item in items:
        key = io.find_new(item, new_path)               # waits for Plex to list the new file
        if not key:
            c.bump('plex_missing')
            continue
        for label, v in item['snap'].items():
            token = users.get(label)
            if not token:
                c.bump('user_gone')
                continue
            meta = io.user_meta(token, key)
            found = find_part([meta] if meta else [], new_path, io.to_local)
            if not found:
                c.bump('part_missing')
                continue
            m, _med, p = found
            cur = int(m.get('viewOffset') or 0)
            same_item = str(key) == str(item.get('plex'))
            untouched = abs(cur - v['offset_ms']) <= MOVED_MS or (not same_item and cur == 0)
            mp = (item.get('maps') or {}).get(label) or {}
            target = None
            if v['offset_ms'] and untouched:
                if mp.get('ok') and mp.get('old_ms') == v['offset_ms']:
                    if abs(mp['new_ms'] - cur) >= SHIFT_MIN_MS:
                        target = mp['new_ms']
                        c.bump('shift')
                elif not same_item:
                    target = v['offset_ms']             # the new item lost the point: put the old one back
                    c.bump('restore_unmapped')
                else:
                    c.bump('kept_unmapped')
            elif v['offset_ms']:
                c.bump('watched_since')
            dur = int(m.get('duration') or 0)
            if target is not None and dur and target > RESUME_MAX_FRAC * dur:
                target = None
                c.bump('shift_past_end')
            picks = plan_picks(v, p)
            if picks:
                c.bump('picks')
            if not cfg.acts:
                if target is not None:
                    c.bump('would_write_offset')
                if picks:
                    c.bump('would_write_picks')
                continue
            try:
                if target is not None:
                    io.set_progress(token, key, target)
                if picks:
                    io.set_streams(token, p.get('id'), picks.get('audio'), picks.get('subs'))
            except Exception:  # noqa: BLE001
                c.bump('write_failed')
    return c


def item_keys_of(who, data):
    """Item keys of an *arr webhook payload: e<episode id> for Sonarr, m<movie id> for Radarr."""
    if who == 'Sonarr':
        return ['e%s' % e.get('id') for e in (data.get('episodes') or []) if e.get('id') is not None]
    mid = (data.get('movie') or {}).get('id')
    return ['m%s' % mid] if mid is not None else []


def drop_items(doc, h, ikeys):
    """Forget items imported from download `h`; the download itself once none is left."""
    entry = (doc.get('tracked') or {}).get(h)
    if not entry:
        return
    for ik in ikeys:
        (entry.get('items') or {}).pop(ik, None)
    if not entry.get('items'):
        doc['tracked'].pop(h, None)
