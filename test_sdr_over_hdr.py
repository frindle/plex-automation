"""Keep HDR: an SDR upgrade may replace an HDR file only when it brings 5.1 audio the HDR file lacks and no HDR
release with 5.1 audio is available. Enforced in apply_hard_rules(); the webhook fetches hdr_surround_available
only in that one case."""
import llm_decide as ld

aw = __import__('arr-webhook')

BASE = {'quality': 'WEBDL-1080p', 'title': 'Movie.2019.1080p.WEB-DL.DDP5.1.x265-GRP', 'hdr': False,
        'existing_quality': 'Bluray-2160p', 'existing_hdr': 'HDR10'}


def gate(action='allow', **over):
    return ld.apply_hard_rules('upgrade_gate', action, dict(BASE, **over))


def test_sdr_over_hdr_rejected_without_channel_facts():
    assert gate() == ('reject', 'sdr_over_hdr')
    assert gate('throttle') == ('reject', 'sdr_over_hdr')
    assert gate('reject') == ('reject', None)


def test_surround_exception_needs_all_three():
    ok = dict(audio_channels=5.1, existing_audio_channels=2, hdr_surround_available=False)
    assert gate(**ok) == ('allow', None)
    assert gate('throttle', **ok) == ('throttle', None)
    # An HDR 5.1 release exists, or nobody could tell: keep the HDR file.
    assert gate(**dict(ok, hdr_surround_available=True)) == ('reject', 'sdr_over_hdr')
    assert gate(**dict(ok, hdr_surround_available=None)) == ('reject', 'sdr_over_hdr')
    no_key = dict(ok)
    del no_key['hdr_surround_available']
    assert gate(**no_key) == ('reject', 'sdr_over_hdr')
    # The candidate is stereo or unknown, or the HDR file already has 5.1.
    assert gate(**dict(ok, audio_channels=2.0)) == ('reject', 'sdr_over_hdr')
    assert gate(**dict(ok, audio_channels=None)) == ('reject', 'sdr_over_hdr')
    assert gate(**dict(ok, existing_audio_channels=5.1)) == ('reject', 'sdr_over_hdr')
    assert gate(**dict(ok, existing_audio_channels=7.1)) == ('reject', 'sdr_over_hdr')
    # Existing channels unknown: the search decides.
    assert gate(**dict(ok, existing_audio_channels=None)) == ('allow', None)


def test_web_dv_file_is_not_kept_but_disc_dv_is():
    assert gate(existing_quality='WEBDL-2160p', existing_hdr='DV') == ('allow', None)
    assert gate(existing_quality='Remux-2160p', existing_hdr='DV') == ('reject', 'sdr_over_hdr')
    assert gate(existing_quality='WEBDL-2160p', existing_hdr='DV HDR10') == ('reject', 'sdr_over_hdr')


def test_only_a_sure_sdr_candidate_is_vetoed():
    assert gate(hdr=True) == ('allow', None)
    # An untagged 2160p release may be HDR: the model decides.
    assert gate(quality='Bluray-2160p', title='Movie.2019.2160p.BluRay.x265-GRP') == ('allow', None)
    assert gate(quality='WEBDL-2160p', title='Movie.2019.2160p.WEB-DL.SDR.DDP5.1.x265-GRP') == ('reject', 'sdr_over_hdr')
    assert gate(quality=None, title='Movie') == ('allow', None)


def test_no_veto_when_existing_is_sdr_or_facts_missing():
    for v in (None, '', 'none', 'false'):
        assert gate(existing_hdr=v) == ('allow', None)
    facts = dict(BASE)
    del facts['existing_hdr']
    assert ld.apply_hard_rules('upgrade_gate', 'allow', facts) == ('allow', None)
    facts = dict(BASE)
    del facts['hdr']
    assert ld.apply_hard_rules('upgrade_gate', 'allow', facts) == ('allow', None)


def test_exception_possible_flags_only_the_lookup_case():
    f = dict(BASE, audio_channels=5.1, existing_audio_channels=2.0)
    assert ld.sdr_surround_exception_possible(f)
    assert ld.sdr_surround_exception_possible(dict(f, existing_audio_channels=None))
    assert not ld.sdr_surround_exception_possible(dict(f, existing_audio_channels=5.1))
    assert not ld.sdr_surround_exception_possible(dict(f, audio_channels=2.0))
    assert not ld.sdr_surround_exception_possible(dict(f, hdr=True))
    assert not ld.sdr_surround_exception_possible(dict(f, existing_hdr=None))
    assert not ld.sdr_surround_exception_possible(None)


def test_comparison_states_audio_and_availability():
    c = ld.upgrade_comparison(dict(BASE, audio_channels=5.1, existing_audio_channels=2.0,
                                   hdr_surround_available=False))
    assert 'audio gains surround (5.1 vs 2 channels)' in c
    assert 'no HDR release with 5.1 audio is available' in c
    c = ld.upgrade_comparison(dict(BASE, audio_channels=2.0, existing_audio_channels=5.1,
                                   hdr_surround_available=True))
    assert 'audio loses surround (2 vs 5.1 channels)' in c
    assert 'an HDR release with 5.1 audio is available' in c
    same = ld.upgrade_comparison(dict(BASE, audio_channels=5.1, existing_audio_channels=7.1))
    assert 'surround' not in same


def test_policy_states_the_rule():
    assert 'hdr_surround_available' in ld.POLICY['upgrade_gate']


# ── webhook side ──────────────────────────────────────────────────────────────

def test_release_name_channels():
    ch = lambda t: aw._llm_tags(t)['channels']
    assert ch('Movie.2019.1080p.BluRay.DDP5.1.x265-GRP') == 5.1
    assert ch('Show.S01E05.1080p.WEB-DL.AAC2.0.H.264-GRP') == 2.0
    assert ch('Movie.2160p.UHD.BluRay.TrueHD.Atmos.7.1.HDR.x265-G') == 7.1
    assert ch('Movie.1080p.BluRay.DTS-HD.MA.5.1.x264-G') == 5.1
    assert ch('Movie 2019 1080p WEB-DL DD+ 5.1 H264-G') == 5.1
    assert ch('Movie.2019.1080p.WEB.DDP.Atmos.H264-G') == 5.1
    assert ch('Show.S02E01.1080p.WEB.h264.5.1-G') == 5.1
    assert ch('Movie.1080p.WEB.H264-GRP') is None
    assert ch('Apollo.13.1995.1080p.BluRay.x264-G') is None
    assert ch('Ocean.8.2018.1080p.WEB-DL.H264-G') is None
    assert ch('') is None


def test_file_facts_carry_audio_channels():
    f = aw._file_facts({'size': 2 * 1024 ** 3, 'mediaInfo': {'audioChannels': 5.1, 'videoDynamicRangeType': 'HDR10'}})
    assert f['existing_audio_channels'] == 5.1 and f['existing_hdr'] == 'HDR10'
    assert aw._file_facts({'mediaInfo': {}})['existing_audio_channels'] is None


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def _route(mp, payload, calls):
    def get(url, headers=None, params=None, timeout=None, **kw):
        calls.append((url, params))
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)
    mp.setattr(aw, 'requests', type('R', (), {'get': staticmethod(get)})())


def rel(title, quality='Bluray-2160p', seeders=50, rejections=()):
    return {'title': title, 'quality': {'quality': {'name': quality}}, 'seeders': seeders,
            'rejections': list(rejections)}


HDR51 = 'Movie.2019.2160p.BluRay.DDP5.1.HDR.x265-A'
RADARR = {'movie': {'id': 7}}


def test_search_finds_hdr_surround_with_only_soft_rejections(monkeypatch):
    calls = []
    _route(monkeypatch, [rel(HDR51, rejections=['Existing file on disk is of equal or higher preference: x'])], calls)
    assert aw._hdr_surround_available('Radarr', RADARR, 'WEBDL-1080p') is True
    assert calls[0][0].endswith('/api/v3/release') and calls[0][1] == {'movieId': 7}


def test_search_ignores_unusable_releases(monkeypatch):
    bad = [
        rel(HDR51, rejections=['Size too large']),
        rel(HDR51, seeders=3),
        rel(HDR51, seeders=None),
        rel('Movie.2019.2160p.WEB-DL.DDP5.1.DV.H265-A', quality='WEBDL-2160p'),
        rel('Movie.2019.2160p.BluRay.AAC2.0.HDR.x265-A'),
        rel('Movie.2019.2160p.BluRay.x265-A'),
        rel('Movie.2019.720p.BluRay.DDP5.1.HDR.x265-A', quality='Bluray-720p'),
        'junk',
    ]
    _route(monkeypatch, bad, [])
    assert aw._hdr_surround_available('Radarr', RADARR, 'WEBDL-1080p') is False


def test_search_failure_is_unknown(monkeypatch):
    _route(monkeypatch, RuntimeError('timeout'), [])
    assert aw._hdr_surround_available('Radarr', RADARR, 'WEBDL-1080p') is None
    assert aw._hdr_surround_available('Radarr', {'movie': {}}, 'WEBDL-1080p') is None


def test_sonarr_searches_episode_or_season(monkeypatch):
    calls = []
    _route(monkeypatch, [], calls)
    one = {'series': {'id': 3}, 'episodes': [{'id': 11, 'seasonNumber': 2}]}
    pack = {'series': {'id': 3}, 'episodes': [{'id': 11, 'seasonNumber': 2}, {'id': 12, 'seasonNumber': 2}]}
    assert aw._hdr_surround_available('Sonarr', one, 'WEBDL-1080p') is False
    assert aw._hdr_surround_available('Sonarr', pack, 'WEBDL-1080p') is False
    assert [c[1] for c in calls] == [{'episodeId': 11}, {'seriesId': 3, 'seasonNumber': 2}]
    assert aw._hdr_surround_available('Sonarr', {'episodes': []}, 'WEBDL-1080p') is None


def test_gate_extra_searches_only_in_the_exception_case(monkeypatch):
    searched = []
    monkeypatch.setattr(aw, '_grab_freeleech', lambda *a: 'no')
    monkeypatch.setattr(aw, '_grab_seeders', lambda *a: 40)
    monkeypatch.setattr(aw, '_hdr_surround_available', lambda s, d, q: searched.append(q) or False)
    existing = {'existing_quality': 'Bluray-2160p', 'existing_hdr': 'HDR10', 'existing_audio_channels': 2.0}
    monkeypatch.setattr(aw, '_existing_file_facts', lambda s, d: dict(existing))
    facts = {'quality': 'WEBDL-1080p', 'title': 'x', 'hdr': False, 'audio_channels': 5.1}
    out = aw._upgrade_gate_extra('Radarr', RADARR, 'h', facts)
    assert out['hdr_surround_available'] is False and searched == ['WEBDL-1080p']
    out = aw._upgrade_gate_extra('Radarr', RADARR, 'h', dict(facts, audio_channels=2.0))
    assert 'hdr_surround_available' not in out and len(searched) == 1
    out = aw._upgrade_gate_extra('Radarr', RADARR, 'h')
    assert 'hdr_surround_available' not in out and len(searched) == 1
