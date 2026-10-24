"""Tests for _auto_rescue persistence, library-skip, downloadClientId, give-up."""

import os, sys, json, tempfile, importlib, requests

TMP = tempfile.mkdtemp()
ACT = os.path.join(TMP, 'activity.jsonl')
STATE = os.path.join(TMP, 'rescue.json')
os.environ['ACTIVITY_LOG'] = ACT
os.environ['RESCUE_STATE_PATH'] = STATE
os.environ['SONARR_API_KEY'] = 'test-key'
os.environ['RADARR_API_KEY'] = 'test-key'
aw = __import__('arr-webhook')

class Resp:
    def __init__(self, payload): self._p, self.ok = payload, True
    def json(self): return self._p
    def raise_for_status(self): pass

TORRENTS = {}
GETS, CMDS = [], []
IMPORTED = set()

def _session_post(self, url, json=None, timeout=None, **kw):
    m, p = json['method'], json.get('params')
    if m == 'auth.login':
        return Resp({'result': True})
    if m == 'core.get_torrents_status':
        return Resp({'result': {h: {f: TORRENTS[h][f] for f in p[1] if f in TORRENTS[h]} for h in TORRENTS}})
    return Resp({'result': None})
requests.Session.post = _session_post

def _get(url, headers=None, params=None, timeout=None, **kw):
    GETS.append(dict(params or {}))
    did = (params or {}).get('downloadId')
    if did is None:
        return Resp({'records': []})
    recs = [{'eventType': 'grabbed', 'date': '2026-10-01T00:00:00Z'}]
    if did.lower() in IMPORTED:
        recs.append({'eventType': 'downloadFolderImported', 'date': '2026-10-02T00:00:00Z'})
    return Resp({'records': recs})

def _post(url, headers=None, json=None, timeout=None, **kw):
    CMDS.append(json)
    return Resp({'id': 1})
requests.get = _get
requests.post = _post

def setup(torrents):
    TORRENTS.clear(); TORRENTS.update(torrents)
    GETS.clear(); CMDS.clear(); IMPORTED.clear()
    open(ACT, 'w').close()

def acts():
    with open(ACT) as f:
        return [json.loads(l)['category'] for l in f if l.strip()]

def saved():
    with open(STATE) as f:
        return json.load(f)

def reload_aw():
    global aw
    aw = importlib.reload(aw)
    return aw


def test_library_torrent_skipped():
    """Test 1: Library torrent is skipped — no requests at all."""
    setup({'lib1': {'name': 'Show.S01', 'label': 'sonarr', 'save_path': '/data/Media/TV Shows/Show/Season 01', 'progress': 100.0}})
    r = aw._auto_rescue('sonarr')
    assert CMDS == []
    assert GETS == []
    assert r['skipped']['library'] == 1
    assert r['triggered_count'] == 0


def test_library_movie_skipped():
    """Test 2: Library movie is skipped — no requests at all."""
    setup({'lib2': {'name': 'Dune.2021.mkv', 'label': 'radarr', 'save_path': '/data/Media/Movies/Dune (2021)', 'progress': 100.0}})
    r = aw._auto_rescue('radarr')
    assert CMDS == []
    assert GETS == []


def test_sonarr_command_has_downloadClientId():
    """Test 3: Sonarr command carries downloadClientId."""
    setup({'abc123def': {'name': 'Show.S01E01', 'label': 'sonarr', 'save_path': '/data/Downloads/Complete/sonarr', 'progress': 100.0}})
    r = aw._auto_rescue('sonarr')
    assert CMDS == [{'name': 'DownloadedEpisodesScan', 'path': '/data/Downloads/Complete/sonarr/Show.S01E01', 'importMode': 'auto', 'downloadClientId': 'ABC123DEF'}]
    assert acts() == ['import-rescue']
    assert saved()['attempts']['abc123def'] == 1


def test_radarr_command_has_downloadClientId():
    """Test 4: Radarr command carries downloadClientId."""
    setup({'fed987': {'name': 'Movie.2020.1080p', 'label': 'radarr', 'save_path': '/data/Downloads/Complete/radarr', 'progress': 100.0}})
    r = aw._auto_rescue('radarr')
    assert CMDS == [{'name': 'DownloadedMoviesScan', 'path': '/data/Downloads/Complete/radarr/Movie.2020.1080p', 'importMode': 'auto', 'downloadClientId': 'FED987'}]


def test_cap_give_up_once_persistence():
    """Test 5: Cap, give-up once, persistence across reload."""
    state_file = os.path.join(TMP, 'rescue.json')
    setup({'cap1': {'name': 'Cap.Show.S02E03', 'label': 'sonarr', 'save_path': '/data/Downloads/Complete/sonarr', 'progress': 100.0}})
    for i in range(6):
        r = aw._auto_rescue('sonarr')
    assert len(CMDS) == 6
    r = aw._auto_rescue('sonarr')
    assert len(CMDS) == 6
    assert acts().count('rescue-gave-up') == 1
    r = aw._auto_rescue('sonarr')
    assert len(CMDS) == 6
    assert acts().count('rescue-gave-up') == 1
    assert saved()['attempts']['cap1'] == 6
    assert 'cap1' in saved()['gave_up']
    reload_aw()
    assert aw._rescue_attempts['cap1'] == 6
    assert 'cap1' in aw._rescue_gave_up
    r = aw._auto_rescue('sonarr')
    assert len(CMDS) == 6
    assert acts().count('rescue-gave-up') == 1


def test_dry_run_writes_nothing():
    """Test 6: Dry run writes nothing to state file or activity."""
    state_file = os.path.join(TMP, 'rescue.json')
    setup({'dry1': {'name': 'Dry.S01E01', 'label': 'sonarr', 'save_path': '/data/Downloads/Complete/sonarr', 'progress': 100.0}})
    open(STATE, 'w').write('{"attempts": {}, "gave_up": []}')
    before = open(STATE).read()
    r = aw._auto_rescue('sonarr', dry_run=True)
    assert CMDS == []
    assert acts() == []
    assert open(STATE).read() == before
    assert r['stuck_count'] == 1


def test_already_imported_clears_counter():
    """Test 7: Already imported clears the counter and gave_up."""
    setup({'imp1': {'name': 'Imp.S01E01', 'label': 'sonarr', 'save_path': '/data/Downloads/Complete/sonarr', 'progress': 100.0}})
    r = aw._auto_rescue('sonarr')
    assert len(CMDS) == 1
    IMPORTED.add('imp1')
    CMDS.clear()
    r = aw._auto_rescue('sonarr')
    assert CMDS == []
    assert 'imp1' not in saved()['attempts']
    assert r['skipped']['already_imported'] == 1


def test_malformed_state_loads_empty():
    """Test 8: Malformed state file loads empty."""
    state_file = os.path.join(TMP, 'rescue.json')
    open(STATE, 'w').write('not json{')
    reload_aw()
    assert aw._rescue_attempts == {}
    assert aw._rescue_gave_up == set()


if __name__ == '__main__':
    results = []
    tests = [
        test_library_torrent_skipped,
        test_library_movie_skipped,
        test_sonarr_command_has_downloadClientId,
        test_radarr_command_has_downloadClientId,
        test_cap_give_up_once_persistence,
        test_dry_run_writes_nothing,
        test_already_imported_clears_counter,
        test_malformed_state_loads_empty,
    ]
    any_failed = False
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"OK {name}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            any_failed = True
    if any_failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL PASS")
