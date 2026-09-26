"""Adversarial fixture for: arr-webhook-recent-upgrade-priority-s4-hourly-sweep-keeps-recent-on-top

Stubs deluge_login / get_all_torrents / session, runs one pass of
prioritize_normal_torrents(), and asserts exactly which hashes land in the
core.queue_top vs core.queue_bottom calls. The recent-upgrade labels must be
topped (never bottomed); plain sonarr/radarr stay topped; old upgrade labels
stay bottomed; empty lists produce no Deluge call at all.

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


class _FakeResp:
    def raise_for_status(self):
        pass


def _run_pass(torrents, boom=None):
    """Run one prioritize_normal_torrents() pass against fake Deluge state and
    return (top_calls, bottom_calls), each a list of the hash lists passed to
    core.queue_top / core.queue_bottom respectively."""
    calls = []

    class _FakeSession:
        def post(self, url, **kw):
            calls.append(kw['json'])
            return _FakeResp()

    orig_login = target.deluge_login
    orig_get = target.get_all_torrents
    orig_session = target.session
    target.deluge_login = lambda: None
    if boom is not None:
        def _get():
            raise boom
        target.get_all_torrents = _get
    else:
        target.get_all_torrents = lambda: torrents
    target.session = _FakeSession()
    try:
        target.prioritize_normal_torrents()
    finally:
        target.deluge_login = orig_login
        target.get_all_torrents = orig_get
        target.session = orig_session
    top = [c['params'][0] for c in calls if c['method'] == 'core.queue_top']
    bottom = [c['params'][0] for c in calls if c['method'] == 'core.queue_bottom']
    return (top, bottom)


CASES = [
    ("radarr-upgrade-recent is topped and appears in NO core.queue_bottom call",
     lambda: _run_pass({'h-rr': {'label': 'radarr-upgrade-recent'}}),
     ([['h-rr']], [])),

    ("sonarr-upgrade-recent is topped and appears in NO core.queue_bottom call",
     lambda: _run_pass({'h-sr': {'label': 'sonarr-upgrade-recent'}}),
     ([['h-sr']], [])),

    ("radarr-upgrade (old upgrade label) still bottomed, NOT in the top list",
     lambda: _run_pass({'h-ru': {'label': 'radarr-upgrade'}}),
     ([], [['h-ru']])),

    ("sonarr-upgrade (old upgrade label) still bottomed, NOT in the top list",
     lambda: _run_pass({'h-su': {'label': 'sonarr-upgrade'}}),
     ([], [['h-su']])),

    ("plain sonarr/radarr labels still topped exactly as before",
     lambda: _run_pass({'a': {'label': 'sonarr'}, 'b': {'label': 'radarr'}}),
     ([['a', 'b']], [])),

    ("one of each of all four labels in a single pass -> exactly the right hashes per call",
     lambda: _run_pass({
         'h-sonarr': {'label': 'sonarr'},
         'h-radarr': {'label': 'radarr'},
         'h-sr': {'label': 'sonarr-upgrade-recent'},
         'h-rr': {'label': 'radarr-upgrade-recent'},
     }),
     ([['h-sonarr', 'h-radarr', 'h-sr', 'h-rr']], [])),

    ("old upgrade labels together -> both bottomed, no top call at all",
     lambda: _run_pass({'s': {'label': 'sonarr-upgrade'}, 'r': {'label': 'radarr-upgrade'}}),
     ([], [['s', 'r']])),

    ("no matching label -> NO Deluge call is made for an empty list",
     lambda: _run_pass({'x': {'label': 'unrelated'}, 'y': {}}),
     ([], [])),

    ("function stays exception-safe: a failing get_all_torrents does not raise out",
     lambda: _run_pass({}, boom=RuntimeError('deluge down')),
     ([], [])),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
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


if __name__ == "__main__":
    sys.exit(main())
