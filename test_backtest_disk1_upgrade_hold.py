"""backtest_disk1_upgrade_hold: upgrade detection, wait times, and that it only reads and prints counts."""
from datetime import datetime, timezone

import backtest_disk1_upgrade_hold as bt

PREFIXES = frozenset({'data/Media/TV Shows/Cold Show'})
COLD = '/data/Media/TV Shows/Cold Show/Season 1/e01.mkv'
WARM = '/data/Media/TV Shows/Warm Show/Season 1/e01.mkv'


def ep(y, mo, d, h, m=0):
    return datetime(y, mo, d, h, m, tzinfo=timezone.utc).timestamp()


def row(item, iso, path, kind='downloadFolderImported'):
    return {'eventType': kind, 'episodeId': item, 'date': iso, 'data': {'importedPath': path}}


def test_upgrades_pairs_each_import_with_the_file_it_replaced():
    rows = [row(1, '2026-01-01T10:00:00Z', COLD), row(1, '2026-01-10T12:00:00Z', WARM),
            row(2, '2026-01-11T12:00:00Z', COLD), row(1, '2026-01-12T00:00:00Z', 'x', kind='grabbed')]
    out = bt.upgrades(rows, 'Sonarr')
    assert out == [(ep(2026, 1, 10, 12), COLD)]                       # item 2's first import is not an upgrade


def test_wait_is_to_window_or_earlier_spin_up():
    day = ep(2026, 1, 10, 12)
    assert bt.wait_seconds(day, [], '22:00-06:00', 'UTC') == 10 * 3600
    assert bt.wait_seconds(day, [day + 3600], '22:00-06:00', 'UTC') == 3600
    assert bt.wait_seconds(day, [day - 5], '22:00-06:00', 'UTC') == 10 * 3600   # past spin rows do not count
    assert bt.wait_seconds(ep(2026, 1, 10, 23), [], '22:00-06:00', 'UTC') == 0
    assert bt.wait_seconds(ep(2026, 1, 10, 7), [], '22:00-06:00', 'UTC') == 15 * 3600


def test_backtest_counts():
    items = {'Sonarr': [(ep(2026, 1, 10, 12), COLD), (ep(2026, 1, 10, 23), COLD), (ep(2026, 1, 10, 13), WARM),
                        (ep(2026, 1, 10, 7), COLD)]}
    r = bt.backtest(items, PREFIXES, [], '22:00-06:00', 'UTC')
    assert (r['upgrades'], r['held'], r['not_held'], r['immediate']) == (4, 2, 1, 1)
    assert r['wait 6-12h'] == 1 and r['wait 12-24h'] == 1
    assert r['max_wait_h'] == 15


class Resp:
    def __init__(self, p):
        self.p = p

    def raise_for_status(self):
        pass

    def json(self):
        return self.p


def test_main_reads_only_and_prints_counts_only(tmp_path, capsys):
    idx = tmp_path / 'idx'
    idx.write_text('data/Media/TV Shows/Cold Show\n')
    calls = []
    now = ep(2026, 1, 20, 0)

    def get(url, **kw):
        calls.append(url)
        page = kw['params']['page']
        recs = [row(1, '2026-01-01T10:00:00Z', COLD), row(1, '2026-01-15T12:00:00Z', WARM)] if page == 1 else []
        return Resp({'records': list(reversed(recs))})

    rc = bt.main(['--sonarr-url', 'http://sonarr.invalid', '--sonarr-key', 'k', '--index', str(idx), '--tz', 'UTC'],
                 get=get, now=now)
    out = capsys.readouterr().out
    assert rc == 0 and 'upgrades=1' in out and 'held=1' in out
    assert all(u.endswith('/api/v3/history') for u in calls)
    assert 'Cold' not in out and 'sonarr' not in out.lower()
    assert all(line.split('=')[1].isdigit() for line in out.strip().splitlines())


def test_main_needs_an_index(tmp_path):
    assert bt.main(['--index', str(tmp_path / 'none')], now=1.0) == 1
