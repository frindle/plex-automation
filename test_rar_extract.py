"""rar_extract: grouping, the RAR-volume check, lsar listing, set deletion,
extraction (volumes kept on any failure) and queue dedupe.
Run: python test_rar_extract.py"""
import json
import os
import subprocess
import tempfile
import types

import rar_extract as rx


class _R:
    def __init__(self, rc=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_rar_sets_groups_both_styles():
    d = '/m/tv/Show/Season 1'
    paths = [f'{d}/a.part01.rar', f'{d}/a.part02.rar', f'{d}/b.rar', f'{d}/b.r00', f'{d}/b.r01',
             f'{d}/b.sfv', f'{d}/c.part02.rar', f'{d}/d.r00', f'{d}/x.mkv']
    sets = {s['first']: s for s in rx.rar_sets(paths)}
    assert set(sets) == {f'{d}/a.part01.rar', f'{d}/b.rar'}, sets
    assert sets[f'{d}/b.rar']['volumes'] == sorted([f'{d}/b.rar', f'{d}/b.r00', f'{d}/b.r01'])
    assert sets[f'{d}/b.rar']['extras'] == [f'{d}/b.sfv']


def test_has_rar_volumes():
    assert rx.has_rar_volumes(['x/a.part01.rar'])
    assert rx.has_rar_volumes(['x/a.r05'])
    assert rx.has_rar_volumes(['x/A.RAR'])
    assert not rx.has_rar_volumes(['x/a.mkv', 'x/a.nfo', 'x/a.srt'])
    assert not rx.has_rar_volumes([])


def test_list_contents_parses_and_skips_dirs():
    out = json.dumps({'lsarContents': [{'XADFileName': 'Show.S01E01.mkv'},
                                       {'XADFileName': 'Subs', 'XADIsDirectory': 1},
                                       {'XADFileName': 'Subs/en.srt'}]})
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _R(0, out)
    r = rx.list_contents('/m/a.rar', run=run)
    assert r == {'ok': True, 'files': ['Show.S01E01.mkv', 'Subs/en.srt']}, r
    assert seen[0][-1] == '/m/a.rar' and '-j' in seen[0]


def test_list_contents_errors():
    assert not rx.list_contents('/m/a.rar', run=lambda c, **k: _R(1, '', 'bad'))['ok']
    assert not rx.list_contents('/m/a.rar', run=lambda c, **k: _R(0, 'not json'))['ok']
    assert not rx.list_contents('/m/a.rar', run=lambda c, **k: _R(0, '{}'))['ok']

    def boom(c, **k):
        raise subprocess.TimeoutExpired(c, 300)
    assert not rx.list_contents('/m/a.rar', run=boom)['ok']


def test_delete_set_first_volume_last_and_missing_ok():
    order = []

    def remove(p):
        order.append(p)
        if p.endswith('r00'):
            raise FileNotFoundError(p)
    rs = {'first': '/m/b.rar', 'volumes': ['/m/b.r00', '/m/b.r01', '/m/b.rar'], 'extras': ['/m/b.sfv']}
    r = rx.delete_set(rs, remove=remove)
    assert r['ok'] and order[-1] == '/m/b.rar', (r, order)
    assert '/m/b.r00' not in r['removed'] and len(r['removed']) == 3


def test_delete_set_stops_on_error():
    def remove(p):
        if p.endswith('r01'):
            raise PermissionError('ro')
    rs = {'first': '/m/b.rar', 'volumes': ['/m/b.r00', '/m/b.r01', '/m/b.rar'], 'extras': []}
    r = rx.delete_set(rs, remove=remove)
    assert not r['ok'] and r['removed'] == ['/m/b.r00'] and 'r01' in r['error'], r


def _mkset(d):
    vols = [os.path.join(d, n) for n in ('s.rar', 's.r00')]
    for v in vols:
        with open(v, 'wb') as f:
            f.write(b'x' * 10)
    sfv = os.path.join(d, 's.sfv')
    open(sfv, 'w').close()
    return {'first': vols[0], 'volumes': vols, 'extras': [sfv]}


def _fake_unar(name, body=b'video'):
    def run(cmd, **kw):
        out = cmd[cmd.index('-output-directory') + 1]
        with open(os.path.join(out, name), 'wb') as f:
            f.write(body)
        return _R(0)
    return run


def test_extract_moves_file_and_deletes_volumes():
    with tempfile.TemporaryDirectory() as d:
        rs = _mkset(d)
        r = rx.extract(rs, run=_fake_unar('Show.S01E01.mkv'))
        assert r['ok'], r
        assert os.listdir(d) == ['Show.S01E01.mkv'], os.listdir(d)


def test_extract_failure_keeps_volumes():
    with tempfile.TemporaryDirectory() as d:
        rs = _mkset(d)
        r = rx.extract(rs, run=lambda c, **k: _R(1, '', 'crc error'))
        assert not r['ok'] and 'crc' in r['error']
        assert sorted(os.listdir(d)) == ['s.r00', 's.rar', 's.sfv'], os.listdir(d)


def test_extract_target_exists_keeps_everything():
    with tempfile.TemporaryDirectory() as d:
        rs = _mkset(d)
        with open(os.path.join(d, 'Show.S01E01.mkv'), 'wb') as f:
            f.write(b'old')
        r = rx.extract(rs, run=_fake_unar('Show.S01E01.mkv', b'new'))
        assert not r['ok'] and 'target exists' in r['error']
        assert sorted(os.listdir(d)) == ['Show.S01E01.mkv', 's.r00', 's.rar', 's.sfv']
        with open(os.path.join(d, 'Show.S01E01.mkv'), 'rb') as f:
            assert f.read() == b'old'


def test_extract_not_enough_space():
    class U:
        free = 5
    with tempfile.TemporaryDirectory() as d:
        rs = _mkset(d)
        r = rx.extract(rs, run=_fake_unar('x.mkv'), disk_usage=lambda p: U)
        assert not r['ok'] and 'free space' in r['error']
        assert len(os.listdir(d)) == 3


def test_enqueue_dedupes():
    q = []
    rs = {'first': '/m/a.rar', 'volumes': ['/m/a.rar'], 'extras': []}
    assert rx.enqueue(q, rs, 'scan', 1)
    assert not rx.enqueue(q, dict(rs), 'removal', 2)
    assert len(q) == 1 and q[0]['attempts'] == 0 and q[0]['source'] == 'scan'


def test_queue_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'q.json')
        assert rx.load_queue(p) == []
        rx.save_queue(p, [{'first': 'a'}])
        assert rx.load_queue(p) == [{'first': 'a'}]


def test_extract_only_uses_indexes_for_glob_names():
    """unar treats file arguments as wildcards; names with [ ] go by lsar index."""
    d = tempfile.mkdtemp()
    first = os.path.join(d, 'pack.rar')
    open(first, 'wb').write(b'x' * 10)
    name = '[Grp] Show S01E02 [1080p].mkv'
    listing = json.dumps({'lsarContents': [{'XADFileName': 'Show S01E01.mkv', 'XADIndex': 0},
                                           {'XADFileName': name, 'XADIndex': 4}]})
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if '-j' in cmd:
            return _R(0, listing)
        out = cmd[cmd.index('-output-directory') + 1]
        open(os.path.join(out, name), 'wb').write(b'video')
        return _R(0, '')
    r = rx.extract({'first': first, 'volumes': [first]}, run=run, keep_volumes=True, only=[name],
                   disk_usage=lambda p: types.SimpleNamespace(free=10**12))
    assert r['ok'], r
    unar = seen[-1]
    assert '-indexes' in unar and unar[-1] == '4' and name not in unar, unar
    assert os.path.isfile(os.path.join(d, name))
    seen.clear()
    r = rx.extract({'first': first, 'volumes': [first]}, run=run, keep_volumes=True, only=['missing.mkv'],
                   disk_usage=lambda p: types.SimpleNamespace(free=10**12))
    assert not r['ok'] and 'not in the archive' in r['error'], r
    assert all('-j' in c for c in seen), 'no unar call when a name is missing'


if __name__ == '__main__':
    for n, f in sorted(globals().items()):
        if n.startswith('test_') and callable(f):
            f()
            print('ok', n)
