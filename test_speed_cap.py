"""Adversarial fixture for: aw-speed-cap (speed_cap.py, a new module).

A fake Deluge rpc(method, params) records every call into one shared event
log, so ordering (cap BEFORE the operation, restore only AFTER Seeding, one
migration finished before the next starts) is asserted directly.
"""
import sys
import importlib.util

import os
spec = importlib.util.spec_from_file_location(
    "target", os.path.join(os.path.dirname(os.path.abspath(__file__)), 'speed_cap.py'))
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)


class FakeDeluge:
    def __init__(self, prior=None, states=None, others=None):
        self.prior = prior or {}          # hash -> max_download_speed before
        self.states = states or {}        # hash -> list of states returned in order
        self.others = others or {}        # extra torrents for get_torrents_status
        self.events = []
        self.sleeps = 0

    def rpc(self, method, params):
        if method == 'core.get_torrent_status':
            h, fields = params
            if 'state' in fields:
                seq = self.states.get(h, ['Seeding'])
                st = seq.pop(0) if len(seq) > 1 else seq[0]
                self.events.append(('poll', h, st))
                return {'state': st}
            return {'max_download_speed': self.prior.get(h, -1)}
        if method == 'core.set_torrent_options':
            hashes, opts = params
            self.events.append(('set', tuple(hashes), dict(opts)))
            return None
        if method == 'core.get_torrents_status':
            self.events.append(('scan',))
            return dict(self.others)
        self.events.append(('other', method))
        return None

    def sleep(self, s):
        self.sleeps += 1

    def op(self, h, rv=True):
        def run():
            self.events.append(('op', h))
            return rv
        return run

    def sets(self):
        return [e for e in self.events if e[0] == 'set']

    def order(self):
        return [e[0] if e[0] != 'set' else 'set%d' % e[2]['max_download_speed']
                for e in self.events if e[0] in ('set', 'op')]


# ── apply / restore ──────────────────────────────────────────────────────
def c_apply():
    d = FakeDeluge(prior={'h1': -1})
    prior = target.apply_cap(d.rpc, 'h1')
    return prior, d.sets()


def c_constants():
    return target.CAP_FIELD, target.CAP_KBPS, target.DEFAULT_PER_TICK, target.HARD_MAX_PER_TICK


# ── capped_operation ─────────────────────────────────────────────────────
def c_cap_before_op_restore_after():
    d = FakeDeluge(prior={'h1': -1}, states={'h1': ['Checking', 'Checking', 'Seeding']})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=d.sleep)
    return ok, d.order(), d.sleeps


def c_restore_to_prior_not_unlimited():
    # an upgrade-lane torrent already sat at 30; another at 50 -> restore exactly that
    d = FakeDeluge(prior={'h1': 50})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=d.sleep)
    return ok, d.sets()[-1]


def c_error_keeps_cap():
    d = FakeDeluge(prior={'h1': -1}, states={'h1': ['Checking', 'Error']})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=d.sleep)
    return ok, d.order()


def c_op_false_keeps_cap_no_poll():
    d = FakeDeluge(prior={'h1': -1})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1', rv=False), sleep=d.sleep)
    return ok, d.order(), [e for e in d.events if e[0] == 'poll']


def c_timeout_keeps_cap():
    d = FakeDeluge(prior={'h1': -1}, states={'h1': ['Checking']})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=d.sleep, poll_seconds=1, max_polls=4)
    return ok, d.order(), d.sleeps


def c_paused_is_not_seeding():
    d = FakeDeluge(prior={'h1': -1}, states={'h1': ['Paused']})
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=d.sleep, max_polls=3)
    return ok, d.order()


# ── pacing ───────────────────────────────────────────────────────────────
def c_limits():
    f = target.per_tick_limit
    return (f({}), f({'MIGRATION_PER_TICK': '3'}), f({'MIGRATION_PER_TICK': '100'}),
            f({'MIGRATION_PER_TICK': 'x'}), f({'MIGRATION_PER_TICK': '-2'}),
            f({'MIGRATION_PER_TICK': '25'}))


def _items(d, n):
    return [('h%d' % i, d.op('h%d' % i)) for i in range(n)]


def c_dry_run_default():
    d = FakeDeluge()
    r = target.run_migrations(d.rpc, _items(d, 7), env={}, sleep=d.sleep)
    return r['dry_run'], r['planned'], r['done'], d.order()


def c_apply_paced_sequential():
    d = FakeDeluge()
    r = target.run_migrations(d.rpc, _items(d, 7), env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return r['dry_run'], r['paused'], r['failed'], r['done'], d.order()


def c_prior_missing_field():
    d = FakeDeluge()
    events = []

    def rpc(method, params):
        if method == 'core.get_torrent_status' and 'state' not in params[1]:
            return None
        return d.rpc(method, params)
    ok = target.capped_operation(rpc, 'h1', d.op('h1'), sleep=d.sleep)
    return ok, d.sets()


def c_poll_defaults():
    d = FakeDeluge(states={'h1': ['Checking']})
    waits = []
    ok = target.capped_operation(d.rpc, 'h1', d.op('h1'), sleep=waits.append)
    return ok, len(waits), set(waits)


def c_apply_env_limit_2():
    d = FakeDeluge()
    r = target.run_migrations(d.rpc, _items(d, 7),
                              env={'MIGRATION_APPLY': '1', 'MIGRATION_PER_TICK': '2'}, sleep=d.sleep)
    return r['done']


def c_pause_on_existing_error():
    d = FakeDeluge(others={'zz': {'state': 'Error'}, 'yy': {'state': 'Seeding'}})
    r = target.run_migrations(d.rpc, _items(d, 3), env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return r['paused'], r['done'], d.order()


def c_pause_when_error_appears_midway():
    d = FakeDeluge()
    items = _items(d, 4)
    first_op = items[0][1]

    def op0():
        d.others['h0'] = {'state': 'Error'}
        return first_op()
    items[0] = ('h0', op0)
    r = target.run_migrations(d.rpc, items, env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return r['paused'], [e[1] for e in d.events if e[0] == 'op']


def c_failed_migration_stops_batch():
    d = FakeDeluge(states={'h0': ['Checking', 'Error']})
    r = target.run_migrations(d.rpc, _items(d, 3), env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return r['failed'], r['done'], [e[1] for e in d.events if e[0] == 'op']


def c_no_parallel_migration():
    d = FakeDeluge()
    nested = {}

    def op0():
        d.events.append(('op', 'h0'))
        nested['r'] = target.run_migrations(d.rpc, [('hX', d.op('hX'))],
                                            env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
        return True
    r = target.run_migrations(d.rpc, [('h0', op0)], env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    again = target.run_migrations(d.rpc, [('h9', d.op('h9'))], env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return (nested['r'].get('skipped'), nested['r']['done'], r['done'], again['done'],
            [e[1] for e in d.events if e[0] == 'op'])


def c_lock_released_after_exception():
    d = FakeDeluge()

    def boom():
        raise RuntimeError('deluge down')
    try:
        target.run_migrations(d.rpc, [('h0', boom)], env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    except RuntimeError:
        pass
    r = target.run_migrations(d.rpc, [('h1', d.op('h1'))], env={'MIGRATION_APPLY': '1'}, sleep=d.sleep)
    return r['done']


SEQ5 = []
for i in range(5):
    SEQ5 += ['set30', 'op', 'set-1']

CASES = [
    ("constants: field/cap/default/hard max", c_constants,
     ('max_download_speed', 30, 5, 25)),
    ("apply_cap returns prior, sets exactly 30 on [hash]", c_apply,
     (-1, [('set', ('h1',), {'max_download_speed': 30})])),
    ("cap applied before op, restored only after Seeding verified", c_cap_before_op_restore_after,
     (True, ['set30', 'op', 'set-1'], 2)),
    ("restore uses the prior limit, not unlimited", c_restore_to_prior_not_unlimited,
     (True, ('set', ('h1',), {'max_download_speed': 50}))),
    ("Error after op: cap kept, returns False", c_error_keeps_cap,
     (False, ['set30', 'op'])),
    ("op returns False: cap kept, no polling", c_op_false_keeps_cap_no_poll,
     (False, ['set30', 'op'], [])),
    ("never Seeding within max_polls: cap kept", c_timeout_keeps_cap,
     (False, ['set30', 'op'], 4)),
    ("Paused is not verified Seeding", c_paused_is_not_seeding,
     (False, ['set30', 'op'])),
    ("per_tick_limit: default/env/hard max/bad/negative/25", c_limits,
     (5, 3, 25, 5, 0, 25)),
    ("dry-run by default: nothing capped, nothing run", c_dry_run_default,
     (True, ['h0', 'h1', 'h2', 'h3', 'h4'], [], [])),
    ("apply: 5 per tick, strictly one at a time", c_apply_paced_sequential,
     (False, False, [], ['h0', 'h1', 'h2', 'h3', 'h4'], SEQ5)),
    ("no prior limit reported -> restore to -1 (unlimited)", c_prior_missing_field,
     (True, [('set', ('h1',), {'max_download_speed': 30}), ('set', ('h1',), {'max_download_speed': -1})])),
    ("default polling: 120 polls, 30s apart, cap kept", c_poll_defaults, (False, 120, {30})),
    ("MIGRATION_PER_TICK=2 honored", c_apply_env_limit_2, ['h0', 'h1']),
    ("existing Error torrent pauses the tick", c_pause_on_existing_error,
     (True, [], [])),
    ("Error appearing mid-tick pauses before the next", c_pause_when_error_appears_midway,
     (True, ['h0'])),
    ("failed migration stops the batch", c_failed_migration_stops_batch,
     (['h0'], [], ['h0'])),
    ("no parallel migration; lock released after", c_no_parallel_migration,
     ('migration-in-flight', [], ['h0'], ['h9'], ['h0', 'h9'])),
    ("lock released even when op raises", c_lock_released_after_exception, ['h1']),
]


def main():
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


def test_all_cases():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
