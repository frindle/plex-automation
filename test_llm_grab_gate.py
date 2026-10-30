"""handle_grab upgrade gate consults llm_advisor.advise('upgrade_gate', facts, rules_action, record=...).

rules_action is 'throttle' when the upgrade label would be applied (upgrade >= 10GB) else 'allow'.
Only 'reject' (remove via remove_torrent) and 'throttle' (apply the upgrade label) change anything;
an exception from advise never breaks the grab.
"""
import pytest

aw = __import__('arr-webhook')

GB = 1024 ** 3
DV_WEB = 'Some.Movie.2021.2160p.WEB-DL.DV.HDR10.DDP5.1.x265-GRP'


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


class World:
    def __init__(self, mp, upgrade=True, advise=None):
        self.labeled, self.removed, self.posts, self.prioritized, self.calls = [], [], [], [], []
        mp.delenv('LLM_DECISIONS', raising=False)
        mp.setattr(aw, 'is_upgrade_radarr', lambda data: 'upgrade' if upgrade else None)
        mp.setattr(aw, 'time', type('T', (), {'sleep': staticmethod(lambda s: None),
                                                       'time': staticmethod(lambda: 0.0)})())
        mp.setattr(aw, 'deluge_login', lambda: None)
        mp.setattr(aw, 'session', type('S', (), {'post': staticmethod(self.post)})())
        mp.setattr(aw, 'ensure_label_exists_named', lambda label: None)
        mp.setattr(aw, 'set_torrent_label', lambda h, label: self.labeled.append((h, label)))
        mp.setattr(aw, 'dedupe_grabbed_release', lambda source, h: None)
        mp.setattr(aw, 'prioritize_new_grab', lambda source, h: self.prioritized.append(h))
        mp.setattr(aw, 'remove_torrent',
                            lambda h, remove_data=True, info=None: self.removed.append((h, remove_data)))
        mp.setattr(aw, 'record_activity', lambda *a, **k: None)
        real = aw.llm_advisor.advise

        def spy(kind, facts, rules_action, **kw):
            self.calls.append((kind, facts, rules_action, kw))
            if advise is None:
                return real(kind, facts, rules_action, **kw)
            return advise(kind, facts, rules_action)
        mp.setattr(aw.llm_advisor, 'advise', spy)

    def post(self, url, json=None, timeout=None, **kw):
        method = (json or {}).get('method')
        self.posts.append(method)
        if method == 'core.get_torrent_status':
            return _Resp({'result': {}})
        return _Resp({'result': None})


def _grab(size_gb, title=DV_WEB):
    return {'downloadId': 'ABCDEF', 'movie': {'id': 7, 'title': 'Some Movie', 'year': 2021},
            'release': {'releaseTitle': title, 'size': int(size_gb * GB), 'indexer': 'idx',
                        'releaseGroup': 'GRP', 'quality': 'WEBDL-2160p'}}


def test_off_mode_asks_with_throttle_and_behaviour_is_unchanged(monkeypatch):
    w = World(monkeypatch)
    aw.handle_grab(_grab(12), 'Radarr')
    assert [(k, r) for k, _, r, _ in w.calls] == [('upgrade_gate', 'throttle')]
    assert w.labeled == [('abcdef', aw.RADARR_UPG_LABEL)]
    assert 'core.queue_bottom' in w.posts
    assert w.removed == []
    w2 = World(monkeypatch)
    aw.handle_grab(_grab(4), 'Radarr')
    assert [(k, r) for k, _, r, _ in w2.calls] == [('upgrade_gate', 'allow')]
    assert w2.labeled == [] and w2.removed == []


def test_facts_are_json_safe_and_carry_release_markers(monkeypatch):
    w = World(monkeypatch)
    aw.handle_grab(_grab(12), 'Radarr')
    assert len(w.calls) == 1
    kind, facts, rules_action, kw = w.calls[0]
    assert callable(kw.get('record'))
    assert facts['candidate_dv_web'] is True
    assert facts['seed_days'] == aw.SEED_DAYS
    assert facts['title'] == DV_WEB
    assert facts['size_gb'] == pytest.approx(12, abs=0.01)
    import json
    json.dumps(facts)


def test_reject_removes_through_remove_torrent_and_skips_label(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'reject')
    aw.handle_grab(_grab(12), 'Radarr')
    assert w.removed == [('abcdef', True)]
    assert w.labeled == []
    assert 'core.queue_bottom' not in w.posts


def test_throttle_labels_a_small_upgrade_and_allow_never_unthrottles(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'throttle')
    aw.handle_grab(_grab(4), 'Radarr')
    assert w.calls and w.calls[0][2] == 'allow'
    assert w.labeled == [('abcdef', aw.RADARR_UPG_LABEL)]
    w2 = World(monkeypatch, advise=lambda k, f, r: 'allow')
    aw.handle_grab(_grab(12), 'Radarr')
    assert w2.calls and w2.calls[0][2] == 'throttle'
    assert w2.labeled == [('abcdef', aw.RADARR_UPG_LABEL)]
    assert w2.removed == []


def test_advise_exception_never_breaks_the_grab(monkeypatch):
    def boom(k, f, r):
        raise RuntimeError('advisor down')
    w = World(monkeypatch, advise=boom)
    aw.handle_grab(_grab(12), 'Radarr')
    assert len(w.calls) == 1
    assert w.labeled == [('abcdef', aw.RADARR_UPG_LABEL)]
    assert w.removed == []
    w2 = World(monkeypatch, upgrade=False, advise=boom)
    aw.handle_grab(_grab(12), 'Radarr')
    assert w2.prioritized == ['abcdef'] and w2.calls == [] and w2.labeled == []
