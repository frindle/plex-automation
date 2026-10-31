"""Frozen test: season_migration.start_worker runs ticks on its own daemon
thread, survives exceptions, and stops on the stop event."""
import threading

import season_migration as sm


def test_runs_on_daemon_thread_until_stopped():
    stop = threading.Event()
    seen = []
    tick_done = threading.Event()

    def tick(rpc, state, fetch=None, env=None):
        seen.append((rpc, threading.current_thread().name, fetch, env))
        if len(seen) >= 2:
            tick_done.set()
        return {'ok': True}
    state = {}
    th = sm.start_worker('RPC', state, fetch='F', env={'E': '1'}, interval=0.01, stop=stop, tick=tick)
    assert isinstance(th, threading.Thread)
    assert th.daemon is True
    assert tick_done.wait(5)
    stop.set()
    th.join(5)
    assert not th.is_alive()
    assert seen[0][0] == 'RPC' and seen[0][2] == 'F' and seen[0][3] == {'E': '1'}
    assert seen[0][1] == th.name != threading.main_thread().name
    assert th.name == 'season-migration'


def test_exception_recorded_and_loop_continues():
    stop = threading.Event()
    n = []
    again = threading.Event()

    def tick(rpc, state, fetch=None, env=None):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError('boom')
        again.set()
    state = {}
    th = sm.start_worker(None, state, interval=0.01, stop=stop, tick=tick)
    assert again.wait(5)
    stop.set()
    th.join(5)
    assert 'boom' in state['last_error']


def test_stop_already_set_runs_no_tick():
    stop = threading.Event()
    stop.set()
    called = []
    th = sm.start_worker(None, {}, interval=0.01, stop=stop, tick=lambda *a, **k: called.append(1))
    th.join(5)
    assert called == []
