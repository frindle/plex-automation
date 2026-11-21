import os

# Tests must never reach the real services. Every service address points at
# a port nothing listens on, so a test that forgets to fake a call fails fast
# instead of logging in to Deluge or calling the live arr-webhook. A test that
# needs a specific address still sets its own.
_DEAD = 'http://127.0.0.1:9'
for _k in ('DELUGE_URL', 'SONARR_URL', 'RADARR_URL', 'PLEX_URL', 'TIERING_API', 'WEBHOOK'):
    os.environ[_k] = _DEAD

# advise() captures facts for back-tests; keep that out of the real /data/backtest.
import tempfile
_TEST_BT = tempfile.mkdtemp(prefix='plex-test-backtest-')
os.environ['LLM_BACKTEST_DIR'] = _TEST_BT

# The earner set (top 30 uploaders never unseed) would cover every torrent in a
# small test fixture. Off by default here; test_seed_earners.py turns it on.
os.environ['SEED_KEEP_TOP_EARNERS'] = '0'

import copy
import sys
import types

import pytest

import llm_advisor

llm_advisor.DEFAULT_BACKTEST_DIR = _TEST_BT   # tests that pass their own env must not reach /data/backtest

# Test isolation: every test runs as if its file ran alone.
# 1. Import time. Many files set os.environ and then import arr-webhook, which reads the env at import. Python caches
#    the first import, so a later file would get a copy built with another file's env. Each test file is therefore
#    imported with only this conftest's env and gets its own fresh arr-webhook; its env and its copy are recorded.
#    Files also patch shared objects when imported (requests.Session.post = fake, requests.get = fake); last one
#    loaded would win for everyone, so each file's import-time patches are recorded, undone, and re-applied only
#    for that file's tests.
# 2. Run time. Many tests patch module globals by plain assignment (aw.session = fake, aw.requests.get = fake,
#    aw.time.sleep = ...) or fill module-level sets and caches, and never put them back. After collection the
#    globals of every module in this repo (each file's arr-webhook copy included) and of the shared library modules
#    tests patch are recorded; after each test they are put back. Plain dict/list/set globals are restored in
#    place, so code holding a reference sees the reset. Before each test, its file's env and arr-webhook copy return.
_ROOT = os.path.dirname(os.path.abspath(__file__))
_AW = 'arr-webhook'
_SHARED = ('requests', 'time', 'threading', 'subprocess', 'shutil', 'socket', 'urllib.request')
_CONTAINERS = (dict, list, set)
_BASE_ENV = dict(os.environ)
_FILE_STATE = {}        # test file path -> (its arr-webhook module or None, its env after import, its patches)
_BASELINE = None


def _set_env(env):
    """Make os.environ equal env, leaving pytest's own PYTEST_* keys alone."""
    for k in [k for k in os.environ if k not in env and not k.startswith('PYTEST_')]:
        del os.environ[k]
    for k, v in env.items():
        if not k.startswith('PYTEST_') and os.environ.get(k) != v:
            os.environ[k] = v


_MISSING = object()
_FILE_PATCHES = {}      # test file path -> [(object, attr, value)] it set on shared objects while being imported


def _watched():
    """Shared objects test files patch at import: library modules, requests.Session, and repo modules."""
    objs = [sys.modules[n] for n in _SHARED if n in sys.modules]
    rq = sys.modules.get('requests')
    if rq is not None:
        objs.append(rq.Session)
    objs += [m for m in list(sys.modules.values()) if _is_repo_module(m)]
    return {id(o): o for o in objs}


def _plain_items(obj):
    """An object's own attributes, minus submodules (importing a submodule adds one; that is not a patch)."""
    return {k: v for k, v in vars(obj).items() if not isinstance(v, types.ModuleType)}


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if not isinstance(collector, pytest.Module):
        yield
        return
    prev = sys.modules.pop(_AW, None)
    _set_env(_BASE_ENV)
    before = [(o, _plain_items(o)) for o in _watched().values()]
    yield
    mine = sys.modules.get(_AW)
    patches = []
    for obj, snap in before:
        cur = _plain_items(obj)
        for k, v in cur.items():
            if snap.get(k, _MISSING) is not v:
                patches.append((obj, k, v))
                if k in snap:
                    setattr(obj, k, snap[k])
                else:
                    delattr(obj, k)
    _FILE_STATE[str(collector.path)] = (mine, dict(os.environ), patches)
    if mine is None and prev is not None:
        sys.modules[_AW] = prev


def _is_repo_module(mod):
    f = getattr(mod, '__file__', None) or ''
    return f.startswith(_ROOT + os.sep) and os.sep + '.venv' not in f[len(_ROOT):] and \
        os.path.basename(f) != 'conftest.py' and not os.path.basename(f).startswith('test_')


def _snap_container(v):
    try:
        return copy.deepcopy(v)
    except Exception:
        return copy.copy(v)


def _take_baseline():
    mods = {id(m): m for m in sys.modules.values() if _is_repo_module(m)}
    for m, _env, _patches in _FILE_STATE.values():
        if m is not None:
            mods[id(m)] = m
    repo = []
    for mod in mods.values():
        d = mod.__dict__
        repo.append((mod, dict(d), {k: _snap_container(v) for k, v in d.items()
                                    if type(v) in _CONTAINERS and not k.startswith('__')}))
    shared = [sys.modules[n] for n in _SHARED if n in sys.modules]
    if 'requests' in sys.modules:
        shared.append(sys.modules['requests'].Session)
    return repo, [(o, _plain_items(o)) for o in shared]


def _restore(baseline):
    repo, shared = baseline
    for mod, attrs, conts in repo:
        d = mod.__dict__
        for k in [k for k in d if k not in attrs]:
            del d[k]
        for k, v in attrs.items():
            if d.get(k, d) is not v:
                d[k] = v
        for k, snap in conts.items():
            cur = d[k]
            try:
                same = cur == snap
            except Exception:
                same = False
            if not same:
                cur.clear()
                if isinstance(cur, list):
                    cur.extend(_snap_container(snap))
                else:
                    cur.update(_snap_container(snap))
    for obj, attrs in shared:
        cur = _plain_items(obj)
        for k in [k for k in cur if k not in attrs]:
            delattr(obj, k)
        for k, v in attrs.items():
            if cur.get(k, _MISSING) is not v:
                setattr(obj, k, v)


def pytest_collection_finish(session):
    global _BASELINE
    _BASELINE = _take_baseline()


@pytest.fixture(autouse=True)
def _reset_module_state(request):
    mine, env, patches = _FILE_STATE.get(str(request.path), (None, None, ()))
    if env is not None:
        _set_env(env)
    if mine is not None:
        sys.modules[_AW] = mine
    for obj, k, v in patches:
        setattr(obj, k, v)
    yield
    if _BASELINE is not None:
        _restore(_BASELINE)
    if env is not None:
        _set_env(env)


@pytest.fixture
def aw(_reset_module_state):
    """The arr-webhook module, for the script-style tests that take it as an argument (test_queue_lanes)."""
    return __import__(_AW)


@pytest.fixture(autouse=True)
def _isolated_llm_pending(_reset_module_state, tmp_path, monkeypatch):
    """Keep the pending store off /data and out of other tests."""
    monkeypatch.setenv('LLM_PENDING_PATH', str(tmp_path / 'llm_pending.json'))
    llm_advisor._STORES.clear()
    llm_advisor._MODELS_CACHE.clear()
    llm_advisor._UNRAID_HEALTH.clear()
    yield
