"""Grab-time identity check: alert-only, never raises. Tiers: (a) hard-rule flag + model not 'match' -> push + log,
(b) flag but model 'match' -> log only, (c) clean rules + model 'mismatch' -> log only."""
aw = __import__('arr-webhook')

BAD = {'downloadId': 'ABC', 'series': {'id': 7, 'title': 'The Traitors (US)', 'year': 2023},
       'release': {'releaseTitle': 'The Traitors New Blood S01E08 1080p WEB h264-GRP'}}
GOOD = {'downloadId': 'DEF', 'series': {'id': 8, 'title': 'Some Show', 'year': 2010},
        'release': {'releaseTitle': 'Some.Show.S01E02.1080p.WEB.x264-GRP'}}


class World:
    def __init__(self, mp, model='off', alts=None, model_raises=False):
        self.pushes, self.acts, self.asked = [], [], []
        mp.delenv('GRAB_IDENTITY_CHECK', raising=False)
        mp.setattr(aw, '_ib_seen', {})
        mp.setattr(aw, '_llm_mode', lambda kind=None: 'off' if model == 'off' else 'shadow')
        mp.setattr(aw, 'send_pushover', lambda t, m, url=None: self.pushes.append((t, m)))
        mp.setattr(aw, 'record_activity', lambda c, s: self.acts.append((c, s)))
        mp.setattr(aw, '_grab_alt_titles', lambda source, item_id: list(alts or []))

        def advise(kind, facts, rules_action, info=None):
            self.asked.append(kind)
            if model_raises:
                raise RuntimeError('boom')
            if info is not None:
                if model == 'pending':
                    info['pending'] = True
                else:
                    info['llm_action'] = model
            return rules_action
        mp.setattr(aw, '_llm_advise', advise)


def test_flagged_pushes_once_when_model_off(monkeypatch):
    w = World(monkeypatch)
    aw._grab_identity_check(BAD, 'Sonarr')
    aw._grab_identity_check(BAD, 'Sonarr')
    assert len(w.pushes) == 1 and w.pushes[0][0] == 'Possible wrong match'
    assert 'was grabbed for The Traitors (US)' in w.pushes[0][1]
    assert [c for c, _ in w.acts] == ['identity-grab']
    assert w.asked == []


def test_flagged_model_unsure_or_pending_pushes(monkeypatch):
    for m in ('unsure', 'mismatch', 'pending'):
        w = World(monkeypatch, model=m)
        aw._grab_identity_check(BAD, 'Sonarr')
        assert len(w.pushes) == 1, m


def test_tier_b_flag_but_model_match_logs_only(monkeypatch):
    w = World(monkeypatch, model='match')
    aw._grab_identity_check(BAD, 'Sonarr')
    assert w.pushes == [] and len(w.acts) == 1 and 'log only' in w.acts[0][1]


def test_tier_c_model_mismatch_clean_rules_logs_only(monkeypatch):
    w = World(monkeypatch, model='mismatch')
    aw._grab_identity_check(GOOD, 'Sonarr')
    assert w.pushes == [] and len(w.acts) == 1 and 'model mismatch' in w.acts[0][1]


def test_clean_grab_no_alert(monkeypatch):
    w = World(monkeypatch, model='match')
    aw._grab_identity_check(GOOD, 'Sonarr')
    assert w.pushes == [] and w.acts == []


def test_alternate_title_clears_flag(monkeypatch):
    w = World(monkeypatch, alts=['The Traitors New Blood'])
    aw._grab_identity_check(BAD, 'Sonarr')
    assert w.pushes == [] and w.acts == []


def test_radarr_flagged_and_year(monkeypatch):
    w = World(monkeypatch)
    data = {'downloadId': 'R1', 'movie': {'id': 3, 'title': 'Alien', 'year': 1979},
            'release': {'releaseTitle': 'Alien 1986 1080p BluRay x264-GRP'}}
    aw._grab_identity_check(data, 'Radarr')
    assert len(w.pushes) == 1 and 'year_mismatch' in w.pushes[0][1]


def test_exceptions_swallowed(monkeypatch):
    World(monkeypatch, model='match', model_raises=True)
    monkeypatch.setattr(aw, 'send_pushover', lambda *a, **k: 1 / 0)
    aw._grab_identity_check(BAD, 'Sonarr')
    aw._grab_identity_check(None, 'Sonarr')
    aw._grab_identity_check({'series': 5}, 'Radarr')
    monkeypatch.setattr(aw.threading, 'Thread', lambda *a, **k: 1 / 0)
    aw._start_grab_identity_check(BAD, 'Sonarr')


def test_flag_zero_does_nothing(monkeypatch):
    w = World(monkeypatch, model='mismatch')
    monkeypatch.setenv('GRAB_IDENTITY_CHECK', '0')
    aw._grab_identity_check(BAD, 'Sonarr')
    started = []
    monkeypatch.setattr(aw.threading, 'Thread', lambda *a, **k: started.append(1))
    aw._start_grab_identity_check(BAD, 'Sonarr')
    assert w.pushes == [] and w.acts == [] and w.asked == [] and started == []


def test_handle_grab_starts_thread_before_upgrade_return(monkeypatch):
    World(monkeypatch)
    seen = []
    monkeypatch.setattr(aw, '_start_grab_identity_check', lambda d, s: seen.append(s))
    monkeypatch.setattr(aw, 'label_blank_sonarr_grab', lambda h: None)
    monkeypatch.setattr(aw, 'is_upgrade_sonarr', lambda d: False)
    monkeypatch.setattr(aw, 'prioritize_new_grab', lambda s, h: None)
    aw.handle_grab(BAD, 'Sonarr')
    assert seen == ['Sonarr']
