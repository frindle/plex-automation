"""POST /tiering/torrent-gate and tiering_gate: free / release / wait for glacier-bound files.
Deluge is mocked; remove_torrent is replaced so no real removal can happen."""
import pytest

import tiering_gate

aw = __import__('arr-webhook')

DAY = 86400
SEEDED = aw.SEED_DAYS * DAY + 1


def tor(name, save_path, seeding_time=SEEDED, progress=100.0, state='Seeding'):
    return {'name': name, 'save_path': save_path, 'seeding_time': seeding_time,
            'progress': progress, 'state': state}


TORRENTS = {
    'aaaaaaaa11': tor('Show.S01.1080p', '/data/Media/TV Shows/Show'),                      # multi-file, seed met
    'bbbbbbbb22': tor('Movie.2020.mkv', '/data/Media/Movies', seeding_time=3 * DAY),       # single-file, too young
    'cccccccc33': tor('Pack', '/data/Media/Other', progress=40.0),                         # incomplete
    'dddddddd44': tor('Odd', '/data/Media/Odd', seeding_time=None),                        # seed time unknown
    'eeeeeeee55': {'name': None, 'save_path': None},                                       # junk row
}


@pytest.fixture
def client(monkeypatch):
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    calls = {'rpc': [], 'removed': []}
    box = {'torrents': dict(TORRENTS), 'refuse': set(), 'boom': set()}
    monkeypatch.setattr(aw, 'deluge_login', lambda: calls['rpc'].append('login'))

    def rpc(method, params):
        calls['rpc'].append(method)
        return box['torrents']

    def fake_remove(h, remove_data=True, info=None):
        assert remove_data is False, 'glacier must keep the data'
        if h in box['boom']:
            raise RuntimeError('deluge exploded')
        if h in box['refuse']:
            return aw.DEFERRED
        calls['removed'].append(h)
        return True

    monkeypatch.setattr(aw, '_deluge_rpc', rpc)
    monkeypatch.setattr(aw, 'remove_torrent', fake_remove)
    return aw.app.test_client(), calls, box


def post(c, paths, query=''):
    return c.post('/tiering/torrent-gate' + query, data='\n'.join(paths), content_type='text/plain')


P_SHOW = '/data/Media/TV Shows/Show/Show.S01.1080p/ep1.mkv'
P_FREE = '/data/Media/Movies/Unowned (2019)/u.mkv'
P_MOVIE = '/data/Media/Movies/Movie.2020.mkv'


def by_path(resp):
    return {r['path']: r for r in resp.get_json()['paths']}


def test_dry_run_classifies_and_removes_nothing(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_FREE, P_MOVIE])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['dry_run'] is True and body['counts'] == {'release': 1, 'free': 1, 'wait': 1}
    rows = by_path(resp)
    assert rows[P_SHOW]['status'] == 'release'
    assert rows[P_FREE]['status'] == 'free'
    assert rows[P_MOVIE]['status'] == 'wait' and '3.0d of 21d' in rows[P_MOVIE]['reason']
    assert calls['removed'] == []


def test_apply_removes_released_torrent_once_and_keeps_data(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, '/data/Media/TV Shows/Show/Show.S01.1080p/ep2.mkv', P_FREE], '?apply=1')
    assert resp.status_code == 200
    assert calls['removed'] == ['aaaaaaaa11']          # two files, one torrent, one removal
    assert resp.get_json()['counts'] == {'release': 2, 'free': 1}


def test_apply_never_removes_a_waiting_torrent(client):
    c, calls, box = client
    post(c, [P_MOVIE], '?apply=1')
    assert calls['removed'] == []


def test_incomplete_and_unknown_seed_time_wait(client):
    c, calls, box = client
    rows = by_path(post(c, ['/data/Media/Other/Pack/a.mkv', '/data/Media/Odd/Odd/a.mkv']))
    assert rows['/data/Media/Other/Pack/a.mkv']['reason'].endswith('torrent not complete')
    assert rows['/data/Media/Odd/Odd/a.mkv']['reason'].endswith('seed time unknown')
    assert all(r['status'] == 'wait' for r in rows.values())


def test_remove_torrent_refusal_turns_the_file_into_wait(client):
    c, calls, box = client
    box['refuse'].add('aaaaaaaa11')
    rows = by_path(post(c, [P_SHOW], '?apply=1'))
    assert rows[P_SHOW]['status'] == 'wait' and 'deferred' in rows[P_SHOW]['reason']
    assert calls['removed'] == []


def test_remove_torrent_exception_turns_the_file_into_wait(client):
    c, calls, box = client
    box['boom'].add('aaaaaaaa11')
    rows = by_path(post(c, [P_SHOW, P_FREE], '?apply=1'))
    assert rows[P_SHOW]['status'] == 'wait' and 'removal error' in rows[P_SHOW]['reason']
    assert rows[P_FREE]['status'] == 'free'


def test_two_torrents_on_one_file_need_both_released(client):
    c, calls, box = client
    box['torrents']['ffffffff66'] = tor('Show.S01.1080p', '/data/Media/TV Shows/Show', seeding_time=DAY)
    rows = by_path(post(c, [P_SHOW], '?apply=1'))
    assert rows[P_SHOW]['status'] == 'wait'
    assert calls['removed'] == []


def test_sibling_folder_with_same_prefix_is_not_owned(client):
    c, calls, box = client
    rows = by_path(post(c, ['/data/Media/TV Shows/Show/Show.S01.1080p.extra/x.mkv']))
    assert list(rows.values())[0]['status'] == 'free'


def test_tsv_format_for_the_bash_mover(client):
    c, calls, box = client
    resp = post(c, [P_SHOW, P_FREE, P_MOVIE], '?format=tsv')
    assert resp.mimetype == 'text/plain'
    lines = resp.get_data(as_text=True).splitlines()
    assert lines[0] == f'release\tseed rule met\t{P_SHOW}'
    assert lines[1] == f'free\tno torrent\t{P_FREE}'
    assert lines[2].startswith('wait\t') and lines[2].endswith('\t' + P_MOVIE)


def test_json_body_and_bad_bodies(client):
    c, calls, box = client
    resp = c.post('/tiering/torrent-gate', json={'paths': [P_FREE]})
    assert resp.get_json()['counts'] == {'free': 1}
    assert post(c, []).status_code == 400
    assert post(c, ['relative/path.mkv']).status_code == 400
    assert c.post('/tiering/torrent-gate', data='{nope').status_code == 400
    assert c.get('/tiering/torrent-gate').status_code == 405


def test_deluge_down_answers_503_and_removes_nothing(client, monkeypatch):
    c, calls, box = client

    def down(method, params):
        raise ConnectionError('deluge down')
    monkeypatch.setattr(aw, '_deluge_rpc', down)
    resp = post(c, [P_SHOW], '?apply=1')
    assert resp.status_code == 503
    assert calls['removed'] == []


def test_deluge_garbage_answers_503(client):
    c, calls, box = client
    box['torrents'] = None
    assert post(c, [P_SHOW]).status_code == 503


def test_seed_threshold_boundary():
    assert tiering_gate.seed_state({'seeding_time': 21 * DAY, 'progress': 100}, 21)[0] is True
    assert tiering_gate.seed_state({'seeding_time': 21 * DAY - 1, 'progress': 100}, 21)[0] is False


def test_endpoint_uses_the_same_seed_days_as_remove_torrent():
    import inspect
    src = inspect.getsource(aw.tiering_torrent_gate_route)
    assert 'SEED_DAYS' in src and 'remove_torrent(h, False' in src
