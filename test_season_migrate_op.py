"""Frozen test: season_migration.migrate_op builds the per-torrent op:
cp -al hardlink, inode check, pause, move_storage, wait, re-check the library
copy, force_recheck, resume. Any failed step returns False and stops there."""
import os

import season_migration as sm


def mk(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('x')


class Proc:
    def __init__(self, rc):
        self.returncode = rc


def link_run(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        assert cmd[:2] == ['cp', '-al']
        src, dst = cmd[2], cmd[3]
        for root, _d, files in os.walk(src):
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            for n in files:
                os.link(os.path.join(root, n), os.path.join(dst, rel, n))
        return Proc(0)
    return run


class Rpc:
    def __init__(self, seeding_dir, moves=('Moving', None), fail_state=None):
        self.calls = []
        self.seeding_dir = seeding_dir
        self.moves = list(moves)
        self.fail_state = fail_state

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'core.get_torrent_status':
            if self.fail_state:
                return {'state': self.fail_state, 'save_path': '/old'}
            nxt = self.moves.pop(0) if self.moves else None
            if nxt == 'Moving':
                return {'state': 'Moving', 'save_path': '/old'}
            return {'state': 'Paused', 'save_path': self.seeding_dir + '/'}
        return None

    def methods(self):
        return [m for m, _ in self.calls if m != 'core.get_torrent_status']


def setup(tmp_path):
    lib = str(tmp_path / 'Media' / 'TV' / 'Show')
    seed = str(tmp_path / 'seed')
    os.makedirs(seed)
    mk(os.path.join(lib, 'Show.S01', 'e1.mkv'))
    mk(os.path.join(lib, 'Show.S01', 'e2.mkv'))
    t = {'name': 'Show.S01', 'save_path': lib}
    return lib, seed, t


def test_happy_path_order_and_library_intact(tmp_path):
    lib, seed, t = setup(tmp_path)
    rpc, calls, sleeps = Rpc(seed), [], []
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=link_run(calls), sleep=sleeps.append, poll_seconds=7)
    assert rpc.calls == []  # building the op does nothing
    assert op() is True
    assert calls == [['cp', '-al', os.path.join(lib, 'Show.S01'), os.path.join(seed, 'Show.S01')]]
    assert rpc.methods() == ['core.pause_torrent', 'core.move_storage', 'core.force_recheck', 'core.resume_torrent']
    assert ('core.move_storage', [['h1'], seed]) in rpc.calls
    assert ('core.pause_torrent', [['h1']]) in rpc.calls
    assert sleeps == [7]
    assert os.path.exists(os.path.join(lib, 'Show.S01', 'e1.mkv'))


def test_cp_failure_stops_before_rpc(tmp_path):
    _lib, seed, t = setup(tmp_path)
    rpc = Rpc(seed)
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=lambda cmd, **kw: Proc(1), sleep=lambda s: None)
    assert op() is False
    assert rpc.calls == []
    # an existing dst that is a copy, not a hardlink, is refused before any rpc too
    mk(os.path.join(seed, 'Show.S01', 'e1.mkv'))
    mk(os.path.join(seed, 'Show.S01', 'e2.mkv'))
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=link_run([]), sleep=lambda s: None)
    assert op() is False
    assert rpc.calls == []


def test_existing_dst_not_recopied_but_must_match(tmp_path):
    lib, seed, t = setup(tmp_path)
    calls = []
    link_run(calls)(['cp', '-al', os.path.join(lib, 'Show.S01'), os.path.join(seed, 'Show.S01')])
    calls.clear()
    rpc = Rpc(seed, moves=[None])
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=link_run(calls), sleep=lambda s: None)
    assert op() is True
    assert calls == []


def test_error_or_stuck_move_stops(tmp_path):
    _lib, seed, t = setup(tmp_path)
    rpc = Rpc(seed, fail_state='Error')
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=link_run([]), sleep=lambda s: None)
    assert op() is False
    assert rpc.methods() == ['core.pause_torrent', 'core.move_storage']
    # a move that never finishes gives up after MOVE_POLLS polls
    rpc = Rpc(seed, moves=['Moving'] * (sm.MOVE_POLLS + 5))
    sleeps = []
    op = sm.migrate_op(rpc, 'h1', t, seeding_dir=seed, run=link_run([]), sleep=sleeps.append)
    assert op() is False
    assert len(sleeps) == sm.MOVE_POLLS
    assert 'core.force_recheck' not in rpc.methods()


def test_library_copy_lost_after_move_stops(tmp_path):
    lib, seed, t = setup(tmp_path)
    rpc = Rpc(seed, moves=[None])
    orig = rpc.__call__

    def rpc_deleting(method, params):
        if method == 'core.move_storage':
            os.remove(os.path.join(lib, 'Show.S01', 'e2.mkv'))
        return orig(method, params)
    op = sm.migrate_op(rpc_deleting, 'h1', t, seeding_dir=seed, run=link_run([]), sleep=lambda s: None)
    assert op() is False
    assert 'core.force_recheck' not in rpc.methods()
    assert 'core.resume_torrent' not in rpc.methods()
