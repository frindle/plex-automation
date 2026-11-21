"""Replay the last weeks of Sonarr/Radarr upgrade imports against a sample GLACIER_INDEX and count how many
the disk1 upgrade hold would have held, and for how long.

usage: python backtest_disk1_upgrade_hold.py --sonarr-url URL --sonarr-key KEY --radarr-url URL --radarr-key KEY
           --index sample-index.txt [--spin-dir DIR] [--window 22:00-06:00] [--days 21] [--tz Europe/London]

Read-only: only GET requests to the arr APIs (history, and the replaced file's path from the history
row's data). Nothing is changed. Prints counts only, never titles, paths or ids.

How a past import is judged: a history row of type downloadFolderImported is an import. It is an upgrade
when the same episode or movie has an earlier import row, and the replaced file is that earlier row's
importedPath. It counts as on disk1 when it is at or under a prefix of the index. The wait runs from
the import time to the first moment disk1 was up: the next spin-*.tsv 'up' row (if --spin-dir is given)
or the start of the nightly window, whichever comes first. An import inside the window waits zero.
Spin rows only help when the dir covers the replayed weeks.
"""
import argparse
import bisect
import datetime
import sys

import glacier_hold as gh
import requests

IMPORT_TYPES = {'Sonarr': 'downloadFolderImported', 'Radarr': 'downloadFolderImported'}
BUCKETS = (('<1h', 3600), ('1-6h', 6 * 3600), ('6-12h', 12 * 3600), ('12-24h', 24 * 3600))


def fetch_history(base, key, days, now, page_size=500, get=requests.get):
    """All history rows newer than `days`, newest first, via paging. Read-only."""
    since = now - days * 86400
    rows, page = [], 1
    while True:
        r = get(f'{base}/api/v3/history', headers={'X-Api-Key': key}, timeout=60,
                params={'page': page, 'pageSize': page_size, 'sortKey': 'date', 'sortDirection': 'descending'})
        r.raise_for_status()
        recs = (r.json() or {}).get('records') or []
        if not recs:
            break
        rows.extend(recs)
        oldest = _epoch(recs[-1].get('date'))
        if oldest is not None and oldest < since:
            break
        page += 1
    return [x for x in rows if (_epoch(x.get('date')) or 0) >= since], since


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _item(row):
    return row.get('episodeId') if row.get('episodeId') is not None else row.get('movieId')


def upgrades(rows, kind):
    """[(epoch, replaced_path)] for imports that replaced an earlier import of the same item.
    `rows` may include rows older than the window (to find the earlier file); the caller filters by time."""
    last_path = {}
    out = []
    imports = sorted((r for r in rows if r.get('eventType') == IMPORT_TYPES[kind]), key=lambda r: r.get('date') or '')
    for r in imports:
        item = _item(r)
        path = (r.get('data') or {}).get('importedPath') or ''
        ts = _epoch(r.get('date'))
        if item is None or ts is None:
            continue
        if item in last_path and last_path[item]:
            out.append((ts, last_path[item]))
        if path:
            last_path[item] = path
    return out


def spin_up_times(spin_dir):
    """Sorted epochs of disk1 'up' rows from spin-*.tsv, or []."""
    import glob
    import os
    ups = []
    for f in glob.glob(os.path.join(spin_dir or '', 'spin-*.tsv')):
        try:
            with open(f, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    p = line.rstrip('\n').split('\t')
                    if len(p) >= 3 and p[1].strip() == gh.SPIN_DISK and p[2].strip().lower() == 'up':
                        try:
                            ups.append(float(p[0]))
                        except ValueError:
                            pass
        except OSError:
            pass
    return sorted(ups)


def next_window_start(ts, window, tz=None):
    """Epoch of the first moment at or after ts when the nightly window is open."""
    start, _ = gh.parse_window(window) or gh.parse_window(gh.DEFAULT_WINDOW)
    zone = None
    if tz:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    d = datetime.datetime.fromtimestamp(ts, zone)
    if gh.in_window(d, window):
        return ts
    t = d.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if t < d:
        t += datetime.timedelta(days=1)
    return t.timestamp()


def wait_seconds(ts, ups, window, tz=None, spin_covers=None):
    """Seconds from ts until disk1 is up: the earlier of the next spin-up row and the window opening."""
    cand = next_window_start(ts, window, tz)
    if ups:
        i = bisect.bisect_left(ups, ts)
        if i < len(ups):
            cand = min(cand, ups[i])
    return max(cand - ts, 0)


def backtest(imports_by_kind, prefixes, ups, window, tz=None):
    """Counts: upgrades, held, not_held, plus wait buckets and total/max hours (ints)."""
    out = {'upgrades': 0, 'held': 0, 'not_held': 0, 'immediate': 0}
    for label, _ in BUCKETS:
        out['wait ' + label] = 0
    out['wait >24h'] = 0
    waits = []
    for kind, items in imports_by_kind.items():
        for ts, old_path in items:
            out['upgrades'] += 1
            if not gh.on_disk1(old_path, prefixes):
                out['not_held'] += 1
                continue
            w = wait_seconds(ts, ups, window, tz)
            if w <= 0:
                out['immediate'] += 1
                continue
            out['held'] += 1
            waits.append(w)
            for label, limit in BUCKETS:
                if w < limit:
                    out['wait ' + label] += 1
                    break
            else:
                out['wait >24h'] += 1
    out['max_wait_h'] = int(max(waits) // 3600) if waits else 0
    out['mean_wait_h'] = int(sum(waits) / len(waits) // 3600) if waits else 0
    return out


def main(argv=None, get=requests.get, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sonarr-url', default='')
    ap.add_argument('--sonarr-key', default='')
    ap.add_argument('--radarr-url', default='')
    ap.add_argument('--radarr-key', default='')
    ap.add_argument('--index', required=True, help='sample GLACIER_INDEX (same format as production)')
    ap.add_argument('--spin-dir', default='')
    ap.add_argument('--window', default=gh.DEFAULT_WINDOW)
    ap.add_argument('--tz', default='')
    ap.add_argument('--days', type=int, default=21)
    args = ap.parse_args(argv)
    import time
    now = time.time() if now is None else now
    prefixes = gh.load_index(args.index, now, max_age_days=10 ** 6)
    if not prefixes:
        print('index empty or unreadable; nothing to replay')
        return 1
    ups = spin_up_times(args.spin_dir)
    by_kind = {}
    for kind, url, key in (('Sonarr', args.sonarr_url, args.sonarr_key), ('Radarr', args.radarr_url, args.radarr_key)):
        if not (url and key):
            continue
        # look back further than the window so the file an import replaced is known
        rows, _ = fetch_history(url, key, args.days + 60, now, get=get)
        since = now - args.days * 86400
        by_kind[kind] = [(ts, p) for ts, p in upgrades(rows, kind) if ts >= since]
    res = backtest(by_kind, prefixes, ups, args.window, args.tz or None)
    res['spin_rows_used'] = len(ups)
    for k, v in res.items():
        print('%s=%d' % (k, v))
    return 0


if __name__ == '__main__':
    sys.exit(main())
