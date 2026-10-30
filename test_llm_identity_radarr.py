"""Radarr importBlocked auto-import consults llm_advisor.advise('import_identity', facts, 'match', record=...)
right before ManualImport. 'unsure'/'mismatch' go to the existing Pushover/manual-review path; an advisor
exception never blocks the import. Driven through the real check_import_blocked()."""
import json

aw = __import__('arr-webhook')


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


class World:
    def __init__(self, mp, advise=None, queue_title='Some.Movie.2015.1080p.BluRay.x264-GRP', year=2015,
                 candidates=None):
        self.commands, self.pushes, self.calls = [], [], []
        self.queue = [{'id': 1, 'downloadId': 'HASH1', 'movieId': 42, 'title': queue_title,
                       'trackedDownloadState': 'importBlocked',
                       'statusMessages': [{'messages': ['Manual Import required']}]}]
        self.candidates = candidates if candidates is not None else [
            {'path': '/data/Downloads/x/movie.mkv', 'movie': {'id': 42, 'title': 'Some Movie', 'year': year},
             'rejections': [], 'quality': {'q': 1}, 'languages': [], 'releaseGroup': 'GRP'}]
        mp.delenv('LLM_DECISIONS', raising=False)
        mp.setattr(aw, 'RADARR_API_KEY', 'rk')
        mp.setattr(aw, 'SONARR_API_KEY', '')
        mp.setattr(aw, '_ib_seen', {})
        mp.setattr(aw, 'requests', type('R', (), {'get': staticmethod(self.get), 'post': staticmethod(self.post)})())
        mp.setattr(aw, 'send_pushover', lambda title, message, url=None: self.pushes.append((title, message)))
        mp.setattr(aw, 'record_activity', lambda *a, **k: None)
        real = aw.llm_advisor.advise

        def recorder(kind, facts, rules_action, **kw):
            self.calls.append((kind, facts, rules_action, kw))
            if advise is None:
                return real(kind, facts, rules_action, **kw)
            return advise(kind, facts, rules_action)
        mp.setattr(aw.llm_advisor, 'advise', recorder)

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        if url.endswith('/api/v3/queue'):
            return _Resp({'records': self.queue})
        if url.endswith('/api/v3/manualimport'):
            return _Resp(self.candidates)
        raise AssertionError(f'unexpected GET {url}')

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.commands.append((json or {}).get('name'))
        return _Resp({'id': 1})


def test_off_mode_asks_with_match_and_still_imports(monkeypatch):
    w = World(monkeypatch)
    aw.check_import_blocked()
    assert [(k, r) for k, _, r, _ in w.calls] == [('import_identity', 'match')]
    assert callable(w.calls[0][3].get('record'))
    assert w.commands == ['ManualImport']
    assert w.pushes == []


def test_facts_carry_titles_and_year_matches(monkeypatch):
    w = World(monkeypatch)
    aw.check_import_blocked()
    assert len(w.calls) == 1
    facts = w.calls[0][1]
    assert facts['queue_title'] == 'Some.Movie.2015.1080p.BluRay.x264-GRP'
    assert facts['matched_title'] == 'Some Movie'
    assert facts['matched_year'] == 2015
    assert facts['year_matches'] is True
    json.dumps(facts)
    w2 = World(monkeypatch, queue_title='Some.Movie.2019.1080p.WEB.x264-GRP')
    aw.check_import_blocked()
    assert w2.calls and w2.calls[0][1]['year_matches'] is False


def test_unsure_goes_to_manual_review_not_import(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'unsure')
    aw.check_import_blocked()
    assert len(w.calls) == 1
    assert w.commands == []
    assert len(w.pushes) == 1 and 'Some.Movie.2015' in w.pushes[0][1]


def test_mismatch_goes_to_manual_review_not_import(monkeypatch):
    w = World(monkeypatch, advise=lambda k, f, r: 'mismatch')
    aw.check_import_blocked()
    assert len(w.calls) == 1
    assert 'ManualImport' not in w.commands
    assert [t for t, _ in w.pushes] == ['Radarr import stuck']


def test_advise_exception_never_blocks_the_import(monkeypatch):
    def boom(k, f, r):
        raise RuntimeError('advisor down')
    w = World(monkeypatch, advise=boom)
    aw.check_import_blocked()
    assert len(w.calls) == 1
    assert w.commands == ['ManualImport']
    assert w.pushes == []
