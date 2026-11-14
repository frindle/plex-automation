"""Deluge's core.move_storage returns before the files move and save_path updates only when they are done.
An immediate save_path read must not record supersede-move-failed; only a move that never lands does
(Nov 2026: 14 of 14 'failed' moves were seeding in Just4Seeding)."""
import os
import sys
import tempfile
import importlib.util

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
spec = importlib.util.spec_from_file_location("aw_move_verify", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
sys.modules["aw_move_verify"] = aw
spec.loader.exec_module(aw)

H = 'a' * 40
DEST = '/data/Downloads/Just4Seeding'


class Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class SlowDeluge:
    """save_path stays at the old path for `lag` status reads after move_storage."""

    def __init__(self, lag, present=True):
        self.lag, self.present, self.reads, self.dest = lag, present, 0, None

    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        if m == 'core.move_storage':
            self.dest = p[1]
            return Resp({'result': None})
        if m == 'core.get_torrents_status':
            self.reads += 1
            if not self.present:
                return Resp({'result': {}})
            path = self.dest if (self.dest and self.reads > self.lag) else '/data/Downloads/complete'
            return Resp({'result': {H: {'save_path': path}}})
        return Resp({'result': None})


def _setup(mp, deluge):
    rec, threads = [], []
    mp.setattr(aw, 'session', deluge)
    mp.setattr(aw, 'record_activity', lambda cat, msg: rec.append(cat))
    mp.setattr(aw, '_invalidate_snapshot', lambda: None)
    mp.setattr(aw.time, 'sleep', lambda s: None)

    class T:
        def __init__(self, target, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            threads.append(self)
            self.target(*self.args)
    mp.setattr(aw.threading, 'Thread', T)
    return rec, threads


def test_lagging_move_is_not_a_failure(monkeypatch):
    rec, threads = _setup(monkeypatch, SlowDeluge(lag=5))
    assert aw.move_torrent_storage(H, DEST) is True
    assert len(threads) == 1, 'not landed at first read: verify in the background'
    assert rec == []


def test_immediate_landing_spawns_no_thread(monkeypatch):
    rec, threads = _setup(monkeypatch, SlowDeluge(lag=0))
    aw.move_torrent_storage(H, DEST)
    assert threads == [] and rec == []


def test_never_lands_records_one_failure(monkeypatch):
    rec, _ = _setup(monkeypatch, SlowDeluge(lag=10 ** 9))
    monkeypatch.setattr(aw, 'MOVE_VERIFY_TRIES', 4)
    aw.move_torrent_storage(H, DEST)
    assert rec == ['supersede-move-failed']


def test_torrent_gone_is_not_a_failure(monkeypatch):
    rec, threads = _setup(monkeypatch, SlowDeluge(lag=0, present=False))
    aw.move_torrent_storage(H, DEST)
    assert threads == [] and rec == []
    assert aw._verify_move(H, DEST, tries=3, poll_s=0) is None and rec == []
