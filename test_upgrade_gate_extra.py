"""Upgrade-gate facts beyond the release name: freeleech from indexerFlags (payload, else grab history), the file
the upgrade replaces, and Deluge swarm seeders. Fetched only when LLM_DECISIONS is shadow/enforce."""
aw = __import__('arr-webhook')
RF, SF = aw._RADARR_INDEXER_FLAGS, aw._SONARR_INDEXER_FLAGS
GB = 1024 ** 3


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError('http %d' % self.status)


def _requests(mp, routes, calls):
    def get(url, headers=None, params=None, timeout=None, **kw):
        calls.append((url, params))
        for frag, out in routes.items():
            if frag in url:
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError('unrouted ' + url)
    mp.setattr(aw, 'requests', type('R', (), {'get': staticmethod(get)})())


def test_flag_bits_accepts_int_names_and_strings():
    assert aw._flag_bits(None, RF) is None and aw._flag_bits('', RF) is None and aw._flag_bits([], RF) is None
    assert aw._flag_bits(True, RF) is None
    assert aw._flag_bits(5, RF) == 5
    assert aw._flag_bits(['G_Freeleech', 'g_scene'], RF) == 1 | 128
    assert aw._flag_bits('freeleech, internal', RF) == 1 | 32
    assert aw._flag_bits('freeleech,internal', SF) == 1 | 8
    assert aw._flag_bits(['g_halfleech'], SF) == 2
    assert aw._flag_bits('3', RF) == 3 and aw._flag_bits(['bogus'], RF) == 0


def test_freeleech_levels():
    assert aw._freeleech(None, RF) is None
    assert aw._freeleech(1 | 128, RF) == 'full'
    assert aw._freeleech(256, RF) == 'partial' and aw._freeleech(2, RF) == 'partial'
    assert aw._freeleech(64, SF) == 'partial'
    assert aw._freeleech(128, RF) == 'no' and aw._freeleech(0, SF) == 'no'


def test_grab_freeleech_prefers_payload_flags(monkeypatch):
    calls = []
    _requests(monkeypatch, {}, calls)
    assert aw._grab_freeleech('Radarr', {'release': {'indexerFlags': ['G_Freeleech']}}, 'H') == 'full'
    assert calls == []


def test_grab_freeleech_falls_back_to_newest_grab_history(monkeypatch):
    calls = []
    hist = {'records': [{'date': '2026-11-01T00:00:00Z', 'data': {'indexerFlags': '0'}},
                        {'date': '2026-11-13T00:00:00Z', 'data': {'indexerFlags': '2'}}]}
    _requests(monkeypatch, {'/api/v3/history': _Resp(hist)}, calls)
    assert aw._grab_freeleech('Sonarr', {'release': {}}, 'HASH') == 'partial'
    assert calls[0][1]['downloadId'] == 'HASH' and calls[0][1]['eventType'] == 1


def test_grab_freeleech_history_without_flags_is_no_and_errors_are_unknown(monkeypatch):
    _requests(monkeypatch, {'/api/v3/history': _Resp({'records': [{'date': 'x', 'data': {}}]})}, [])
    assert aw._grab_freeleech('Radarr', {}, 'H') == 'no'
    _requests(monkeypatch, {'/api/v3/history': _Resp({'records': []})}, [])
    assert aw._grab_freeleech('Radarr', {}, 'H') is None
    _requests(monkeypatch, {'/api/v3/history': RuntimeError('down')}, [])
    assert aw._grab_freeleech('Radarr', {}, 'H') is None


def test_existing_file_facts_radarr_picks_largest_file(monkeypatch):
    files = [{'size': 4 * GB, 'quality': {'quality': {'name': 'WEBDL-1080p'}}, 'releaseGroup': 'SMALL'},
             {'size': 20 * GB, 'quality': {'quality': {'name': 'Bluray-2160p'}}, 'releaseGroup': 'BIG',
              'mediaInfo': {'videoDynamicRangeType': 'DV HDR10', 'audioCodec': 'TrueHD Atmos', 'videoCodec': 'x265'}}]
    calls = []
    _requests(monkeypatch, {'/api/v3/moviefile': _Resp(files)}, calls)
    out = aw._existing_file_facts('Radarr', {'movie': {'id': 7}})
    assert out == {'existing_quality': 'Bluray-2160p', 'existing_size_gb': 20.0, 'existing_group': 'BIG',
                   'existing_hdr': 'DV HDR10', 'existing_audio': 'TrueHD Atmos', 'existing_codec': 'x265'}
    assert calls[0][1] == {'movieId': 7}


def test_existing_file_facts_sonarr_follows_episode_file(monkeypatch):
    ep_file = {'size': int(2.5 * GB), 'quality': {'quality': {'name': 'WEBDL-1080p'}}, 'releaseGroup': 'G',
               'mediaInfo': {}}
    _requests(monkeypatch, {'/api/v3/episode/11': _Resp({'episodeFileId': 99}),
                            '/api/v3/episodefile/99': _Resp(ep_file)}, [])
    out = aw._existing_file_facts('Sonarr', {'episodes': [{'id': 11}]})
    assert out['existing_quality'] == 'WEBDL-1080p' and out['existing_size_gb'] == 2.5
    assert out['existing_hdr'] is None and out['existing_audio'] is None and out['existing_codec'] is None


def test_existing_file_facts_sonarr_pack_totals_distinct_files(monkeypatch):
    eps = [{'id': 11}, {'id': 12}, {'id': 13}, {'id': 14}]
    series_eps = [{'id': 11, 'episodeFileId': 101}, {'id': 12, 'episodeFileId': 102},
                  {'id': 13, 'episodeFileId': 102}, {'id': 14, 'episodeFileId': None}, {'id': 99, 'episodeFileId': 199}]
    files = [{'id': 101, 'size': 1 * GB, 'quality': {'quality': {'name': 'HDTV-720p'}}, 'releaseGroup': 'A'},
             {'id': 102, 'size': 2 * GB, 'quality': {'quality': {'name': 'WEBDL-1080p'}}, 'releaseGroup': 'B',
              'mediaInfo': {'videoCodec': 'h264'}},
             {'id': 199, 'size': 50 * GB}]
    calls = []
    _requests(monkeypatch, {'/api/v3/episodefile': _Resp(files), '/api/v3/episode': _Resp(series_eps)}, calls)
    out = aw._existing_file_facts('Sonarr', {'series': {'id': 5}, 'episodes': eps})
    assert out['existing_size_gb'] == 3.0  # 101 + 102 once; 199 is another season's file
    assert out['existing_quality'] == 'WEBDL-1080p' and out['existing_group'] == 'B' and out['existing_codec'] == 'h264'
    assert out['episodes'] == 4 and out['episodes_with_file'] == 3
    assert [c[1] for c in calls] == [{'seriesId': 5}, {'seriesId': 5}]


def test_existing_file_facts_sonarr_pack_without_files_is_empty(monkeypatch):
    calls = []
    _requests(monkeypatch, {'/api/v3/episode': _Resp([{'id': 11, 'episodeFileId': 0}, {'id': 12}])}, calls)
    assert aw._existing_file_facts('Sonarr', {'series': {'id': 5}, 'episodes': [{'id': 11}, {'id': 12}]}) == {}
    assert len(calls) == 1
    _requests(monkeypatch, {'/api/v3/episode': RuntimeError('down')}, [])
    assert aw._existing_file_facts('Sonarr', {'series': {'id': 5}, 'episodes': [{'id': 11}, {'id': 12}]}) == {}


def test_existing_file_facts_empty_on_missing_or_error(monkeypatch):
    _requests(monkeypatch, {'/api/v3/episode/11': _Resp({'episodeFileId': None})}, [])
    assert aw._existing_file_facts('Sonarr', {'episodes': [{'id': 11}]}) == {}
    assert aw._existing_file_facts('Sonarr', {'episodes': []}) == {}
    assert aw._existing_file_facts('Radarr', {'movie': {}}) == {}
    _requests(monkeypatch, {'/api/v3/moviefile': _Resp([])}, [])
    assert aw._existing_file_facts('Radarr', {'movie': {'id': 7}}) == {}
    _requests(monkeypatch, {'/api/v3/moviefile': RuntimeError('down')}, [])
    assert aw._existing_file_facts('Radarr', {'movie': {'id': 7}}) == {}


def _deluge(mp, answers):
    polls, sleeps = [], []
    seq = iter(answers)

    def post(url, json=None, timeout=None, **kw):
        polls.append((json or {}).get('params'))
        out = next(seq)
        if isinstance(out, Exception):
            raise out
        return _Resp({'result': out})
    mp.setattr(aw, 'deluge_login', lambda: None)
    mp.setattr(aw, 'session', type('S', (), {'post': staticmethod(post)})())
    mp.setattr(aw.time, 'sleep', lambda s: sleeps.append(s))
    return polls, sleeps


def test_grab_seeders_polls_until_tracker_reports(monkeypatch):
    polls, sleeps = _deluge(monkeypatch, [{'total_seeds': -1}, {}, {'total_seeds': 0}])
    assert aw._grab_seeders('H', polls=4, poll_s=2.0) == 0
    assert len(polls) == 3 and sleeps == [2.0, 2.0]
    assert polls[0] == ['H', ['total_seeds']]


def test_grab_seeders_unknown_is_none_never_zero(monkeypatch):
    _deluge(monkeypatch, [{'total_seeds': -1}] * 3)
    assert aw._grab_seeders('H', polls=3, poll_s=0) is None
    _deluge(monkeypatch, [{'total_seeds': True}])
    assert aw._grab_seeders('H', polls=1, poll_s=0) is None
    _deluge(monkeypatch, [RuntimeError('deluge down')])
    assert aw._grab_seeders('H', polls=3, poll_s=0) is None


def test_llm_mode_values(monkeypatch):
    for raw, want in (('shadow', 'shadow'), (' Enforce ', 'enforce'), ('off', 'off'), ('bogus', 'off'), ('', 'off')):
        monkeypatch.setenv('LLM_DECISIONS', raw)
        assert aw._llm_mode() == want
    monkeypatch.delenv('LLM_DECISIONS')
    assert aw._llm_mode() == 'off'


def _grab_world(mp, mode):
    seen, extra_calls = [], []
    if mode is None:
        mp.delenv('LLM_DECISIONS', raising=False)
    else:
        mp.setenv('LLM_DECISIONS', mode)
    mp.setattr(aw, 'is_upgrade_radarr', lambda data: 'upgrade')
    mp.setattr(aw, 'deluge_login', lambda: None)
    mp.setattr(aw, 'session', type('S', (), {'post': staticmethod(lambda *a, **k: _Resp({'result': None}))})())
    mp.setattr(aw, 'ensure_label_exists_named', lambda label: None)
    mp.setattr(aw, 'set_torrent_label', lambda h, label: None)
    mp.setattr(aw, 'dedupe_grabbed_release', lambda source, h: None)
    mp.setattr(aw, 'record_activity', lambda *a, **k: None)
    mp.setattr(aw.time, 'sleep', lambda s: None)

    def extra(source, data, download_id):
        extra_calls.append(download_id)
        return {'existing_quality': 'WEBDL-1080p', 'freeleech': 'full', 'seeders': 12}
    mp.setattr(aw, '_upgrade_gate_extra', extra)
    mp.setattr(aw.llm_advisor, 'advise', lambda kind, facts, rules_action, **kw: seen.append(facts) or rules_action)
    return seen, extra_calls


def _grab():
    return {'downloadId': 'ABCDEF', 'movie': {'id': 7, 'title': 'Some Movie', 'year': 2021},
            'release': {'releaseTitle': 'Some.Movie.2021.2160p.WEB-DL.x265-GRP', 'size': 12 * GB,
                        'indexer': 'idx', 'releaseGroup': 'GRP', 'quality': 'WEBDL-2160p'}}


def test_handle_grab_adds_extra_facts_only_when_advisor_is_on(monkeypatch):
    seen, extra_calls = _grab_world(monkeypatch, None)
    aw.handle_grab(_grab(), 'Radarr')
    assert extra_calls == [] and 'freeleech' not in seen[0]
    for mode in ('shadow', 'enforce'):
        seen, extra_calls = _grab_world(monkeypatch, mode)
        aw.handle_grab(_grab(), 'Radarr')
        assert extra_calls == ['abcdef'] or extra_calls == ['ABCDEF']
        assert seen[0]['freeleech'] == 'full' and seen[0]['seeders'] == 12
        assert seen[0]['existing_quality'] == 'WEBDL-1080p'


def test_grab_total_size_polls_until_metadata_arrives(monkeypatch):
    polls, sleeps = _deluge(monkeypatch, [{'total_size': 0}, {}, {'total_size': 12 * GB}])
    assert aw._grab_total_size_gb('H', polls=4, poll_s=2.0) == 12.0
    assert len(polls) == 3 and sleeps == [2.0, 2.0]
    assert polls[0] == ['H', ['total_size']]


def test_grab_total_size_unknown_is_none_never_zero(monkeypatch):
    _deluge(monkeypatch, [{'total_size': 0}] * 3)
    assert aw._grab_total_size_gb('H', polls=3, poll_s=0) is None
    _deluge(monkeypatch, [{'total_size': True}])
    assert aw._grab_total_size_gb('H', polls=1, poll_s=0) is None
    _deluge(monkeypatch, [{'total_size': -5}])
    assert aw._grab_total_size_gb('H', polls=1, poll_s=0) is None
    _deluge(monkeypatch, [RuntimeError('deluge down')])
    assert aw._grab_total_size_gb('H', polls=3, poll_s=0) is None
