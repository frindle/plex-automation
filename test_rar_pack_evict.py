"""Season-pack RAR torrents seeding from the library: extract the missing
episodes (volumes kept), then label superseded and move to SEEDING_DIR.
Run: python test_rar_pack_evict.py"""
import importlib.util
import os
import tempfile
import types

import rar_extract

_spec = importlib.util.spec_from_file_location(
    'arr_webhook_rar_pack', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)
aw.DELUGE_PATH_MAP = [('/data/Media/TV Shows', '/media/tv'), ('/data/Downloads', '/data/Downloads')]
aw.SONARR_API_KEY = 'k'
aw.SONARR_URL = 'http://sonarr'

SP = '/data/Media/TV Shows/Show/Season 1'
INFO = {'name': 'Show.S01', 'label': 'sonarr', 'save_path': SP,
        'files': [{'path': 'Show.S01/show.s01.rar'}, {'path': 'Show.S01/show.s01.r00'},
                  {'path': 'Show.S01/show.s01.sfv'}]}
SERIES = [{'id': 1, 'title': 'Show', 'path': '/data/Media/TV Shows/Show'}]
INNER = ['Show.S01E01.mkv', 'Show.S01E02.mkv', 'Show.S01E03.mkv']


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def _requests(have):
    def get(url, params=None, headers=None, timeout=None):
        if url.endswith('/api/v3/series'):
            return _Resp(SERIES)
        if url.endswith('/api/v3/episode'):
            return _Resp([{'seasonNumber': 1, 'episodeNumber': e, 'hasFile': h} for e, h in have.items()])
        raise AssertionError(url)
    return types.SimpleNamespace(get=get)


def _ls(files):
    return lambda first: {'ok': True, 'files': files}


def _stat(dev_a, dev_b):
    def stat(p):
        return types.SimpleNamespace(st_dev=dev_b if 'Just4Seeding' in p else dev_a)
    return stat


_ORIG_EXTRACT = rar_extract.extract


def teardown_function(_f=None):
    rar_extract.extract = _ORIG_EXTRACT


def _wire(have=None, calls=None, extracted=None):
    calls = [] if calls is None else calls
    aw.requests = _requests(have or {1: True, 2: False, 3: False})
    aw.set_torrent_label = lambda h, lab: calls.append(('label', h, lab))
    aw.move_torrent_storage = lambda h, d: calls.append(('move', h, d)) or True
    aw._torrent_save_path = lambda h: aw.SEEDING_DIR
    aw.deluge_force_recheck = lambda h: calls.append(('recheck', h))
    aw._rescan_arr_for = lambda p: calls.append(('rescan',)) or 'Sonarr RescanSeries Show'
    aw._wait_rar_pack_settled = lambda h, **k: True
    aw.record_activity = lambda *a: None

    def fake_extract(rset, keep_volumes=False, only=None):
        calls.append(('extract', keep_volumes, tuple(only)))
        return {'ok': True, 'files': list(extracted or [])}
    rar_extract.extract = fake_extract
    return calls


def test_keep_volumes_leaves_volumes_and_extracts_only_requested():
    d = tempfile.mkdtemp()
    vols = [os.path.join(d, n) for n in ('a.rar', 'a.r00')]
    sfv = os.path.join(d, 'a.sfv')
    for p in vols + [sfv]:
        open(p, 'wb').write(b'x' * 10)
    seen = []

    def run(cmd, **k):
        seen.append(cmd)
        out = cmd[cmd.index('-output-directory') + 1]
        open(os.path.join(out, 'E02.mkv'), 'wb').write(b'video')
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')
    rs = {'first': vols[0], 'volumes': vols, 'extras': [sfv]}
    real = _real_extract()
    r = real(rs, run=run, keep_volumes=True, only=['E02.mkv'])
    assert r['ok'], r
    assert all(os.path.isfile(p) for p in vols + [sfv]), 'volumes must stay'
    assert seen[0][-1] == 'E02.mkv' and seen[0][-2] == vols[0]
    assert os.path.isfile(os.path.join(d, 'E02.mkv'))
    assert not real(rs, run=run, keep_volumes=True)['ok'], 'keep_volumes needs a file list'
    os.remove(os.path.join(d, 'E02.mkv'))
    r = real(rs, run=run)  # default behaviour: volumes deleted
    assert r['ok'] and not any(os.path.exists(p) for p in vols + [sfv])


_REAL = None


def _real_extract():
    global _REAL
    if _REAL is None:
        spec = importlib.util.spec_from_file_location(
            'rar_extract_real', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rar_extract.py'))
        _REAL = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_REAL)
    return _REAL.extract


def test_plan_extracts_only_missing_episodes():
    _wire()
    p = aw.plan_rar_pack('h', INFO, {}, _ls(INNER), _stat(1, 1))
    assert p['action'] == 'evict' and p['same_filesystem'] is True, p
    assert p['extract'][0]['files'] == ['Show.S01E02.mkv', 'Show.S01E03.mkv']
    assert p['episodes'] == 3


def test_refuses_non_library_non_rar_single_episode():
    _wire()
    assert aw.plan_rar_pack('h', dict(INFO, save_path='/data/Downloads/Just4Seeding'), {}, _ls(INNER))['action'] == 'refuse'
    no_rar = dict(INFO, files=[{'path': 'Show.S01/e01.mkv'}])
    assert aw.plan_rar_pack('h', no_rar, {}, _ls(INNER))['action'] == 'refuse'
    p = aw.plan_rar_pack('h', INFO, {}, _ls(INNER[:1]), _stat(1, 1))
    assert p['action'] == 'refuse' and 'season pack' in p['reason'], p
    assert aw.plan_rar_pack('h', dict(INFO, label='superseded'), {}, _ls(INNER))['action'] == 'refuse'


def test_unknown_episode_refuses():
    _wire(have={1: True})
    p = aw.plan_rar_pack('h', INFO, {}, _ls(INNER), _stat(1, 1))
    assert p['action'] == 'refuse' and 'not in Sonarr' in p['reason'], p


def test_different_filesystem_refuses_apply():
    calls = _wire()
    r = aw.evict_library_rar_pack('h', dry_run=False, info=INFO, contents=_ls(INNER), stat=_stat(1, 2))
    assert not r['ok'] and 'filesystem' in r['error'], r
    assert calls == [], calls


def test_dry_run_changes_nothing():
    calls = _wire()
    r = aw.evict_library_rar_pack('h', dry_run=True, info=INFO, contents=_ls(INNER), stat=_stat(1, 1))
    assert r['ok'] and r['dry_run'] and r['plan']['action'] == 'evict'
    assert calls == [], calls


def test_apply_runs_steps_in_order():
    d = tempfile.mkdtemp()
    f = os.path.join(d, 'Show.S01E02.mkv')
    open(f, 'wb').write(b'x')
    calls = _wire(extracted=[f])
    r = aw.evict_library_rar_pack('h', dry_run=False, info=INFO, contents=_ls(INNER), stat=_stat(1, 1),
                                  sleep=lambda s: None)
    assert r['ok'], r
    kinds = [c[0] for c in calls]
    assert kinds == ['extract', 'rescan', 'label', 'move', 'recheck', 'rescan'], kinds
    assert calls[0][1] is True and calls[2] == ('label', 'h', 'superseded')
    assert calls[3] == ('move', 'h', aw.SEEDING_DIR)


def test_extracted_file_missing_blocks_move():
    calls = _wire(extracted=['/nonexistent/Show.S01E02.mkv'])
    r = aw.evict_library_rar_pack('h', dry_run=False, info=INFO, contents=_ls(INNER), stat=_stat(1, 1))
    assert not r['ok']
    assert not any(c[0] in ('label', 'move') for c in calls), calls


def test_route_allowlisted():
    assert '/rar-pack-scan' in aw._DESTRUCTIVE_GET_GUARD_PATHS


if __name__ == '__main__':
    for n, f in list(globals().items()):
        if n.startswith('test_') and callable(f):
            try:
                f()
            finally:
                teardown_function()
            print('ok', n)
