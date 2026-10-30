"""Regression: background jobs must never change a torrent's per-torrent speed
limit except the superseded-release path (Penn's policy). 2026-10-13: a stray
maintain_unlimited_upgrade_slots() (30s loop) set max_download_speed=-1 on the
top 2 upgrade-lane torrents in the queue, silently un-throttling them.

Queue moves (prioritize_normal_torrents) may only change queue position.
No framework, no live services. Run: python test_no_speed_limit_tampering.py

One narrow allowance (2026-10-30): writes issued from inside speed_cap.py, the
30 KB/s slow-migration cap (applied before a move/recheck, restored once the
torrent is verified Seeding). The same write from anywhere else still fails.
"""
import inspect


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def main():
    aw = __import__('arr-webhook')
    status = {}
    for q in range(6):
        label = ('radarr-upgrade', 'sonarr-upgrade', 'radarr', 'sonarr-upgrade-recent', 'radarr-upgrade', 'adult')[q]
        status['h%d' % q] = {'state': 'Downloading', 'label': label, 'progress': 10.0,
                             'max_download_speed': 30, 'max_upload_speed': -1, 'queue': q}
    rpc = []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            # The ONE narrow allowance: a write issued from inside speed_cap.py
            # (the slow-migration 30 KB/s cap: apply before move/recheck,
            # restore once Seeding). Anything else writing options is tampering.
            via_cap = any(f.filename.endswith('speed_cap.py') for f in inspect.stack())
            rpc.append((json['method'], json['params'], via_cap))
            if json['method'] == 'core.get_torrents_status':
                return _Resp({'result': status})
            if json['method'] == 'core.get_torrent_status':
                return _Resp({'result': {'state': 'Seeding', 'max_download_speed': 30}})
            return _Resp({'result': True})

    def tampered():
        return [(m, p) for m, p, via_cap in rpc if m == 'core.set_torrent_options' and not via_cap]

    s = S()
    aw.session = s
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: status
    aw.send_pushover = lambda *a, **k: None

    aw.prioritize_normal_torrents()
    fn = getattr(aw, 'maintain_unlimited_upgrade_slots', None)
    if fn is not None:
        fn(s)
    # the 6h-tick jobs added alongside speed_cap must not touch limits either
    aw.library_seed_audit()
    aw.episode_retention_run(env={})
    assert not hasattr(aw, 'upgrade_slots_scheduler'), 'no 30s speed-limit rewriting loop'
    assert tampered() == [], 'background jobs must not touch speed limits: %r' % tampered()
    assert not any(m == 'core.set_torrent_options' for m, _, _ in rpc), 'no cap writes at all so far'
    assert any(m == 'core.queue_top' for m, _, _ in rpc), 'queue moves still happen'

    # allowance: speed_cap's own writes, and ONLY the download cap field
    del rpc[:]
    assert aw.speed_cap.capped_operation(aw._deluge_rpc, 'h0', lambda: True, sleep=lambda s: None)
    allowed = [p for m, p, via_cap in rpc if m == 'core.set_torrent_options' and via_cap]
    assert allowed == [[['h0'], {'max_download_speed': 30}], [['h0'], {'max_download_speed': 30}]], allowed
    assert tampered() == [], tampered()
    # ...and the allowance is not a blanket pass: the same write from outside
    # speed_cap.py is still tampering
    aw._deluge_rpc('core.set_torrent_options', [['h1'], {'max_download_speed': -1}])
    assert tampered() == [('core.set_torrent_options', [['h1'], {'max_download_speed': -1}])], tampered()
    print('test_no_speed_limit_tampering: all assertions passed')


if __name__ == '__main__':
    main()
