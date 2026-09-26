"""Regression: purge_stalled_upgrade_torrents must not treat a torrent
Deluge itself has queued (waiting for a free download slot) as "stalled".

Bug (2026-09-26): the stalled check only looked at total_done < 5MB. Every
torrent Deluge puts in state='Queued' has total_done == 0 by definition --
it hasn't been given a chance to download anything yet -- so the very first
time the newly-wired hourly scheduler ran, it purged the entire queued
backlog (~260 torrents) instead of just genuinely stuck ones.

Fix: skip anything in state == 'Queued'. Only a torrent Deluge IS actively
running (state == 'Downloading', or anything else) that still shows 0 bytes
is a genuine stall.

Discriminating case: a Queued, 0-byte torrent must survive. Revert the
`if i.get('state') == 'Queued': continue` line and
test_purge_skips_queued_zero_byte_torrent fails (it gets removed).
"""
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

LABEL = 'radarr-upgrade'


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _setup(monkeypatch, torrents):
    monkeypatch.setattr(target, 'deluge_login', lambda: None)
    monkeypatch.setattr(
        target.session, 'post',
        lambda *a, **k: _Resp({'result': torrents}),
    )
    removed = []
    monkeypatch.setattr(
        target, 'remove_torrent',
        lambda h, remove_data=True, info=None: removed.append((h, remove_data)),
    )
    activity = []
    monkeypatch.setattr(
        target, 'record_activity',
        lambda category, message: activity.append((category, message)),
    )
    return removed, activity


def test_purge_skips_queued_zero_byte_torrent(monkeypatch):
    torrents = {
        'h1': {'name': 'queued-upgrade', 'label': LABEL, 'total_done': 0, 'state': 'Queued'},
    }
    removed, activity = _setup(monkeypatch, torrents)
    target.purge_stalled_upgrade_torrents(LABEL)
    assert removed == []
    assert activity == []


def test_purge_removes_genuinely_stalled_downloading_torrent(monkeypatch):
    torrents = {
        'h2': {'name': 'dead-upgrade', 'label': LABEL, 'total_done': 0, 'state': 'Downloading'},
    }
    removed, activity = _setup(monkeypatch, torrents)
    target.purge_stalled_upgrade_torrents(LABEL)
    assert removed == [('h2', False)]
    assert activity == [('cleanup', 'Purged 1 stalled radarr-upgrade torrent(s) (< 5MB downloaded, not queued)')]


def test_purge_skips_torrent_with_progress_regardless_of_state(monkeypatch):
    torrents = {
        'h3': {'name': 'progressing-upgrade', 'label': LABEL, 'total_done': 10 * 1024 * 1024, 'state': 'Downloading'},
    }
    removed, _ = _setup(monkeypatch, torrents)
    target.purge_stalled_upgrade_torrents(LABEL)
    assert removed == []


def test_purge_ignores_other_labels(monkeypatch):
    torrents = {
        'h4': {'name': 'unrelated', 'label': 'sonarr', 'total_done': 0, 'state': 'Queued'},
        'h5': {'name': 'unrelated-dl', 'label': 'sonarr', 'total_done': 0, 'state': 'Downloading'},
    }
    removed, _ = _setup(monkeypatch, torrents)
    target.purge_stalled_upgrade_torrents(LABEL)
    assert removed == []


def test_purge_mixed_batch_only_removes_the_genuine_stall(monkeypatch):
    torrents = {
        'queued1': {'name': 'q1', 'label': LABEL, 'total_done': 0, 'state': 'Queued'},
        'queued2': {'name': 'q2', 'label': LABEL, 'total_done': 0, 'state': 'Queued'},
        'stalled1': {'name': 's1', 'label': LABEL, 'total_done': 0, 'state': 'Downloading'},
        'progressing': {'name': 'p1', 'label': LABEL, 'total_done': 10 * 1024 * 1024, 'state': 'Downloading'},
        'other_label': {'name': 'o1', 'label': 'radarr', 'total_done': 0, 'state': 'Queued'},
    }
    removed, activity = _setup(monkeypatch, torrents)
    target.purge_stalled_upgrade_torrents(LABEL)
    assert removed == [('stalled1', False)]
    assert activity == [('cleanup', 'Purged 1 stalled radarr-upgrade torrent(s) (< 5MB downloaded, not queued)')]
