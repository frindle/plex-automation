"""Frozen test: season_migration.run_once plans a pilot of one torrent first,
then batches of BATCH, through speed_cap.run_migrations (cap, dry-run
default, pause on Error). Real files under tmp_path; only Deluge is faked."""
import os

import episode_retention
import season_migration as sm
import speed_cap


class Proc:
    def __init__(self, rc):
        self.returncode = rc


def link_run(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        src, dst = cmd[2], cmd[3]
        for root, _d, files in os.walk(src):
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            for n in files:
                os.link(os.path.join(root, n), os.path.join(dst, rel, n))
        return Proc(0)
    return run


def library(tmp_path, monkeypatch, n):
    media = str(tmp_path / 'Media')
    seed = str(tmp_path / 'seed')
    os.makedirs(seed)
    monkeypatch.setattr(episode_retention, 'MEDIA_ROOT', media)
    out = {}
    for i in range(n):
        name = 'Show%02d.S01' % i
        save = os.path.join(media, 'TV', 'Show%02d' % i)
        os.makedirs(os.path.join(save, name))
        with open(os.path.join(save, name, 'e1.mkv'), 'w') as f:
            f.write('x')
        out['h%02d' % i] = {'name': name, 'label': 'sonarr', 'state': 'Seeding',
                            'progress': 100.0, 'save_path': save}
    return out, seed


class Rpc:
    def __init__(self, all_torrents, seed, error=False):
        self.all = all_torrents
        self.seed = seed
        self.error = error
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'core.get_torrents_status':
            if params[1] == ['state']:
                return {'x': {'state': 'Error' if self.error else 'Seeding'}}
            return self.all
        if method == 'core.get_torrent_status':
            fields = params[1]
            if fields == [speed_cap.CAP_FIELD]:
                return {speed_cap.CAP_FIELD: -1}
            if 'save_path' in fields:
                return {'state': 'Paused', 'save_path': self.seed}
            return {'state': 'Seeding'}
        return None

    def methods(self, name):
        return [p for m, p in self.calls if m == name]


def test_dry_run_pilot_plans_one_and_runs_nothing(tmp_path, monkeypatch):
    tors, seed = library(tmp_path, monkeypatch, 15)
    rpc, state, calls = Rpc(tors, seed), {}, []
    res = sm.run_once(rpc, state, env={'SEEDING_DIR': seed}, run=link_run(calls))
    assert res['dry_run'] is True
    assert res['planned'] == ['h00']
    assert res['candidates'] == 15
    assert not state.get('pilot_done')
    assert state['last'] is res
    assert calls == []
    assert rpc.methods('core.set_torrent_options') == []
    assert rpc.methods('core.move_storage') == []


def test_apply_pilot_then_batch(tmp_path, monkeypatch):
    tors, seed = library(tmp_path, monkeypatch, 15)
    rpc, state, calls = Rpc(tors, seed), {}, []
    env = {'MIGRATION_APPLY': '1', 'SEEDING_DIR': seed}
    res = sm.run_once(rpc, state, env=env, run=link_run(calls), sleep=lambda s: None)
    assert res['done'] == ['h00'] and len(calls) == 1
    assert state['pilot_done'] is True
    lib = os.path.join(tors['h00']['save_path'], 'Show00.S01', 'e1.mkv')
    assert os.stat(lib).st_ino == os.stat(os.path.join(seed, 'Show00.S01', 'e1.mkv')).st_ino
    assert rpc.methods('core.move_storage') == [[['h00'], seed]]
    assert [['h00'], {speed_cap.CAP_FIELD: speed_cap.CAP_KBPS}] in rpc.methods('core.set_torrent_options')
    res2 = sm.run_once(rpc, state, env=env, run=link_run(calls), sleep=lambda s: None)
    assert len(res2['done']) == sm.BATCH
    assert res2['done'][0] == 'h00'


def test_error_torrent_pauses(tmp_path, monkeypatch):
    tors, seed = library(tmp_path, monkeypatch, 3)
    rpc, state, calls = Rpc(tors, seed, error=True), {}, []
    res = sm.run_once(rpc, state, env={'MIGRATION_APPLY': '1', 'SEEDING_DIR': seed},
                      run=link_run(calls), sleep=lambda s: None)
    assert res['paused'] is True and res['done'] == []
    assert calls == []
    assert not state.get('pilot_done')


def test_failed_pilot_keeps_pilot_mode(tmp_path, monkeypatch):
    tors, seed = library(tmp_path, monkeypatch, 3)
    rpc, state = Rpc(tors, seed), {}
    res = sm.run_once(rpc, state, env={'MIGRATION_APPLY': '1', 'SEEDING_DIR': seed},
                      run=lambda cmd, **kw: Proc(1), sleep=lambda s: None)
    assert res['failed'] == ['h00']
    assert not state.get('pilot_done')
    assert rpc.methods('core.move_storage') == []


def test_fetch_used_instead_of_rpc_list_and_seeding_dir_env(tmp_path, monkeypatch):
    tors, seed = library(tmp_path, monkeypatch, 2)
    rpc, state, calls = Rpc({}, seed), {'pilot_done': True}, []
    res = sm.run_once(rpc, state, fetch=lambda: tors, env={'MIGRATION_APPLY': '1', 'SEEDING_DIR': seed},
                      run=link_run(calls), sleep=lambda s: None)
    assert res['done'] == ['h00', 'h01']
    assert rpc.methods('core.move_storage') == [[['h00'], seed], [['h01'], seed]]
    assert [c[3] for c in calls] == [os.path.join(seed, 'Show00.S01'), os.path.join(seed, 'Show01.S01')]
    assert not any(p[1] == sm.FIELDS for p in rpc.methods('core.get_torrents_status'))
