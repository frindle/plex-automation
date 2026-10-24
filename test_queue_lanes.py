"""Policy P1 (strict lanes): in the Deluge queue every unfinished normal
sonarr/radarr torrent sits above every recent upgrade, and every recent
upgrade above every old upgrade. Upgrades never get queue_top over normals.
No framework, no live services. Run: python test_queue_lanes.py"""


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeDeluge:
    """List-based queue applying queue_top / queue_bottom."""

    def __init__(self, status):
        self.status = status
        self.queue = list(status)
        self.rpc = []

    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json['params']
        self.rpc.append((m, p))
        if m == 'core.queue_top':
            hs = list(p[0])
            self.queue = hs + [h for h in self.queue if h not in hs]
        elif m == 'core.queue_bottom':
            hs = list(p[0])
            self.queue = [h for h in self.queue if h not in hs] + hs
        elif m == 'core.get_torrent_status':
            return _Resp({'result': self.status.get(p[0]) or {}})
        elif m == 'core.get_torrents_status':
            return _Resp({'result': self.status})
        return _Resp({'result': True})

    def tops(self):
        return [p[0] for m, p in self.rpc if m == 'core.queue_top']


def _install(aw, status):
    d = FakeDeluge(status)
    aw.session = d
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: status
    return d


def _lane(aw, label):
    if label in ('sonarr', 'radarr'):
        return 0
    if label in (aw.SONARR_UPG_PRIORITY_LABEL, aw.RADARR_UPG_PRIORITY_LABEL):
        return 1
    if label in (aw.SONARR_UPG_LABEL, aw.RADARR_UPG_LABEL):
        return 2
    return None


def _assert_lanes(aw, d, status):
    ranks = [(_lane(aw, status[h]['label']), h) for h in d.queue if _lane(aw, status[h]['label']) is not None
             and status[h]['progress'] < 100]
    lanes = [r for r, _ in ranks]
    assert lanes == sorted(lanes), 'lanes out of order: %r' % ranks


def _mixed(aw):
    labels = [aw.RADARR_UPG_LABEL, aw.SONARR_UPG_PRIORITY_LABEL, 'radarr', aw.SONARR_UPG_LABEL,
              aw.RADARR_UPG_PRIORITY_LABEL, 'sonarr', aw.RADARR_UPG_LABEL, 'adult',
              aw.SONARR_UPG_PRIORITY_LABEL, 'radarr']
    st = {}
    for i, lb in enumerate(labels):
        st['h%d' % i] = {'state': 'Downloading', 'label': lb, 'progress': 10.0, 'queue': i, 'name': 'n%d' % i}
    st['done'] = {'state': 'Seeding', 'label': 'radarr', 'progress': 100.0, 'queue': 99, 'name': 'fin'}
    return st


def test_prioritize_normal_torrents(aw):
    st = _mixed(aw)
    d = _install(aw, st)
    aw.prioritize_normal_torrents()
    _assert_lanes(aw, d, st)
    assert 'done' not in [h for t in d.tops() for h in t], 'finished torrents are not reordered'
    assert not [m for m, _ in d.rpc if m == 'core.set_torrent_options']


def test_relabel_never_tops_upgrade(aw):
    # Radarr
    aw.RADARR_URL, aw.RADARR_API_KEY = 'http://radarr', 'k'
    st = {
        'a' * 40: {'state': 'Downloading', 'label': 'radarr', 'progress': 5.0, 'name': 'Recent 2026'},
        'b' * 40: {'state': 'Downloading', 'label': 'radarr', 'progress': 5.0, 'name': 'Older 1999'},
        'c' * 40: {'state': 'Downloading', 'label': 'radarr', 'progress': 5.0, 'name': 'New grab'},
    }
    d = _install(aw, st)
    labels = {}
    aw.set_torrent_label = lambda h, lb: (labels.__setitem__(h, lb), st[h].__setitem__('label', lb))
    aw.ensure_label_exists_named = lambda lb: None
    movies = [{'id': 1, 'hasFile': True, 'year': 2026}, {'id': 2, 'hasFile': True, 'year': 1999},
              {'id': 3, 'hasFile': False, 'year': 2026}]
    queue = {'records': [{'downloadId': ('a' * 40).upper(), 'movieId': 1},
                         {'downloadId': 'b' * 40, 'movieId': 2},
                         {'downloadId': 'c' * 40, 'movieId': 3}]}
    real = aw.requests
    aw.requests = type('R', (), {'get': staticmethod(
        lambda url, **kw: _Resp(queue if url.endswith('/queue') else movies))})()
    aw._is_recent_year = lambda y: y == 2026
    try:
        assert aw.relabel_radarr_upgrades() == 2
    finally:
        aw.requests = real
    assert labels['a' * 40] == aw.RADARR_UPG_PRIORITY_LABEL
    for t in d.tops():
        assert 'a' * 40 not in t and 'b' * 40 not in t, 'upgrade hash got queue_top: %r' % t
    assert d.queue.index('c' * 40) < d.queue.index('a' * 40) < d.queue.index('b' * 40), d.queue


def test_promote_keeper(aw):
    st = {
        'n' * 40: {'label': 'radarr', 'progress': 3.0, 'name': 'normal'},
        'u1' + '0' * 38: {'label': aw.RADARR_UPG_LABEL, 'progress': 3.0, 'name': 'u1'},
        'k' * 40: {'label': aw.RADARR_UPG_LABEL, 'progress': 3.0, 'name': 'keeper'},
    }
    d = _install(aw, st)
    d.queue = ['u1' + '0' * 38, 'k' * 40, 'n' * 40]
    aw._promote_keeper('k' * 40, st)
    assert d.queue[0] == 'n' * 40, 'normal stays first: %r' % d.queue
    assert d.queue.index('k' * 40) < d.queue.index('u1' + '0' * 38), 'keeper tops the upgrade lane'
    assert 'k' * 40 not in [h for t in d.tops() for h in t], 'keeper never gets queue_top'


def test_new_radarr_grab_topped(aw):
    st = {'old': {'label': 'radarr', 'progress': 5.0}}
    d = _install(aw, st)
    aw.time.sleep = lambda s: None
    aw.GRAB_PRIORITIZE_TRIES = 3

    # Deluge registers the hash only on the 2nd poll
    calls = {'n': 0}
    real_post = d.post

    def post(url, json=None, **kw):
        if json['method'] == 'core.get_torrent_status':
            calls['n'] += 1
            if calls['n'] == 2:
                st['newhash'] = {'label': 'radarr', 'progress': 0.0}
            return _Resp({'result': st.get(json['params'][0]) or {}})
        return real_post(url, json=json, **kw)
    aw.session.post = post
    d.queue.append('newhash')
    aw.prioritize_new_grab('Radarr', 'newhash')
    assert ['newhash'] in d.tops(), d.rpc
    assert d.queue[0] == 'newhash'

    # never registered -> safe no-op
    d2 = _install(aw, {})
    aw.prioritize_new_grab('Radarr', 'ghost')
    assert d2.tops() == []

    # handle_grab: new radarr grab -> prioritized; upgrade <10GB untouched
    seen = []
    aw.prioritize_new_grab = lambda src, h: seen.append((src, h))
    aw.is_upgrade_radarr = lambda data: False
    aw.handle_grab({'downloadId': 'ABC'}, 'Radarr')
    assert seen == [('Radarr', 'abc')], seen
    seen.clear()
    aw.is_upgrade_radarr = lambda data: True
    aw.handle_grab({'downloadId': 'ABC', 'release': {'size': 1024 ** 3}}, 'Radarr')
    assert seen == []


def test_helper_never_raises(aw):
    class Boom:
        def post(self, *a, **k):
            raise RuntimeError('deluge down')
    aw.session = Boom()
    aw.reorder_deluge_queue(_mixed(aw))


def main():
    aw = __import__('arr-webhook')
    orig = {k: getattr(aw, k) for k in ('session', 'deluge_login', 'get_all_torrents', 'set_torrent_label',
                                        'ensure_label_exists_named', '_is_recent_year', 'requests',
                                        'prioritize_new_grab', 'is_upgrade_radarr')}
    for fn in (test_prioritize_normal_torrents, test_relabel_never_tops_upgrade, test_promote_keeper,
               test_new_radarr_grab_topped, test_helper_never_raises):
        fn(aw)
        for k, v in orig.items():
            setattr(aw, k, v)
    print('test_queue_lanes: all assertions passed')


if __name__ == '__main__':
    main()
