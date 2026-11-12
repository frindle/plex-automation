"""POST /tiering/torrent-gate, /torrent-pause, /torrent-resume and tiering_gate:
free / pause / wait for glacier-bound files, pause-around-move, never remove.
Deluge is mocked; remove_torrent is replaced so any removal attempt fails the test."""
import pytest

import tiering_gate

aw = __import__('arr-webhook')


def tor(name, save_path, progress=100.0, state='Seeding'):
    return {'name': name, 'save_path': save_path, 'progress': progress, 'state': state}


TORRENTS = {
    'aaaaaaaa11': tor('Show.S01.1080p', '/data/Media/TV Shows/Show'),                      # multi-file, seeding
    'bbbbbbbb22': tor('Movie.2020.mkv', '/data/Media/Movies', state='Paused'),             # single-file, paused by user
    'cccccccc33': tor('Pack', '/data/Media/Other', progress=40.0),                         # incomplete
    'dddddddd44': tor('Odd', '/data/Media/Odd', state=None),                               # state unknown
    'eeeeeeee55': {'name': None, 'save_path': None},                                       # junk row
    'gggggggg77': tor('Err', '/data/Media/Err', state='Error'),                            # erroring
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    calls = {'rpc': [], 'paused': [], 'resumed': []}
    box = {'torrents': {k: dict(v) for k, v in TORRENTS.items()}, 'pause_fail': set(), 'resume_fail': set()}
    monkeypatch.setattr(aw, 'TIERING_PAUSE_STATE_PATH', str(tmp_path / 'tiering_paused.json'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: calls['rpc'].append('login'))

    def rpc(method, params):
        calls['rpc'].append(method)
        return box['torrents']

    def pause(h):
        if h in box['pause_fail']:
            raise RuntimeError('deluge exploded')
        calls['paused'].append(h)
        box['torrents'][h]['state'] = 'Paused'

    def resume(h):
        if h in box['resume_fail']:
            raise RuntimeError('deluge exploded')
        calls['resumed'].append(h)
        box['torrents'][h]['state'] = 'Seeding'

    def no_remove(*a, **k):
        raise AssertionError('tiering must never remove a torrent')

    monkeypatch.setattr(aw, '_deluge_rpc', rpc)
    monkeypatch.setattr(aw, 'deluge_pause_torrent', pause)
    monkeypatch.setattr(aw, 'deluge_resume_torrent', resume)
    monkeypatch.setattr(aw, 'remove_torrent', no_remove)
    return aw.app.test_client(), calls, box


def post(c, paths, query='', route='torrent-gate'):
    return c.post(f'/tiering/{route}' + query, data='\n'.join(paths), content_type='text/plain')


def tsv(resp):
    return [ln.split('\t') for ln in resp.get_data(as_text=True).splitlines()]


P_SHOW = '/data/Media/TV Shows/Show/Show.S01.1080p/ep1.mkv'
P_SHOW2 = '/data/Media/TV Shows/Show/Show.S01.1080p/ep2.mkv'
P_FREE = '/data/Media/Movies/Unowned (2019)/u.mkv'
P_MOVIE = '/data/Media/Movies/Movie.2020.mkv'     # owner paused by the user
P_PACK = '/data/Media/Other/Pack/a.mkv'


def by_path(resp):
    return {r['path']: r for r in resp.get_json()['paths']}


def test_gate_classifies_with_no_side_effects(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_FREE, P_MOVIE, P_PACK])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['counts'] == {'pause': 2, 'free': 1, 'wait': 1}
    rows = by_path(resp)
    assert rows[P_SHOW]['status'] == 'pause'
    assert rows[P_FREE]['status'] == 'free'
    assert rows[P_MOVIE]['status'] == 'pause'            # paused by user is still movable
    assert rows[P_PACK]['status'] == 'wait' and 'not complete' in rows[P_PACK]['reason']
    assert calls['paused'] == [] and calls['resumed'] == []


def test_gate_ignores_seed_time_and_apply_flag(client):
    c, calls, box = client
    box['torrents']['aaaaaaaa11']['seeding_time'] = 5     # young torrent: no longer a blocker
    resp = post(c, [P_SHOW], '?apply=1')
    assert by_path(resp)[P_SHOW]['status'] == 'pause'
    assert calls['paused'] == []                          # apply=1 no longer does anything


def test_incomplete_error_and_unknown_state_wait(client):
    c, calls, box = client
    rows = by_path(post(c, [P_PACK, '/data/Media/Odd/Odd/a.mkv', '/data/Media/Err/Err/a.mkv']))
    assert rows[P_PACK]['reason'].endswith('torrent not complete')
    assert rows['/data/Media/Odd/Odd/a.mkv']['reason'].endswith('torrent state unknown')
    assert rows['/data/Media/Err/Err/a.mkv']['reason'].endswith('torrent state Error')
    assert all(r['status'] == 'wait' for r in rows.values())


def test_two_owners_one_waiting_waits(client):
    c, calls, box = client
    box['torrents']['ffffffff66'] = tor('Show.S01.1080p', '/data/Media/TV Shows/Show', progress=50.0)
    assert by_path(post(c, [P_SHOW]))[P_SHOW]['status'] == 'wait'


def test_sibling_folder_with_same_prefix_is_not_owned(client):
    c, calls, box = client
    rows = by_path(post(c, ['/data/Media/TV Shows/Show/Show.S01.1080p.extra/x.mkv']))
    assert list(rows.values())[0]['status'] == 'free'


def test_tsv_format_for_the_bash_mover(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_FREE, P_PACK], '?format=tsv')
    assert resp.mimetype == 'text/plain'
    lines = resp.get_data(as_text=True).splitlines()
    assert lines[0] == f'pause\tcomplete, pause around move\t{P_SHOW}'
    assert lines[1] == f'free\tno torrent\t{P_FREE}'
    assert lines[2].startswith('wait\t') and lines[2].endswith('\t' + P_PACK)


def test_json_body_and_bad_bodies(client):
    c, calls, box = client
    resp = c.post('/tiering/torrent-gate', json={'paths': [P_FREE]})
    assert resp.get_json()['counts'] == {'free': 1}
    assert post(c, []).status_code == 400
    assert post(c, ['relative/path.mkv']).status_code == 400
    assert c.post('/tiering/torrent-gate', data='{nope').status_code == 400
    assert c.get('/tiering/torrent-gate').status_code == 405


def test_deluge_down_answers_503(client, monkeypatch):
    c, calls, box = client

    def down(method, params):
        raise ConnectionError('deluge down')
    monkeypatch.setattr(aw, '_deluge_rpc', down)
    assert post(c, [P_SHOW]).status_code == 503
    assert post(c, [P_SHOW], route='torrent-pause').status_code == 503
    assert calls['paused'] == []


def test_deluge_garbage_answers_503(client):
    c, calls, box = client
    box['torrents'] = None
    assert post(c, [P_SHOW]).status_code == 503


# ---- pause / resume ---------------------------------------------------------

def test_pause_pauses_owner_once_and_records_ledger(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_SHOW2], route='torrent-pause')
    assert resp.status_code == 200
    assert tsv(resp) == [['paused', 'aaaaaaaa11']]        # two files, one torrent, one pause
    assert calls['paused'] == ['aaaaaaaa11']
    assert 'aaaaaaaa11' in aw._tiering_ledger_load()


def test_pause_free_path_pauses_nothing(client):
    c, calls, box = client
    resp = post(c, [P_FREE], route='torrent-pause')
    assert resp.status_code == 200 and resp.get_data(as_text=True) == ''
    assert calls['paused'] == [] and aw._tiering_ledger_load() == {}


def test_already_paused_by_user_is_not_paused_or_resumed(client):
    c, calls, box = client
    resp = post(c, [P_MOVIE], route='torrent-pause')
    assert tsv(resp) == [['foreign', 'bbbbbbbb22']]
    assert calls['paused'] == [] and aw._tiering_ledger_load() == {}
    resp = post(c, ['bbbbbbbb22'], route='torrent-resume')
    assert tsv(resp) == [['ignored', 'bbbbbbbb22']]
    assert calls['resumed'] == []                          # never resume what the gate did not pause


def test_pause_refuses_when_any_path_waits(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_PACK], route='torrent-pause')
    assert resp.status_code == 409
    assert calls['paused'] == [] and aw._tiering_ledger_load() == {}


def test_pause_failure_rolls_back_and_answers_503(client):
    c, calls, box = client
    box['torrents']['hhhhhhhh88'] = tor('Show.S01.1080p', '/data/Media/TV Shows/Show')
    box['pause_fail'].add('hhhhhhhh88')
    resp = post(c, [P_SHOW], route='torrent-pause')
    assert resp.status_code == 503
    assert calls['paused'] == calls['resumed'] == ['aaaaaaaa11']   # the first was paused then put back
    assert aw._tiering_ledger_load() == {}


def test_resume_resumes_ledger_hashes_and_clears_them(client):
    c, calls, box = client
    post(c, [P_SHOW], route='torrent-pause')
    resp = post(c, ['aaaaaaaa11'], route='torrent-resume')
    assert resp.status_code == 200 and tsv(resp) == [['resumed', 'aaaaaaaa11']]
    assert calls['resumed'] == ['aaaaaaaa11'] and aw._tiering_ledger_load() == {}


def test_resume_failure_keeps_ledger_entry(client):
    c, calls, box = client
    post(c, [P_SHOW], route='torrent-pause')
    box['resume_fail'].add('aaaaaaaa11')
    resp = post(c, ['aaaaaaaa11'], route='torrent-resume')
    assert resp.status_code == 503 and tsv(resp) == [['failed', 'aaaaaaaa11']]
    assert 'aaaaaaaa11' in aw._tiering_ledger_load()


def test_resume_bad_bodies(client):
    c, calls, box = client
    assert post(c, [], route='torrent-resume').status_code == 400
    assert c.post('/tiering/torrent-resume', json={'hashes': ['zz']}).status_code == 200
    assert c.post('/tiering/torrent-resume', data='{nope').status_code == 400
    assert calls['resumed'] == []


def test_stale_ledger_entries_are_resumed_by_the_next_gate_call(client):
    c, calls, box = client
    post(c, [P_SHOW], route='torrent-pause')
    old = aw.time.time() - (aw.TIERING_PAUSE_MAX_MIN + 1) * 60
    aw._tiering_ledger_save({'aaaaaaaa11': old})
    post(c, [P_FREE])                                      # a plain gate call
    assert calls['resumed'] == ['aaaaaaaa11'] and aw._tiering_ledger_load() == {}


def test_fresh_ledger_entries_are_not_auto_resumed(client):
    c, calls, box = client
    post(c, [P_SHOW], route='torrent-pause')
    post(c, [P_FREE])
    post(c, [P_FREE], route='torrent-pause')
    assert calls['resumed'] == [] and 'aaaaaaaa11' in aw._tiering_ledger_load()


def test_stale_resume_failure_keeps_entry_and_does_not_break_the_call(client):
    c, calls, box = client
    aw._tiering_ledger_save({'aaaaaaaa11': 1})
    box['resume_fail'].add('aaaaaaaa11')
    assert post(c, [P_FREE]).status_code == 200
    assert 'aaaaaaaa11' in aw._tiering_ledger_load()


def test_pause_again_after_a_crash_reuses_the_ledger_entry(client):
    c, calls, box = client
    post(c, [P_SHOW], route='torrent-pause')               # mover crashed, no resume
    resp = post(c, [P_SHOW2], route='torrent-pause')       # same torrent, still within the limit
    assert tsv(resp) == [['paused', 'aaaaaaaa11']]         # still ours: the mover resumes it
    assert calls['paused'] == ['aaaaaaaa11']


def test_tiering_path_never_removes_a_torrent():
    import inspect
    src = ''.join(inspect.getsource(f) for f in (
        aw.tiering_torrent_gate_route, aw.tiering_torrent_pause_route, aw.tiering_torrent_resume_route,
        aw._tiering_resume_stale, aw._tiering_torrents))
    assert 'remove_torrent' not in src and 'apply_releases' not in src
    assert not hasattr(tiering_gate, 'apply_releases') and not hasattr(tiering_gate, 'seed_state')
    assert 'SEED_DAYS' not in src
