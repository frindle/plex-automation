"""Earner set (Penn, 11-21: keep the top earners seeding). The labeled torrents
with the most total_uploaded are never retired (retire_old_seeds) and never
unseeded as stalled (cleanup_stalled_seeds)."""
import importlib.util

import deluge_trim as dt

DAY = 86400
spec = importlib.util.spec_from_file_location("aw_earners", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)


def t(up, label='radarr', **kw):
    base = {'label': label, 'state': 'Seeding', 'is_finished': True, 'progress': 100.0,
            'seeding_time': 400 * DAY, 'total_seeds': 9, 'total_uploaded': up,
            'time_since_upload': 30 * DAY, 'ratio': 1.0, 'name': 'n', 'save_path': '/data/Downloads/Complete'}
    base.update(kw)
    return base


def test_top_earners_ranks_by_upload_and_skips_unlabeled_and_unknown():
    ts = {'a': t(500), 'b': t(900), 'c': t(700), 'blank': t(10**12, label=''),
          'none': t(None), 'zero': t(0), 'neg': t(-1)}
    assert dt.top_earners(ts, 2) == {'b', 'c'}
    assert dt.top_earners(ts, 10) == {'a', 'b', 'c'}
    assert dt.top_earners(ts, 0) == frozenset()
    assert dt.top_earners(ts, -3) == frozenset()


def test_top_earners_label_filter_and_tie_break():
    ts = {'z': t(100), 'y': t(100), 'adult': t(5000, label='adult')}
    assert dt.top_earners(ts, 1, labels=('radarr',)) == {'y'}
    assert dt.top_earners(ts, 1) == {'adult'}


def test_retire_picks_skip_kept_hashes():
    labels = ('radarr',)
    ts = {'a': t(5), 'b': t(9)}
    assert dt.seed_retire_plan(ts, labels, 180, 5) == ['a', 'b']
    assert dt.seed_retire_plan(ts, labels, 180, 5, keep={'b'}) == ['a']
    assert dt.seed_retire_picks(ts, labels, 180, 5, 14, 21, keep={'a', 'b'}) == []


def test_retire_old_seeds_keeps_earners(monkeypatch):
    ts = {f'h{i}': t(1000 * (i + 1)) for i in range(5)}
    monkeypatch.setattr(aw, 'SEED_KEEP_TOP_EARNERS', 2)
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda *a, **k: {h: dict(v) for h, v in ts.items()})
    out = aw.retire_old_seeds(dry_run=True)
    picked = {c['hash'] for c in out['candidates']}
    assert out['kept_earners'] == 2
    assert picked == {'h0', 'h1', 'h2'}, 'h3 and h4 uploaded most and keep seeding'


def test_stalled_cleanup_keeps_earners(monkeypatch, tmp_path):
    ts = {'big': t(10**9, seeding_time=60 * DAY), 'small': t(10, seeding_time=60 * DAY)}
    monkeypatch.setattr(aw, 'SEED_KEEP_TOP_EARNERS', 1)
    monkeypatch.setattr(aw, 'SEED_STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda *a, **k: {h: dict(v) for h, v in ts.items()})
    monkeypatch.setattr(aw, 'record_activity', lambda *a: None)
    aw.cleanup_stalled_seeds()  # baseline week
    out = aw.cleanup_stalled_seeds(dry_run=True)
    assert [c['hash'] for c in out] == ['small']
