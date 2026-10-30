"""Frozen test: season_migration.same_inodes proves dst is a full hardlink
mirror of src (same device + inode for every regular file)."""
import os

import season_migration as sm


def mk(path, data='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(data)


def mirror(src, dst):
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for n in files:
            os.link(os.path.join(root, n), os.path.join(dst, rel, n))


def test_directory_hardlink_mirror(tmp_path):
    src, dst = str(tmp_path / 'lib' / 'Pack'), str(tmp_path / 'seed' / 'Pack')
    mk(os.path.join(src, 'e1.mkv'))
    mk(os.path.join(src, 'sub', 'e2.mkv'))
    mirror(src, dst)
    assert sm.same_inodes(src, dst) is True


def test_copy_not_link_or_missing_file_fails(tmp_path):
    src, dst = str(tmp_path / 'c'), str(tmp_path / 'd')
    mk(os.path.join(src, 'e1.mkv'), 'same')
    mk(os.path.join(dst, 'e1.mkv'), 'same')
    assert sm.same_inodes(src, dst) is False
    src, dst = str(tmp_path / 'a'), str(tmp_path / 'b')
    mk(os.path.join(src, 'e1.mkv'))
    mk(os.path.join(src, 'e2.mkv'))
    os.makedirs(dst)
    os.link(os.path.join(src, 'e1.mkv'), os.path.join(dst, 'e1.mkv'))
    assert sm.same_inodes(src, dst) is False


def test_missing_paths_and_empty_dir(tmp_path):
    src = str(tmp_path / 'a')
    os.makedirs(src)
    assert sm.same_inodes(src, str(tmp_path / 'b')) is False
    assert sm.same_inodes(str(tmp_path / 'nope'), src) is False
    os.makedirs(str(tmp_path / 'b'))
    assert sm.same_inodes(src, str(tmp_path / 'b')) is False


def test_single_file_torrent(tmp_path):
    src, dst = str(tmp_path / 'm.mkv'), str(tmp_path / 'seed.mkv')
    mk(src)
    os.link(src, dst)
    assert sm.same_inodes(src, dst) is True
    other = str(tmp_path / 'c.mkv')
    mk(other)
    assert sm.same_inodes(src, other) is False


def test_symlink_in_src_fails(tmp_path):
    src, dst = str(tmp_path / 'a'), str(tmp_path / 'b')
    mk(os.path.join(src, 'e1.mkv'))
    os.symlink(os.path.join(src, 'e1.mkv'), os.path.join(src, 'link.mkv'))
    os.makedirs(dst)
    os.link(os.path.join(src, 'e1.mkv'), os.path.join(dst, 'e1.mkv'))
    os.symlink(os.path.join(src, 'e1.mkv'), os.path.join(dst, 'link.mkv'))
    assert sm.same_inodes(src, dst) is False
