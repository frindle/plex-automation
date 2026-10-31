"""Frozen test: migrate_op passes Deluge a LIST of ids ([[hash]]) to
force_recheck and resume_torrent, like pause_torrent and move_storage."""
import os

import season_migration as sm


class Proc:
    returncode = 0


def fake_run(cmd, **kw):
    assert cmd[:2] == ['cp', '-al']
    src, dst = cmd[2], cmd[3]
    for root, _d, files in os.walk(src):
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for n in files:
            os.link(os.path.join(root, n), os.path.join(dst, rel, n))
    return Proc()


def test_recheck_and_resume_take_a_list_of_ids(tmp_path):
    lib = str(tmp_path / 'Media' / 'TV')
    seed = str(tmp_path / 'seed')
    os.makedirs(seed)
    os.makedirs(os.path.join(lib, 'Show.S01'))
    for n in ('e1.mkv', 'e2.mkv'):
        with open(os.path.join(lib, 'Show.S01', n), 'w') as f:
            f.write('x')
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == 'core.get_torrent_status':
            return {'state': 'Paused', 'save_path': seed}
        return None

    op = sm.migrate_op(rpc, 'h1', {'name': 'Show.S01', 'save_path': lib},
                       seeding_dir=seed, run=fake_run, sleep=lambda s: None)
    assert op() is True
    assert ('core.pause_torrent', [['h1']]) in calls
    assert ('core.force_recheck', [['h1']]) in calls
    assert ('core.resume_torrent', [['h1']]) in calls


def test_recheck_and_resume_skipped_when_move_never_finishes(tmp_path):
    lib = str(tmp_path / 'Media' / 'TV')
    seed = str(tmp_path / 'seed')
    os.makedirs(seed)
    os.makedirs(os.path.join(lib, 'Show.S01'))
    with open(os.path.join(lib, 'Show.S01', 'e1.mkv'), 'w') as f:
        f.write('x')
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == 'core.get_torrent_status':
            return {'state': 'Error', 'save_path': lib}
        return None

    op = sm.migrate_op(rpc, 'h1', {'name': 'Show.S01', 'save_path': lib},
                       seeding_dir=seed, run=fake_run, sleep=lambda s: None)
    assert op() is False
    assert ('core.move_storage', [['h1'], seed]) in calls
    assert not [c for c in calls if c[0] in ('core.force_recheck', 'core.resume_torrent')]
