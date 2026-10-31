"""Frozen contract: handle_grab (source 'Sonarr') labels a torrent that has an empty or
missing Deluge label as 'sonarr' (label ensured first) BEFORE any upgrade/priority logic,
never overriding a non-empty label. Radarr behaviour is unchanged."""
aw = __import__('arr-webhook')

GB = 1024 ** 3


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


class World:
    def __init__(self, mp, status, upgrade=False):
        self.events = []
        self.status = status  # dict returned by core.get_torrent_status (mutated by label sets)
        mp.delenv('LLM_DECISIONS', raising=False)
        mp.setattr(aw, 'is_upgrade_sonarr', lambda data: upgrade)
        mp.setattr(aw, 'is_upgrade_radarr', lambda data: None)
        mp.setattr(aw, 'time', type('T', (), {'sleep': staticmethod(lambda s: None),
                                              'time': staticmethod(lambda: 0.0)})())
        mp.setattr(aw, 'deluge_login', lambda: None)
        mp.setattr(aw, 'session', type('S', (), {'post': staticmethod(self.post)})())
        mp.setattr(aw, 'ensure_label_exists_named', lambda label: self.events.append(('ensure', label)))
        mp.setattr(aw, '_ensure_deluge_label', lambda label: self.events.append(('ensure', label)))
        mp.setattr(aw, 'set_torrent_label', self.set_label)
        mp.setattr(aw, 'dedupe_grabbed_release', lambda source, h: None)
        mp.setattr(aw, 'prioritize_new_grab', lambda source, h: self.events.append(('prioritize', h)))
        mp.setattr(aw, 'remove_torrent', lambda *a, **k: self.events.append(('remove', a)))
        mp.setattr(aw, 'record_activity', lambda *a, **k: None)
        mp.setattr(aw.llm_advisor, 'advise', lambda kind, facts, rules_action, **kw: rules_action)

    def set_label(self, h, label):
        self.events.append(('label', h, label))
        if self.status:
            self.status['label'] = label

    def post(self, url, json=None, timeout=None, **kw):
        method = (json or {}).get('method')
        if method == 'core.get_torrent_status' or method == 'core.get_torrents_status':
            return _Resp({'result': dict(self.status)})
        return _Resp({'result': None})

    @property
    def labels(self):
        return [e[1:] for e in self.events if e[0] == 'label']


def _grab(size_gb=1):
    return {'downloadId': 'ABC123', 'episodes': [{'id': 5}],
            'release': {'releaseTitle': 'Show.S01E01.1080p.WEB-DL-GRP', 'size': int(size_gb * GB),
                        'indexer': 'idx', 'releaseGroup': 'GRP'}}


def test_blank_or_missing_label_gets_sonarr_before_priority(monkeypatch):
    for status in ({'label': ''}, {'label': None}, {'progress': 0.0}):
        w = World(monkeypatch, dict(status))
        aw.handle_grab(_grab(), 'Sonarr')
        assert w.labels == [('abc123', 'sonarr')]
        kinds = [e[0] for e in w.events]
        assert kinds.index('label') < kinds.index('prioritize')
        assert ('ensure', 'sonarr') in w.events[:kinds.index('label')]


def test_unregistered_torrent_is_not_labeled_and_never_raises(monkeypatch):
    w = World(monkeypatch, {})
    aw.handle_grab(_grab(), 'Sonarr')
    assert w.labels == []
    assert ('prioritize', 'abc123') in w.events


def test_non_empty_label_is_never_overridden(monkeypatch):
    for existing in ('sonarr', 'sonarr-upgrade', 'tv-custom'):
        w = World(monkeypatch, {'label': existing})
        aw.handle_grab(_grab(), 'Sonarr')
        assert w.labels == []
        assert w.status['label'] == existing


def test_blank_big_upgrade_is_labeled_sonarr_first_then_upgrade_lane(monkeypatch):
    w = World(monkeypatch, {'label': '', 'progress': 0.0}, upgrade=True)
    aw.handle_grab(_grab(12), 'Sonarr')
    assert w.labels[0] == ('abc123', 'sonarr')
    assert w.labels[-1] == ('abc123', aw.SONARR_UPG_LABEL)


def test_radarr_grab_with_blank_label_is_unchanged(monkeypatch):
    w = World(monkeypatch, {'label': ''})
    data = {'downloadId': 'ABC123', 'movie': {'id': 7}, 'release': {'size': GB, 'releaseTitle': 'M.2021'}}
    aw.handle_grab(data, 'Radarr')
    assert w.labels == []
