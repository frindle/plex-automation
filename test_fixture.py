"""Adversarial fixture for: arr-webhook-recent-upgrade-priority-s2-priority-hashes-list-sen

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

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


def _run_prioritize(torrents):
    """Patch deluge/session, run prioritize_normal_torrents, return the list of
    (method, params) payloads it posted to Deluge."""
    calls = []

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            calls.append((json['method'], json['params']))
            return _FakeResp()

    target.deluge_login = lambda: None
    target.get_all_torrents = lambda: torrents
    target.session = FakeSession()
    target.prioritize_normal_torrents()
    return calls


CASES = [
    # 1. The selector returns exactly the sonarr/radarr hashes, in map order,
    #    and excludes upgrade-labeled + unlabeled entries (a wrong fix that also
    #    queues upgrades to top fails this).
    ("selector picks only sonarr/radarr labels, in order",
     lambda: target._select_priority_hashes(
         {'h1': {'label': 'sonarr'}, 'h2': {'label': 'radarr'},
          'h3': {'label': 'sonarr-upgrade'}, 'h4': {}, 'h5': {'label': ''}},
         {'sonarr', 'radarr'}),
     ['h1', 'h2']),

    # 2. Degenerate inputs must not raise and yield an empty list.
    ("selector on empty/missing-label map returns []",
     lambda: target._select_priority_hashes({}, {'sonarr', 'radarr'}) +
             target._select_priority_hashes({'a': {}}, {'sonarr'}),
     []),

    # 3. The priority_hashes list is what reaches core.queue_top -- and the
    #    upgrade hashes still go to core.queue_bottom (regression half).
    ("prioritize sends priority_hashes to core.queue_top, upgrades to bottom",
     lambda: _run_prioritize({
         'a': {'label': 'sonarr'}, 'b': {'label': 'radarr-upgrade'},
         'c': {'label': 'radarr'}, 'd': {'label': 'sonarr-upgrade'}}),
     [('core.queue_top', [['a', 'c']]), ('core.queue_bottom', [['b', 'd']])]),

    # 4. Boundary: no priority-labeled torrents -> NO core.queue_top call at all,
    #    but the bottom move still happens (a fix that always posts queue_top
    #    with an empty list fails this).
    ("no priority torrents -> queue_top skipped, queue_bottom kept",
     lambda: _run_prioritize({'x': {'label': 'radarr-upgrade'}}),
     [('core.queue_bottom', [['x']])]),

    # 5. Boundary: no upgrade-labeled torrents -> NO core.queue_bottom call,
    #    top move still happens (symmetric half of case 4).
    ("no upgrade torrents -> queue_bottom skipped, queue_top kept",
     lambda: _run_prioritize({'p': {'label': 'sonarr'}}),
     [('core.queue_top', [['p']])]),
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
