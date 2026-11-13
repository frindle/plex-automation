"""Shadow mode never pays; import_identity/dupe_keeper answers are cached by pending key; an unsure identity
answer never imports in enforce; a pending identity check doesn't push 'import stuck'."""
import json

import llm_advisor as la
import llm_decide
from test_llm_chain import ENV, Clock, FakeClaude, FakeHttp, chat, run

aw = __import__('arr-webhook')
ID_FACTS = {'queue_title': 'Some.Show.S01E01.1080p.WEB-GRP', 'matched_title': 'Other Show', 'seeders': 5}


def test_shadow_past_wait_makes_no_paid_or_claude_call(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'darkbloom.dev': chat('reject')})
    claude = FakeClaude('reject')
    run(tmp_path, clock, http, claude=claude, mode='shadow')
    clock.minutes(20)
    out, info, rows = run(tmp_path, clock, http, claude=claude, mode='shadow')
    assert out == 'allow'
    assert http.n('darkbloom.dev') == 0 and claude.calls == []
    assert all(r['layer'] != 'paid' and r['layer'] != 'claude' for r in rows)


def test_enforce_past_wait_still_uses_paid(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': ConnectionError('down'), 'api.darkbloom.dev': chat('reject')})
    run(tmp_path, clock, http)
    clock.minutes(20)
    out, info, _ = run(tmp_path, clock, http)
    assert out == 'reject' and info['layer'] == 'paid'


def test_identity_answer_cached_and_volatile_facts_ignored(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': chat('match', 0.95)})
    out, info, rows = run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    assert out == 'match' and info['layer'] == 'local' and http.n('local.invalid') >= 1
    n_local, n_rows = http.n('local.invalid'), len(rows)
    clock.minutes(60)
    out, info, rows = run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS, seeders=40))
    assert out == 'match' and info['layer'] == 'cache' and info['pending'] is False
    assert http.n('local.invalid') == n_local
    assert len(rows) == n_rows  # a cache hit writes no log row


def test_identity_cache_expires_after_ttl(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': chat('match', 0.95)})
    run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    n_local = http.n('local.invalid')
    clock.minutes(24 * 60 + 1)
    _, info, _ = run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    assert info['layer'] == 'local' and http.n('local.invalid') > n_local


def test_dupe_keeper_cached_but_upgrade_gate_not(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': chat('keep_first', 0.95)})
    facts = {'first': 'A', 'second': 'B'}
    run(tmp_path, clock, http, kind='dupe_keeper', rules='keep_both', facts=facts)
    n = http.n('local.invalid')
    _, info, _ = run(tmp_path, clock, http, kind='dupe_keeper', rules='keep_both', facts=facts)
    assert info['layer'] == 'cache' and http.n('local.invalid') == n

    http2 = FakeHttp({'local.invalid': chat('allow', 0.95)})
    run(tmp_path, clock, http2)
    n2 = http2.n('local.invalid')
    _, info, _ = run(tmp_path, clock, http2)
    assert info['layer'] == 'local' and http2.n('local.invalid') > n2


def test_pending_answer_is_not_cached(tmp_path):
    clock = Clock()
    state = {'up': False}
    http = FakeHttp({'local.invalid': lambda: chat('mismatch', 0.9) if state['up'] else ConnectionError('x')})
    _, info, _ = run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    assert info['pending'] is True
    state['up'] = True
    out, info, _ = run(tmp_path, clock, http, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    assert out == 'mismatch' and info['layer'] == 'local'
    cache = json.loads((tmp_path / 'llm_answers.json').read_text())
    assert len(cache) == 1 and list(cache.values())[0]['decision']['action'] == 'mismatch'


def test_answer_ttl_zero_disables_cache(tmp_path):
    clock = Clock()
    http = FakeHttp({'local.invalid': chat('match', 0.95)})
    env = dict(ENV, LLM_ANSWER_TTL_H='0')
    run(tmp_path, clock, http, env=env, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    n = http.n('local.invalid')
    _, info, _ = run(tmp_path, clock, http, env=env, kind='import_identity', rules='match', facts=dict(ID_FACTS))
    assert info['layer'] == 'local' and http.n('local.invalid') > n
    assert not (tmp_path / 'llm_answers.json').exists()


def test_answer_cache_survives_reload_and_drops_junk(tmp_path):
    p = tmp_path / 'a.json'
    p.write_text(json.dumps({'k1': {'at': 5.0, 'decision': {'action': 'match'}}, 'k2': {'at': 'x'}, 'k3': 7}))
    c = la.AnswerCache(str(p), now=lambda: 10.0)
    assert set(c.data) == {'k1'}
    assert c.get('k1', 100)['decision'] == {'action': 'match'}
    assert c.get('k1', 1) is None and 'k1' not in json.loads(p.read_text())


def test_unsure_identity_low_confidence_never_imports_in_enforce():
    dec = {'action': 'unsure', 'confidence': 0.5, 'reason': 'r'}
    assert llm_decide.resolve_action('import_identity', 'match', dec, 'enforce') == 'unsure'
    assert llm_decide.resolve_action('import_identity', 'match', dict(dec, confidence=0.0), 'enforce') == 'unsure'
    assert llm_decide.resolve_action('import_identity', 'match', dec, 'shadow') == 'match'
    low_mismatch = {'action': 'mismatch', 'confidence': 0.5, 'reason': 'r'}
    assert llm_decide.resolve_action('import_identity', 'match', low_mismatch, 'enforce') == 'match'
    assert llm_decide.resolve_action('import_identity', 'match', dict(dec, confidence=1.5), 'enforce') == 'match'


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


def _radarr_world(mp, answers):
    pushes, commands = [], []
    queue = [{'id': 1, 'downloadId': 'HASH1', 'movieId': 42, 'title': 'Some.Movie.2015.1080p.BluRay.x264-GRP',
              'trackedDownloadState': 'importBlocked', 'statusMessages': [{'messages': ['Manual Import required']}]}]
    cands = [{'path': '/data/Downloads/x/movie.mkv', 'movie': {'id': 42, 'title': 'Some Movie', 'year': 2015},
              'rejections': [], 'quality': {'q': 1}, 'languages': [], 'releaseGroup': 'GRP'}]

    def get(url, **kw):
        if url.endswith('/api/v3/queue'):
            return _Resp({'records': queue})
        if url.endswith('/api/v3/manualimport'):
            return _Resp(cands)
        raise AssertionError(url)

    def post(url, json=None, **kw):
        commands.append((json or {}).get('name'))
        return _Resp({'id': 1})

    mp.delenv('LLM_DECISIONS', raising=False)
    mp.setattr(aw, 'RADARR_API_KEY', 'rk')
    mp.setattr(aw, 'SONARR_API_KEY', '')
    mp.setattr(aw, '_ib_seen', {})
    mp.setattr(aw, 'requests', type('R', (), {'get': staticmethod(get), 'post': staticmethod(post)})())
    mp.setattr(aw, 'send_pushover', lambda title, message, url=None: pushes.append((title, message)))
    mp.setattr(aw, 'record_activity', lambda *a, **k: None)
    seq = iter(answers)

    def advise(kind, facts, rules_action, record=None, info=None, **kw):
        action, pending = next(seq)
        if info is not None:
            info.update(pending=pending, layer='pending' if pending else 'local')
        return action
    mp.setattr(aw.llm_advisor, 'advise', advise)
    return pushes, commands


def test_pending_identity_retries_quietly_then_pushes_once_when_unsure(monkeypatch):
    pushes, commands = _radarr_world(monkeypatch, [('unsure', True), ('unsure', True), ('unsure', False),
                                                   ('unsure', False)])
    aw.check_import_blocked()
    aw.check_import_blocked()
    assert pushes == [] and commands == [] and aw._ib_seen == {}
    aw.check_import_blocked()
    assert len(pushes) == 1 and 'did not confirm' in pushes[0][1]
    aw.check_import_blocked()
    assert len(pushes) == 1 and commands == []


def test_pending_then_match_imports_without_push(monkeypatch):
    pushes, commands = _radarr_world(monkeypatch, [('unsure', True), ('match', False)])
    aw.check_import_blocked()
    aw.check_import_blocked()
    assert pushes == [] and commands == ['ManualImport']
