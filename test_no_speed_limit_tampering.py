"""Regression: background jobs must never change a torrent's per-torrent speed
limit except the superseded-release path (Penn's policy). 2026-10-13: a stray
maintain_unlimited_upgrade_slots() (30s loop) set max_download_speed=-1 on the
top 2 upgrade-lane torrents in the queue, silently un-throttling them.

Queue moves (prioritize_normal_torrents) may only change queue position.
No framework, no live services. Run: python test_no_speed_limit_tampering.py
"""


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
            rpc.append((json['method'], json['params']))
            if json['method'] == 'core.get_torrents_status':
                return _Resp({'result': status})
            return _Resp({'result': True})

    s = S()
    aw.session = s
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: status

    aw.prioritize_normal_torrents()
    fn = getattr(aw, 'maintain_unlimited_upgrade_slots', None)
    if fn is not None:
        fn(s)
    assert not hasattr(aw, 'upgrade_slots_scheduler'), 'no 30s speed-limit rewriting loop'
    tampered = [(m, p) for m, p in rpc if m == 'core.set_torrent_options']
    assert tampered == [], 'background jobs must not touch speed limits: %r' % tampered
    assert any(m == 'core.queue_top' for m, _ in rpc), 'queue moves still happen'
    print('test_no_speed_limit_tampering: all assertions passed')


if __name__ == '__main__':
    main()
