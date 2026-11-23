"""dupe_confirm: the check before /plex-dupe-fix removes a copy. Code guards keep different episodes, years,
cuts and running times in every mode; the model is free layers only, shadow logs only, and enforce removes a
copy only on a confident "same". File names are synthetic."""
import pytest

import llm_advisor as la
import llm_decide as ld

aw = __import__('arr-webhook')


def F(keeper, loser, kd=None, ld_=None, **kw):
    f = {'media_type': 'movie', 'title': 'Some Movie', 'keeper_file': keeper, 'loser_file': loser,
         'keeper_duration_min': kd, 'loser_duration_min': ld_}
    f.update(kw)
    return f


# ---- name parsing ----

@pytest.mark.parametrize('name,eps', [
    ('Show.S01E02.1080p.WEB.x265-GRP.mkv', {(1, 2)}),
    ('Show - S01E01E02 - Pilot.mkv', {(1, 1), (1, 2)}),
    ('Show.S02E05-E07.720p.mkv', {(2, 5), (2, 6), (2, 7)}),
    ('Show.S02E05-07.720p.mkv', {(2, 5), (2, 6), (2, 7)}),
    ('Show.S01E01-1080p.mkv', {(1, 1)}),
    ('Some.Movie.2021.1080p.mkv', set()),
])
def test_file_episodes(name, eps):
    assert ld.file_episodes(name) == frozenset(eps)


# Sonarr's "Series - SxxEyy - Episode Title" names: a title starting with a number is not a range, and words
# or years in a title are not edition tags or release years (offload review of PR #34).
SONARR_PAIRS = [
    ('Show (2005) - S01E05 - 7 Days [WEBDL-1080p].mkv', 'Show.S01E05.1080p.WEB.x265-GRP.mkv'),
    ('Show - S02E03 - 24 Hours.mkv', 'Show.S02E03.720p.HDTV.x264-GRP.mkv'),
    ('Show - S01E09 - 10 Years Later.mkv', 'Show.S01E09.1080p.WEB.x265-GRP.mkv'),
    ('Show - S02E03 - The Extended Family.mkv', 'Show.S02E03.1080p.WEB.x265-GRP.mkv'),
    ('Show - S03E01 - Theatrical Release [HDTV-720p].mkv', 'Show.S03E01.720p.HDTV.x264-GRP.mkv'),
    ('Show - S03E02 - Redux.mkv', 'Show.S03E02.1080p.WEB.x265-GRP.mkv'),
    ('Show - S03E03 - Special Edition.mkv', 'Show.S03E03.1080p.WEB.x265-GRP.mkv'),
    ('Show - S03E04 - Alternate Ending.mkv', 'Show.S03E04.1080p.WEB.x265-GRP.mkv'),
    ('Show - S03E05 - Back to 1984.mkv', 'Show.2019.S03E05.1080p.WEB.x265-GRP.mkv'),
    ('Talk Show - 2026-10-30 - The Extended Interview.mkv', 'Talk.Show.2026.10.30.Guest.1080p.mkv'),
]


@pytest.mark.parametrize('keeper,loser', SONARR_PAIRS)
def test_sonarr_episode_titles_are_not_conflicts(keeper, loser):
    f = F(keeper, loser, media_type='episode')
    assert ld.dupe_conflict(f) is None
    assert ld.dupe_conflict(F(loser, keeper, media_type='episode')) is None
    assert 'not the same media' not in ld.dupe_comparison(f)


@pytest.mark.parametrize('name,eps', [
    ('Show (2005) - S01E05 - 7 Days [WEBDL-1080p].mkv', {(1, 5)}),
    ('Show - S02E03 - 24 Hours.mkv', {(2, 3)}),
    ('Show - S01E09 - 10 Years Later.mkv', {(1, 9)}),
    ('Show - S01E05-E06 - Two Parter.mkv', {(1, 5), (1, 6)}),
    ('Show - S01E05-06 - Two Parter.mkv', {(1, 5), (1, 6)}),
])
def test_sonarr_names_episodes(name, eps):
    assert ld.file_episodes(name) == frozenset(eps)


def test_sonarr_names_still_catch_real_differences():
    assert ld.dupe_conflict(F('Show - S02E03 - 24 Hours.mkv', 'Show.S02E04.720p.mkv'))[0] == 'episode_differs'
    assert ld.dupe_conflict(F('Show - S01E05-06 - Two Parter.mkv', 'Show.S01E05.720p.mkv'))[0] == \
        'episode_differs'
    assert ld.dupe_conflict(F('Show (2005) - S01E01 - Pilot.mkv', 'Show.1987.S01E01.720p.mkv'))[0] == \
        'year_differs'
    # a real extended episode cut shows up in the running time
    assert ld.dupe_conflict(F('Show - S01E01 - Pilot.mkv', 'Show.S01E01.Extended.720p.mkv', 44, 58))[0] == \
        'duration_differs'


def test_movie_titles_keep_the_edition_guard():
    assert ld.dupe_conflict(F('Some Movie (2010).mkv', 'Some.Movie.2010.Extended.1080p.mkv'))[0] == \
        'edition_differs'


def test_file_years_skip_daily_dates_and_resolutions():
    assert ld.file_years('Some.Movie.2021.2160p.mkv') == {2021}
    assert ld.file_years('Talk.Show.2026.10.30.Guest.1080p.mkv') == frozenset()
    assert ld.file_years('Film.1080p.x264.mkv') == frozenset()


@pytest.mark.parametrize('name,eds', [
    ('Some.Movie.2010.Extended.1080p.mkv', {'extended'}),
    ("Some Movie (2010) Director's Cut.mkv", {'directors cut'}),
    ('Some.Movie.2010.Directors.Cut.mkv', {'directors cut'}),
    ('Some.Movie.2010.UNCUT.mkv', {'unrated'}),
    ('Some Movie (2010) {edition-Extended Edition}.mkv', {'extended'}),
    ('Some Movie (2010) {edition-Criterion}.mkv', {'criterion'}),
    ('Some.Movie.2010.REMASTERED.1080p.mkv', set()),
    ('Some.Movie.2010.1080p.mkv', set()),
])
def test_file_editions(name, eds):
    assert ld.file_editions(name) == frozenset(eds)


# ---- code guards ----

def test_conflict_episode():
    rule, words = ld.dupe_conflict(F('Show.S01E01.mkv', 'Show.S01E02.mkv'))
    assert rule == 'episode_differs' and 'S01E01' in words and 'S01E02' in words


def test_conflict_multi_episode_file_is_different():
    assert ld.dupe_conflict(F('Show.S01E01E02.mkv', 'Show.S01E01.mkv'))[0] == 'episode_differs'


def test_conflict_year_two_apart_but_not_one():
    assert ld.dupe_conflict(F('Film.1998.mkv', 'Film.2018.mkv'))[0] == 'year_differs'
    assert ld.dupe_conflict(F('Film.2018.mkv', 'Film.2019.mkv')) is None


def test_title_year_in_both_names_is_no_conflict():
    assert ld.dupe_conflict(F('Blade.Runner.2049.2017.2160p.mkv', 'Blade Runner 2049 (2017).mkv')) is None


def test_conflict_edition():
    rule, words = ld.dupe_conflict(F('Film.2010.Extended.2160p.mkv', 'Film.2010.1080p.mkv'))
    assert rule == 'edition_differs' and 'extended' in words and 'none' in words


def test_conflict_duration_but_not_pal_speedup():
    assert ld.dupe_conflict(F('Film.2010.a.mkv', 'Film.2010.b.mkv', 150, 120))[0] == 'duration_differs'
    assert ld.dupe_conflict(F('Film.2010.a.mkv', 'Film.2010.b.mkv', 120, 115.2)) is None
    assert ld.dupe_conflict(F('Show.S01E01.a.mkv', 'Show.S01E01.b.mkv', 45, 41)) is None
    assert ld.dupe_conflict(F('Film.2010.a.mkv', 'Film.2010.b.mkv', 0, 120)) is None


def test_quality_differences_are_no_conflict():
    assert ld.dupe_conflict(F('Film.2010.2160p.x265-A.mkv', 'Film.2010.720p.x264-B.mkv', 118, 118.4)) is None


def test_hard_rule_turns_same_into_different():
    assert ld.apply_hard_rules('dupe_confirm', 'same', F('Film.2010.Extended.mkv', 'Film.2010.mkv')) == \
        ('different', 'edition_differs')
    assert ld.apply_hard_rules('dupe_confirm', 'same', F('Film.2010.a.mkv', 'Film.2010.b.mkv')) == ('same', None)
    assert ld.apply_hard_rules('dupe_confirm', 'unsure', F('Film.2010.Extended.mkv', 'Film.2010.mkv')) == \
        ('unsure', None)


def test_malformed_duration_fails_closed():
    out = ld.apply_hard_rules('dupe_confirm', 'same', F('Film.2010.a.mkv', 'Film.2010.b.mkv', 'nan', 120))
    assert out[0] == 'same'  # nan reads as unknown (no veto), never as a crash
    out = ld.apply_hard_rules('dupe_confirm', 'same', 'not a dict')
    assert out == ('same', None)


# ---- prompt and resolve ----

def test_prompt_carries_policy_and_comparison():
    p = ld.build_prompt('dupe_confirm', F('Film.2010.2160p.mkv', 'Film.2010.720p.mkv', 118, 118))
    assert 'Allowed actions: same, different, unsure' in p
    assert 'years agree (2010)' in p and 'neither name carries an edition tag' in p
    p = ld.build_prompt('dupe_confirm', F('Film.2010.Extended.mkv', 'Film.2010.mkv'))
    assert 'not the same media' in p


def test_resolve_keeps_both_on_any_non_same_answer():
    def d(a, c):
        return {'action': a, 'confidence': c, 'reason': 'r'}
    assert ld.resolve_action('dupe_confirm', 'unsure', d('same', 0.9), 'enforce') == 'same'
    assert ld.resolve_action('dupe_confirm', 'unsure', d('same', 0.5), 'enforce') == 'unsure'
    assert ld.resolve_action('dupe_confirm', 'unsure', d('different', 0.3), 'enforce') == 'different'
    assert ld.resolve_action('dupe_confirm', 'unsure', d('unsure', 0.1), 'enforce') == 'unsure'
    assert ld.resolve_action('dupe_confirm', 'same', d('different', 0.99), 'shadow') == 'same'


# ---- advisor: free layers only ----

def _layer(name, action, conf=0.9, seen=None):
    def fn(prompt):
        if seen is not None:
            seen.append(name)
        return '{"action": "%s", "confidence": %s, "reason": "r"}' % (action, conf)
    return la.Layer(name, fn, name + '-model')


def _env(tmp_path, **kw):
    e = {'LLM_PENDING_PATH': str(tmp_path / 'pending.json'), 'LLM_DECISIONS_LOG': str(tmp_path / 'log.jsonl'),
         'LLM_LOCAL_WAIT_MIN': '0'}
    e.update(kw)
    return e


@pytest.fixture(autouse=True)
def _fresh_stores():
    la._STORES.clear()
    yield
    la._STORES.clear()


def test_enforce_never_asks_paid_or_claude(tmp_path):
    seen = []
    layers = {'local': la.Layer('local', lambda p: (_ for _ in ()).throw(RuntimeError('down')), 'm'),
              'paid': _layer('paid', 'same', seen=seen), 'claude': _layer('claude', 'same', seen=seen)}
    out = la.advise('dupe_confirm', F('Film.2010.a.mkv', 'Film.2010.b.mkv'), 'unsure', mode='enforce',
                    env=_env(tmp_path), layers=layers)
    assert seen == [] and out == 'unsure'


def test_enforce_free_layer_same_is_used(tmp_path):
    layers = {'local': _layer('local', 'same', 0.95)}
    out = la.advise('dupe_confirm', F('Film.2010.a.mkv', 'Film.2010.b.mkv'), 'unsure', mode='enforce',
                    env=_env(tmp_path), layers=layers)
    assert out == 'same'


def test_hard_rule_beats_a_model_same(tmp_path):
    layers = {'local': _layer('local', 'same', 0.99)}
    out = la.advise('dupe_confirm', F('Film.2010.Extended.mkv', 'Film.2010.mkv'), 'unsure', mode='enforce',
                    env=_env(tmp_path), layers=layers)
    assert out == 'different'


def test_answers_are_cached_for_a_month(tmp_path):
    seen = []
    facts = F('Film.2010.a.mkv', 'Film.2010.b.mkv')
    t = [1_000_000.0]
    env = _env(tmp_path)
    for _ in range(2):
        la.advise('dupe_confirm', facts, 'unsure', mode='enforce', env=env, now=lambda: t[0],
                  layers={'local': _layer('local', 'same', 0.95, seen)})
        t[0] += 29 * 86400
    assert seen == ['local']
    assert la.answer_ttl_s('dupe_confirm', {}) == 720 * 3600
    assert la.answer_ttl_s('dupe_confirm', {'LLM_ANSWER_TTL_H_DUPE_CONFIRM': '2'}) == 7200
    assert la.answer_ttl_s('import_identity', {'LLM_ANSWER_TTL_H': '5'}) == 18000


def test_dupe_confirm_is_an_unraid_kind_by_default():
    assert 'dupe_confirm' in la.UNRAID_KINDS.split(',')
    assert la.local_approved('dupe_confirm', {}) == set()  # no local model until its back-test passes


# ---- /plex-dupe-fix wiring ----

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


def _m(path, res='1080', size=5.0, mid=1, dur=118.0):
    return {'file': path, 'resolution': res, 'size_gb': size, 'media_id': mid, 'duration_min': dur, 'codec': 'hevc'}


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {'entries': [], 'superseded': [], 'asked': [], 'answer': 'same'}

    def scan():
        return aw.jsonify({'ok': True, 'multi_version': state['entries']}), 200

    def advise(kind, facts, rules_action, mode=None, record=None, info=None, **kw):
        state['asked'].append((kind, facts['loser_file'], rules_action, mode))
        if info is not None:
            info.update(pending=state['answer'] == 'pending', layer='local',
                        llm_action=None if state['answer'] == 'pending' else state['answer'])
        if mode != 'enforce' or state['answer'] == 'pending':
            return rules_action
        return state['answer']

    monkeypatch.setattr(aw, 'PLEX_TOKEN', 'tok')
    monkeypatch.setattr(aw, 'plex_dupe_scan', scan)
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'ensure_label_exists', lambda: None)
    monkeypatch.setattr(aw, '_load_plex_dupe_keep', lambda: set())
    monkeypatch.setattr(aw, 'session', type('S', (), {'post': staticmethod(lambda *a, **k: _Resp({'result': {
        'H1': {'name': 'Film.2010.720p.x264-B.mkv', 'save_path': '/data/x', 'label': 'radarr', 'files': []},
        'H2': {'name': 'Film.2010.Extended.720p-B.mkv', 'save_path': '/data/x', 'label': 'radarr', 'files': []},
    }}))})())
    monkeypatch.setattr(aw, 'supersede_torrent', lambda h: state['superseded'].append(h))
    # These tests are about the checks, not disk state: every listed copy is on disk.
    monkeypatch.setattr(aw, '_plex_file_on_disk', lambda p: bool(p))
    monkeypatch.setattr(aw, 'stream_guard', lambda fail_closed=True: lambda p: False)   # nobody is watching
    monkeypatch.setattr(aw.llm_advisor, 'advise', advise)
    monkeypatch.delenv('LLM_DUPE_CONFIRM', raising=False)
    monkeypatch.delenv('LLM_DUPE_CONFIRM_MAX', raising=False)
    monkeypatch.setenv('LLM_DECISIONS', 'enforce')
    return state


def _run(apply=False, llm=False):
    qs = {}
    if apply:
        qs['apply'] = '1'
    if llm:
        qs['llm'] = '1'
    with aw.app.test_request_context('/plex-dupe-fix', query_string=qs):
        resp, status = aw.plex_dupe_fix()
    assert status == 200
    return resp.get_json()


KEEP = '/media/movies/Film (2010)/Film.2010.2160p.x265-A.mkv'
LOW = '/media/movies/Film (2010)/Film.2010.720p.x264-B.mkv'
EXT = '/media/movies/Film (2010)/Film.2010.Extended.720p-B.mkv'


def _entry(*files):
    return {'plex_key': '1', 'title': 'Film', 'year': 2010,
            'media': [_m(f, '4k' if i == 0 else '720', 40 if i == 0 else 3, i + 1) for i, f in enumerate(files)]}


def test_guard_keeps_a_different_cut_in_every_mode(world, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'off')
    world['entries'] = [_entry(KEEP, EXT)]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert act['result'].startswith('kept: edition tags differ') and world['superseded'] == []
    assert act['check'] == {'rule': 'edition_differs'} and out['checks']['rule_kept'] == 1
    assert out['checks']['review'][0]['why'] == 'edition_differs'
    assert world['asked'] == []


def test_shadow_asks_but_changes_nothing(world, monkeypatch):
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'shadow')
    world['answer'] = 'different'
    world['entries'] = [_entry(KEEP, LOW)]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert world['superseded'] == ['H1'] and act['check']['model'] == 'different'
    assert world['asked'] == [('dupe_confirm', 'Film.2010.720p.x264-B.mkv', 'same', 'shadow')]
    assert out['checks']['model_different'] == 1 and out['checks']['mode'] == 'shadow'
    assert out['checks']['review'] == [{'title': 'Film', 'keep': 'Film.2010.2160p.x265-A.mkv',
                                        'other': 'Film.2010.720p.x264-B.mkv', 'why': 'model different (shadow)'}]


def test_default_mode_is_shadow(world):
    world['entries'] = [_entry(KEEP, LOW)]
    _run(apply=True)
    assert world['asked'][0][3] == 'shadow' and world['superseded'] == ['H1']


def test_global_shadow_caps_kind_enforce(world, monkeypatch):
    monkeypatch.setenv('LLM_DECISIONS', 'shadow')
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'enforce')
    assert aw._dupe_confirm_mode() == 'shadow'
    monkeypatch.setenv('LLM_DECISIONS', 'off')
    assert aw._dupe_confirm_mode() == 'off'


@pytest.mark.parametrize('answer,removed,result', [
    ('same', ['H1'], 'torrent superseded'),
    ('unsure', [], 'kept: model check says unsure'),
    ('different', [], 'kept: model check says different'),
    ('pending', [], 'kept: model check pending'),
])
def test_enforce_removes_only_on_same(world, monkeypatch, answer, removed, result):
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'enforce')
    world['answer'] = answer
    world['entries'] = [_entry(KEEP, LOW)]
    out = _run(apply=True)
    act = out['plan']['quality_upgrade'][0]['supersede'][0]
    assert world['superseded'] == removed and act['result'].startswith(result)
    assert world['asked'][0][2] == 'unsure'


def test_dry_run_asks_only_with_llm_param(world, monkeypatch):
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'enforce')
    world['entries'] = [_entry(KEEP, LOW)]
    out = _run()
    assert world['asked'] == []
    assert out['plan']['quality_upgrade'][0]['supersede'][0]['check']['model'].startswith('not asked')
    _run(llm=True)
    assert len(world['asked']) == 1 and world['superseded'] == []


def test_per_run_cap_keeps_the_rest_in_enforce(world, monkeypatch):
    monkeypatch.setenv('LLM_DUPE_CONFIRM', 'enforce')
    monkeypatch.setenv('LLM_DUPE_CONFIRM_MAX', '1')
    world['entries'] = [_entry(KEEP, LOW), dict(_entry(KEEP.replace('Film', 'Other'), LOW.replace('Film', 'Other')),
                                                plex_key='2', title='Other')]
    out = _run(apply=True)
    acts = [a for q in out['plan']['quality_upgrade'] for a in q['supersede']]
    assert len(world['asked']) == 1
    assert acts[1]['result'].startswith('kept: model check not asked (per-run cap reached)')
    assert out['checks']['not_asked'] == 1


def test_daily_run_asks_and_reports_checks(world, monkeypatch, tmp_path):
    monkeypatch.setattr(aw, 'PLEX_DUPE_FIX_REPORT', str(tmp_path / 'r.json'))
    monkeypatch.setenv('PLEX_DUPE_FIX_DAILY', 'dry')
    monkeypatch.setattr(aw, 'record_activity', lambda *a, **k: None)
    world['answer'] = 'different'
    world['entries'] = [_entry(KEEP, LOW), dict(_entry(KEEP, EXT), plex_key='2')]
    rep = aw.plex_dupe_fix_daily()
    assert len(world['asked']) == 1 and world['superseded'] == []
    assert rep['checks']['rule_kept'] == 1 and rep['checks']['model_different'] == 1
    assert rep['checks']['rules'] == {'edition_differs': 1}
    assert [r['why'] for r in rep['checks']['review']] == ['model different (shadow)', 'edition_differs']
