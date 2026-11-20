"""abandoned_audit: report-only buckets over a real temp tree (real files, real hardlinks).
Run: pytest test_abandoned_audit.py"""
import os
import shutil

import abandoned_audit as aa

NOW = 2_000_000_000
OLD = NOW - 30 * 86400
DONE = NOW - 60 * 86400


def put(path, size=10, mtime=OLD):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)
    os.utime(path, (mtime, mtime))
    return str(path)


def torrent(save, rel, label='radarr', progress=100.0, done=DONE):
    return {'name': rel[0], 'label': label, 'save_path': str(save), 'progress': progress,
            'completed_time': done, 'files': [{'path': r, 'size': 0} for r in rel]}


def run(tmp_path, torrents=None, plex=(), arr=(), **kw):
    dl, lib = tmp_path / 'dl', tmp_path / 'lib'
    dl.mkdir(exist_ok=True)
    lib.mkdir(exist_ok=True)
    rows, errs = aa.walk([str(dl), str(lib)])
    return aa.audit(rows, {} if torrents is None else torrents, None if plex is None else list(plex),
                    None if arr is None else list(arr), NOW, downloads_root=str(dl),
                    library_roots=[str(lib)], seed_days=21, walk_errors=errs, **kw)


def paths(rep, bucket):
    return {r['path'] for r in rep['buckets'][bucket]['rows']}


def test_downloads_orphan_found(tmp_path):
    p = put(tmp_path / 'dl' / 'Gone' / 'a.mkv', 100)
    rep = run(tmp_path)
    row = rep['buckets']['downloads_orphan']['rows'][0]
    assert row['path'] == p and row['reclaim_bytes'] == row['size'] == 100
    assert rep['totals']['reclaim_bytes'] == 100


def test_owned_file_not_reported(tmp_path):
    put(tmp_path / 'dl' / 'T' / 'a.mkv', 100)
    t = {'h': torrent(tmp_path / 'dl', ['T/a.mkv'])}
    rep = run(tmp_path, t)
    assert rep['buckets']['downloads_orphan']['count'] == 0
    assert paths(rep, 'downloads_orphan') == set()


def test_grace_skips_new_and_incomplete(tmp_path):
    put(tmp_path / 'dl' / 'new.mkv', 10, mtime=NOW - 3600)
    put(tmp_path / 'dl' / 'T' / 'part.mkv', 10)
    put(tmp_path / 'lib' / 'M' / 'new.mkv', 10, mtime=NOW - 3600)
    put(tmp_path / 'dl' / 'U' / 'u.mkv', 10)
    t = {'h': torrent(tmp_path / 'dl', ['T/part.mkv'], progress=50.0),
         'i': torrent(tmp_path / 'dl', ['U/u.mkv'], progress=99.0)}
    rep = run(tmp_path, t, plex=[], arr=[])
    assert rep['totals']['count'] == 0


def test_library_known_to_plex_kept(tmp_path):
    p = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 50)
    rep = run(tmp_path, plex=[{'path': p, 'size': 50}], arr=[])
    assert paths(rep, 'library_untracked') == set()


def test_library_known_only_to_arr_kept(tmp_path):
    p = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 50)
    rep = run(tmp_path, plex=[], arr=[{'path': p, 'size': 50}])
    assert paths(rep, 'library_untracked') == set()


def test_basename_size_fallback_keeps_file(tmp_path):
    put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 50)
    other_mount = [{'path': '/elsewhere/Movies/Movie (2000)/Movie.mkv', 'size': 50}]
    assert paths(run(tmp_path, plex=other_mount, arr=[]), 'library_untracked') == set()
    wrong_size = [{'path': '/elsewhere/Movies/Movie (2000)/Movie.mkv', 'size': 51}]
    assert len(run(tmp_path, plex=wrong_size, arr=[])['buckets']['library_untracked']['rows']) == 1


def test_library_untracked_reported(tmp_path):
    p = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 50)
    put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.srt', 5)   # not a video: never untracked
    rep = run(tmp_path, plex=[], arr=[])
    b = rep['buckets']['library_untracked']
    assert b['status'] == 'ok' and paths(rep, 'library_untracked') == {p}
    assert b['rows'][0]['reclaim_bytes'] == 50


def test_hardlink_other_link_kept_reclaims_zero(tmp_path):
    lib_file = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 80)
    os.makedirs(tmp_path / 'dl' / 'T')
    os.link(lib_file, tmp_path / 'dl' / 'T' / 'Movie.mkv')
    rep = run(tmp_path, plex=[{'path': lib_file, 'size': 80}], arr=[])
    rows = rep['buckets']['downloads_orphan']['rows']
    assert len(rows) == 1 and rows[0]['reclaim_bytes'] == 0 and rows[0]['size'] == 80
    assert rep['totals']['reclaim_bytes'] == 0


def test_hardlink_all_links_abandoned_reclaims_once(tmp_path):
    lib_file = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie.mkv', 80)
    os.makedirs(tmp_path / 'dl' / 'T')
    os.link(lib_file, tmp_path / 'dl' / 'T' / 'Other.mkv')
    rep = run(tmp_path, plex=[], arr=[])
    assert rep['buckets']['downloads_orphan']['count'] == 1
    assert rep['buckets']['library_untracked']['count'] == 1
    assert rep['totals']['count'] == 2 and rep['totals']['reclaim_bytes'] == 80
    assert (rep['buckets']['downloads_orphan']['reclaim_bytes']
            + rep['buckets']['library_untracked']['reclaim_bytes']) == 80


def test_hardlink_link_outside_walk_reclaims_zero(tmp_path):
    p = put(tmp_path / 'dl' / 'T' / 'a.mkv', 40)
    os.link(p, tmp_path / 'outside.mkv')
    rep = run(tmp_path)
    assert rep['buckets']['downloads_orphan']['rows'][0]['reclaim_bytes'] == 0


def test_source_failure_drops_bucket(tmp_path):
    put(tmp_path / 'lib' / 'M' / 'M.mkv', 50)
    put(tmp_path / 'dl' / 'T' / 'o.mkv', 5)
    rep = run(tmp_path, plex=None, arr=[])
    for name in ('library_untracked', 'torrent_not_in_library', 'plex_missing_file'):
        b = rep['buckets'][name]
        assert b['status'] == 'incomplete' and b['count'] == 0 and b['rows'] == [] and b['note']
    assert rep['buckets']['downloads_orphan']['status'] == 'ok'
    assert rep['sources']['plex'] == 'failed' and rep['sources']['deluge'] == 'ok'
    rows, _ = aa.walk([str(tmp_path / 'dl')])
    rep2 = aa.audit(rows, None, [], [], NOW, downloads_root=str(tmp_path / 'dl'),
                    library_roots=[str(tmp_path / 'lib')])
    assert rep2['buckets']['downloads_orphan']['status'] == 'incomplete'
    assert rep2['buckets']['downloads_orphan']['count'] == 0 and rep2['sources']['deluge'] == 'failed'


def test_no_video_dir(tmp_path):
    put(tmp_path / 'lib' / 'Gone (1999)' / 'a.nfo', 7)
    put(tmp_path / 'lib' / 'Gone (1999)' / 'Subs' / 'a.srt', 11)
    put(tmp_path / 'lib' / 'Kept (2001)' / 'k.mkv', 100)
    put(tmp_path / 'lib' / 'Kept (2001)' / 'k.nfo', 3)
    rep = run(tmp_path, plex=[], arr=[])
    b = rep['buckets']['library_no_video_dir']
    assert [r['path'] for r in b['rows']] == [str(tmp_path / 'lib' / 'Gone (1999)')]
    assert b['rows'][0]['size'] == 18 and b['rows'][0]['reclaim_bytes'] == 18


def test_torrent_not_in_library(tmp_path):
    dl = tmp_path / 'dl'
    put(dl / 'A' / 'a.mkv', 100)
    put(dl / 'A' / 'a.nfo', 1)
    put(dl / 'B' / 'b.mkv', 100)          # known to Plex
    put(dl / 'C' / 'c.mkv', 100)          # label is not sonarr/radarr
    put(dl / 'D' / 'd.mkv', 100)          # seeded too briefly
    put(dl / 'E' / 'e.mkv', 100)          # hardlinked into the library: nlink 2
    os.makedirs(tmp_path / 'lib' / 'E')
    os.link(dl / 'E' / 'e.mkv', tmp_path / 'lib' / 'E' / 'e2.mkv')
    t = {
        'a': torrent(dl, ['A/a.mkv', 'A/a.nfo'], label='Radarr-x'),
        'b': torrent(dl, ['B/b.mkv']),
        'c': torrent(dl, ['C/c.mkv'], label='tv'),
        'd': torrent(dl, ['D/d.mkv'], done=NOW - 5 * 86400),
        'e': torrent(dl, ['E/e.mkv']),
    }
    plex = [{'path': '/other/mount/b.mkv', 'size': 100}]
    rep = run(tmp_path, t, plex=plex, arr=[])
    b = rep['buckets']['torrent_not_in_library']
    assert [r['path'] for r in b['rows']] == [str(dl / 'A')]
    assert b['rows'][0]['size'] == 101 and b['rows'][0]['reclaim_bytes'] == 101
    assert rep['buckets']['downloads_orphan']['count'] == 0   # every Downloads file is owned


def test_plex_missing_file(tmp_path):
    here = put(tmp_path / 'lib' / 'M' / 'M.mkv', 50)
    gone = str(tmp_path / 'lib' / 'M2' / 'M2.mkv')
    unmounted = '/unmapped/prefix/X.mkv'
    rep = run(tmp_path, plex=[{'path': here, 'size': 50}, {'path': gone, 'size': 9}, {'path': unmounted, 'size': 1}],
              arr=[])
    b = rep['buckets']['plex_missing_file']
    assert paths(rep, 'plex_missing_file') == {gone} and b['count'] == 1 and b['bytes'] == 0
    assert b['reclaim_bytes'] == 0 and rep['totals']['count'] == 0


def test_walk_skips_symlinks_and_trash(tmp_path):
    root = tmp_path / 'r'
    put(root / 'real.mkv', 5)
    put(root / '.Trash-1000' / 't.mkv', 5)
    put(root / '@eaDir' / 'e.mkv', 5)
    put(root / '.recycle' / 'r.mkv', 5)
    put(root / 'lost+found' / 'l.mkv', 5)
    os.symlink(root / 'real.mkv', root / 'link.mkv')
    os.symlink(root, root / 'dirlink')
    rows, errs = aa.walk([str(root)])
    assert [r['path'] for r in rows] == [str(root / 'real.mkv')] and errs == 0


def test_walk_counts_read_errors_and_continues(tmp_path, monkeypatch):
    root = tmp_path / 'r'
    put(root / 'bad' / 'x.mkv', 5)
    put(root / 'ok' / 'y.mkv', 5)
    real = os.scandir

    def flaky(p):
        if str(p).endswith('bad'):
            raise PermissionError('no')
        return real(p)
    monkeypatch.setattr(os, 'scandir', flaky)
    rows, errs = aa.walk([str(root)])
    assert errs == 1 and [os.path.basename(r['path']) for r in rows] == ['y.mkv']


def test_rows_sorted_and_capped(tmp_path):
    n = aa.MAX_ROWS + 5
    for i in range(n):
        put(tmp_path / 'dl' / 'T' / f'f{i:04d}.mkv', 10 + i)
    b = run(tmp_path)['buckets']['downloads_orphan']
    assert b['count'] == n and len(b['rows']) == aa.MAX_ROWS
    rec = [r['reclaim_bytes'] for r in b['rows']]
    assert rec == sorted(rec, reverse=True)
    assert b['reclaim_bytes'] == sum(10 + i for i in range(n))


def test_never_deletes(tmp_path, monkeypatch):
    put(tmp_path / 'dl' / 'T' / 'a.mkv', 10)
    put(tmp_path / 'lib' / 'M' / 'M.mkv', 10)
    put(tmp_path / 'lib' / 'N' / 'n.nfo', 10)

    def boom(*a, **k):
        raise AssertionError('audit must not change the disk')
    for mod, name in ((os, 'remove'), (os, 'unlink'), (os, 'rename'), (shutil, 'rmtree'), (shutil, 'move')):
        monkeypatch.setattr(mod, name, boom)
    rep = run(tmp_path, {'h': torrent(tmp_path / 'dl', ['Z/z.mkv'])}, plex=[], arr=[])
    assert rep['totals']['count'] >= 2
    assert os.path.exists(tmp_path / 'dl' / 'T' / 'a.mkv')


def test_plex_extras_not_untracked(tmp_path):
    put(tmp_path / 'lib' / 'Film (2000)' / 'Trailers' / 'teaser.mkv', 20)
    put(tmp_path / 'lib' / 'Film (2000)' / 'Film-trailer.mp4', 20)
    put(tmp_path / 'lib' / 'Film (2000)' / 'Featurettes' / 'making.mkv', 20)
    lone = put(tmp_path / 'lib' / 'Film (2000)' / 'Film.mkv', 50)
    rep = run(tmp_path, plex=[], arr=[])
    assert paths(rep, 'library_untracked') == {lone}


def test_no_video_dir_skips_arr_folders(tmp_path):
    put(tmp_path / 'lib' / 'Waiting (2024)' / 'poster.jpg', 5)      # Radarr folder, no file yet
    put(tmp_path / 'lib' / 'Show' / 'tvshow.nfo', 5)                # Sonarr series folder, no episodes yet
    put(tmp_path / 'lib' / 'Dropped (1990)' / 'a.srt', 5)           # nobody owns it
    arr = [{'path': '/arr/movies/Waiting (2024)', 'dir': True}, {'path': '/arr/tv/Show/', 'dir': True}]
    rep = run(tmp_path, plex=[], arr=arr)
    assert paths(rep, 'library_no_video_dir') == {str(tmp_path / 'lib' / 'Dropped (1990)')}
    rep2 = run(tmp_path, plex=[], arr=None)
    assert rep2['buckets']['library_no_video_dir']['status'] == 'incomplete'
    assert rep2['buckets']['library_no_video_dir']['rows'] == []


def test_arr_folder_rows_are_not_known_files(tmp_path):
    p = put(tmp_path / 'lib' / 'Movie (2000)' / 'Movie (2000).mkv', 50)
    rep = run(tmp_path, plex=[], arr=[{'path': str(tmp_path / 'lib' / 'Movie (2000)'), 'dir': True}])
    assert paths(rep, 'library_untracked') == {p}


def test_torrent_reason_says_copy_or_failed_import(tmp_path):
    dl = tmp_path / 'dl'
    put(dl / 'A' / 'a.release.mkv', 100)
    put(dl / 'B' / 'b.release.mkv', 300)
    put(tmp_path / 'lib' / 'A (2000)' / 'A (2000).mkv', 100)        # same size, renamed copy
    t = {'a': torrent(dl, ['A/a.release.mkv']), 'b': torrent(dl, ['B/b.release.mkv'])}
    plex = [{'path': '/plexmount/A (2000)/A (2000).mkv', 'size': 100}]
    rep = run(tmp_path, t, plex=plex, arr=[])
    reasons = {r['path']: r['reason'] for r in rep['buckets']['torrent_not_in_library']['rows']}
    assert 'imported as a copy' in reasons[str(dl / 'A')]
    assert 'failed import' in reasons[str(dl / 'B')]
