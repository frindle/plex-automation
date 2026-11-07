"""downloads_audit: report-only findings over a real temp Downloads tree
(real hardlinks, real mtimes). Run: python test_downloads_audit.py"""
import os
import tempfile
import unicodedata

import downloads_audit as da

NOW = 2_000_000_000
OLD = NOW - 3 * 86400


def _tree():
    root = tempfile.mkdtemp()
    dl = os.path.join(root, 'Downloads')
    lib = os.path.join(root, 'Media')
    os.makedirs(dl)
    os.makedirs(lib)

    def put(rel, size=10, base=dl, mtime=OLD):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(b'x' * size)
        os.utime(p, (mtime, mtime))
        return p

    def to_local(p):
        return dl + p[len('/data/Downloads'):] if p.startswith('/data/Downloads') else p
    return root, dl, lib, put, to_local


def T(name, files, label='sonarr', save_path='/data/Downloads/Complete', progress=100.0,
      completed_time=OLD, time_added=OLD - 3600):
    return {'name': name, 'label': label, 'save_path': save_path, 'progress': progress,
            'completed_time': completed_time, 'time_added': time_added,
            'files': [{'path': f} for f in files]}


def test_orphans():
    _, dl, _, put, to_local = _tree()
    put('Complete/Owned/a.mkv')
    put('Complete/Gone/b.mkv', size=1000)
    put('Complete/Gone/b.nfo', size=5)
    put('Complete/Fresh/c.mkv', mtime=NOW - 60)       # in-flight import/move: grace
    put('Complete/Owned/.DS_Store')
    put('Complete/.hidden/d.mkv')                       # dot dirs skipped
    put('Complete/Owned/.abcdef.parts')                 # libtorrent parts file
    put('loose.sh')
    nfd = unicodedata.normalize('NFD', 'Café')
    put(f'Complete/{nfd}/e.mkv')                        # disk NFD, Deluge NFC
    torrents = {'h1': T('Owned', ['Owned/a.mkv']), 'h2': T('Cafe', ['Café/e.mkv'])}
    got = sorted((os.path.relpath(o['path'], dl), o['size'], o['group'])
                 for o in da.find_orphans(torrents, dl, to_local, NOW))
    assert got == [('Complete/Gone/b.mkv', 1000, 'Complete/Gone'),
                   ('Complete/Gone/b.nfo', 5, 'Complete/Gone'),
                   ('loose.sh', 10, 'loose.sh')], got


def test_orphans_no_torrents_and_torrent_without_files():
    _, dl, _, put, to_local = _tree()
    put('Complete/X/x.mkv')
    t = {'h': {'name': 'n', 'save_path': '/data/Downloads/Complete', 'files': None}, 'z': None}
    assert [o['group'] for o in da.find_orphans(t, dl, to_local, NOW)] == ['Complete/X']


def test_single_link():
    _, dl, lib, put, to_local = _tree()
    a = put('Complete/Linked/a.mkv', size=100)
    os.link(a, os.path.join(lib, 'a.mkv'))
    put('Complete/Copy/b.mkv', size=200)
    put('Complete/Copy/b.nfo')
    put('Complete/Copy/Sample/b-sample.mkv')
    put('Complete/Up/c.MKV', size=300)
    put('Complete/LS/d.mp4', size=400)
    put('Complete/Sup/e.mkv')
    put('Complete/Part/f.mkv')
    put('Complete/New/g.mkv')
    put('Complete/Legacy/h.mkv', size=50)
    put('Complete/Un/i.mkv')
    torrents = {
        'linked': T('Linked', ['Linked/a.mkv']),
        'copy': T('Copy', ['Copy/b.mkv', 'Copy/b.nfo', 'Copy/Sample/b-sample.mkv']),
        'upg': T('Up', ['Up/c.MKV'], label='Radarr-Upgrade'),
        'ls': T('LS', ['LS/d.mp4'], label='library-seed'),
        'sup': T('Sup', ['Sup/e.mkv'], label='superseded'),
        'part': T('Part', ['Part/f.mkv'], progress=99.5),
        'new': T('New', ['New/g.mkv'], completed_time=NOW - 3600),
        'legacy': T('Legacy', ['Legacy/h.mkv'], completed_time=0),     # falls back to time_added
        'un': T('Un', ['Un/i.mkv'], label=''),
        'media': T('M', ['x.mkv'], label='library-seed', save_path='/data/Media/TV Shows/S'),
        'missing': T('Missing', ['Missing/nope.mkv']),
    }
    got = [(s['hash'], s['files'], s['size']) for s in da.find_single_link(torrents, to_local, NOW)]
    assert got == [('copy', 1, 200), ('legacy', 1, 50), ('ls', 1, 400), ('upg', 1, 300)], got


def test_unlabeled():
    t = {'b': {'name': 'B', 'label': None}, 'a': {'name': 'A', 'label': '  ', 'save_path': '/x'},
         'c': {'name': 'C', 'label': 'adult'}, 'd': None}
    assert [u['hash'] for u in da.find_unlabeled(t)] == ['a', 'b', 'd']


def test_summarize_format_and_limit():
    orphans = [{'path': '/p1', 'size': 2e9, 'group': 'Complete/G'},
               {'path': '/p2', 'size': 1e9, 'group': 'Complete/G'},
               {'path': '/p3', 'size': 5e9, 'group': 'Complete/H'}]
    single = [{'name': 'Show', 'label': 'sonarr', 'size': 1.25e9}]
    unl = [{'name': 'U%d' % i} for i in range(8)]
    lines = da.summarize(orphans, single, unl).split('\n')
    assert lines[0] == 'orphan files 3 (8.0 GB), single-link 1 torrents (1.2 GB), unlabeled 8', lines[0]
    assert lines[1:4] == ['- orphan: Complete/H (5.0 GB)', '- orphan: Complete/G (3.0 GB)',
                          '- single-link: Show [sonarr] (1.2 GB)'], lines
    assert lines[-1] == '... and 3 more' and len(lines) == 10, lines


def test_audit_notify_once_reannounce_and_dry_run():
    _, dl, _, put, to_local = _tree()
    p = put('Complete/Gone/b.mkv')
    torrents = {'u': T('Unl', [], label='')}
    sent = []

    def notify(title, msg):
        sent.append((title, msg))
    rep = set()
    dry = da.audit(torrents, dl, to_local, notify, rep, NOW, dry_run=True)
    assert sent == [] and rep == set() and dry['notified'] == [] and dry['counts']['orphan_files'] == 1
    r1 = da.audit(torrents, dl, to_local, notify, rep, NOW)
    assert len(sent) == 1 and sent[0][0] == 'Downloads audit: 2 new finding(s)', sent
    assert sorted(r1['notified']) == ['o:' + p, 'u:u'] and rep == {'o:' + p, 'u:u'}
    r2 = da.audit(torrents, dl, to_local, notify, rep, NOW)
    assert len(sent) == 1 and r2['notified'] == []
    os.remove(p)                                   # cleaned up -> dropped from reported
    da.audit(torrents, dl, to_local, notify, rep, NOW)
    assert rep == {'u:u'} and len(sent) == 1
    put('Complete/Gone/b.mkv')                     # comes back -> announced again
    r4 = da.audit(torrents, dl, to_local, notify, rep, NOW)
    assert r4['notified'] == ['o:' + p] and len(sent) == 2
    assert sent[1][1].split('\n')[0].startswith('orphan files 1 ('), sent[1]


def test_audit_never_writes():
    root, dl, lib, put, to_local = _tree()
    put('Complete/Gone/b.mkv')
    before = sorted(os.walk(root))
    da.audit({'u': T('U', [], label='')}, dl, to_local, lambda a, b: None, set(), NOW)
    assert sorted(os.walk(root)) == before


def test_reported_roundtrip_and_bad_state():
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'r.json')
    assert da.load_reported(p) == set()
    da.save_reported(p, {'b', 'a'})
    assert da.load_reported(p) == {'a', 'b'} and not os.path.exists(p + '.tmp')
    with open(p, 'w') as f:
        f.write('{not json')
    assert da.load_reported(p) == set()
    with open(p, 'w') as f:
        f.write('{"a": 1}')
    assert da.load_reported(p) == set()
    da.save_reported(os.path.join(d, 'no', 'such', 'dir.json'), {'x'})   # never raises


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
