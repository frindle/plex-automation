"""/plex-dupe-fix consults llm_advisor.advise('dupe_keeper', facts, rules_action, record=...) for a
two-version group. rules_action is the existing keeper choice (keep_first/keep_second, by Plex media
order). Only 'keep_both' changes anything (nothing superseded/deleted for that group); an advisor
exception never breaks the fix. Driven through the real view with POST ?apply=1."""
import json

aw = __import__('arr-webhook')

DIR = '/data/Movies/Film (2020)'
LOW = f'{DIR}/Film.2020.1080p.BluRay.x264-AAA.mkv'
HIGH = f'{DIR}/Film.2020.2160p.WEB-DL.DV.x265-BBB.mkv'


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


class World:
    def __init__(self, mp, advise=None, media=None):
        self.superseded, self.calls, self.deluge_fields = [], [], []
        media = media if media is not None else [
            {'media_id': 1, 'file': LOW, 'size_gb': 8.0, 'resolution': '1080'},
            {'media_id': 2, 'file': HIGH, 'size_gb': 20.0, 'resolution': '4k'},
        ]
        self.scan = {'ok': True, 'multi_version': [{'plex_key': '77', 'title': 'Film', 'media': media}]}
        self.torrents = {
            'hlow': {'name': LOW.rsplit('/', 1)[1], 'save_path': DIR, 'label': 'radarr', 'files': [],
                     'progress': 100.0, 'seeding_time': 30 * 86400},
            'hhigh': {'name': HIGH.rsplit('/', 1)[1], 'save_path': DIR, 'label': 'radarr', 'files': [],
                      'progress': 100.0, 'seeding_time': 2 * 86400},
        }
        mp.delenv('LLM_DECISIONS', raising=False)
        mp.setattr(aw, 'PLEX_TOKEN', 'tok')
        mp.setattr(aw, 'plex_dupe_scan', lambda: (aw.jsonify(self.scan), 200))
        mp.setattr(aw, 'deluge_login', lambda: None)
        mp.setattr(aw, 'ensure_label_exists', lambda: None)
        mp.setattr(aw, '_load_plex_dupe_keep', lambda: set())
        mp.setattr(aw, 'session', type('S', (), {'post': staticmethod(self.post)})())
        mp.setattr(aw, 'supersede_torrent', lambda h: self.superseded.append(h))
        mp.setattr(aw, 'record_activity', lambda *a, **k: None)
        real = aw.llm_advisor.advise

        def recorder(kind, facts, rules_action, **kw):
            self.calls.append((kind, facts, rules_action, kw))
            if advise is None:
                return real(kind, facts, rules_action, **kw)
            return advise(kind, facts, rules_action)
        mp.setattr(aw.llm_advisor, 'advise', recorder)

    def post(self, url, json=None, timeout=None, **kw):
        if (json or {}).get('method') == 'core.get_torrents_status':
            self.deluge_fields.append(list(json['params'][1]))
            return _Resp({'result': self.torrents})
        return _Resp({'result': None})

    def run(self):
        resp = aw.app.test_client().post('/plex-dupe-fix?apply=1')
        assert resp.status_code == 200
        return resp.get_json()['plan']


def test_off_mode_asks_with_rules_keeper_and_supersedes_as_before(monkeypatch):
    w = World(monkeypatch)
    plan = w.run()
    assert [(k, r) for k, _, r, _ in w.calls] == [('dupe_keeper', 'keep_second')]
    assert callable(w.calls[0][3].get('record'))
    assert w.superseded == ['hlow']
    assert len(plan['quality_upgrade']) == 1
    assert 'kept_by_llm' not in plan


def test_facts_describe_both_versions(monkeypatch):
    w = World(monkeypatch)
    w.run()
    assert len(w.calls) == 1
    facts = w.calls[0][1]
    json.dumps(facts)
    assert facts['seed_days'] == aw.SEED_DAYS
    first, second = facts['first'], facts['second']
    assert first['path'] == '/media/movies/Film (2020)/Film.2020.1080p.BluRay.x264-AAA.mkv'
    assert (first['size_gb'], first['res'], first['hdr'], first['seeding']) == (8.0, '1080', False, True)
    assert (second['size_gb'], second['res'], second['hdr']) == (20.0, '4k', True)
    assert first['seeded_days'] == 30.0 and second['seeded_days'] == 2.0
    assert 'seeding_time' in w.deluge_fields[0]


def test_keep_both_supersedes_and_deletes_nothing(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'keep_both')
    plan = w.run()
    assert len(w.calls) == 1
    assert w.superseded == []
    assert plan['quality_upgrade'] == []
    assert [g['plex_key'] for g in plan['kept_by_llm']] == ['77']


def test_flipped_keeper_is_not_honoured_and_groups_of_three_are_not_asked(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'keep_first')
    w.run()
    assert len(w.calls) == 1
    assert w.superseded == ['hlow']
    three = [{'media_id': i, 'file': f'{DIR}/Film.2020.{r}.x264-G{i}.mkv', 'size_gb': s, 'resolution': r}
             for i, (r, s) in enumerate([('1080', 8.0), ('4k', 20.0), ('720', 4.0)])]
    w3 = World(monkeypatch, advise=lambda k, f, r: 'keep_both', media=three)
    plan = w3.run()
    assert w3.calls == []
    assert len(plan['quality_upgrade']) == 1


def test_advise_exception_never_breaks_the_fix(monkeypatch):
    def boom(k, f, r):
        raise RuntimeError('advisor down')
    w = World(monkeypatch, advise=boom)
    plan = w.run()
    assert len(w.calls) == 1
    assert w.superseded == ['hlow']
    assert len(plan['quality_upgrade']) == 1
