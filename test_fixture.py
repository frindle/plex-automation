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


class _FakeLog:
    def __init__(self):
        self.infos = []

    def info(self, msg):
        self.infos.append(msg)

    def error(self, msg):
        pass


def _run_prioritize(torrents):
    """Patch deluge/session/log, run prioritize_normal_torrents, return
    (calls, log_infos): calls is the list of (method, params, id) payloads
    posted to Deluge (id included -- it is a distinct literal per queue
    sweep, 8 for queue_top / 11 for queue_bottom, and observable on the
    wire); log_infos is every message passed to log.info()."""
    calls = []

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            calls.append((json['method'], json['params'], json.get('id')))
            return _FakeResp()

    fake_log = _FakeLog()
    target.deluge_login = lambda: None
    target.get_all_torrents = lambda: torrents
    target.session = FakeSession()
    target.log = fake_log
    target.prioritize_normal_torrents()
    return calls, fake_log.infos


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

    # 3. The priority_hashes list is what reaches core.queue_top (id 8) --
    #    and the upgrade hashes still go to core.queue_bottom (id 11),
    #    regression half. The distinct ids are observable and asserted so a
    #    swapped/renumbered id fails this.
    ("prioritize sends priority_hashes to core.queue_top (id 8), upgrades to core.queue_bottom (id 11)",
     lambda: _run_prioritize({
         'a': {'label': 'sonarr'}, 'b': {'label': 'radarr-upgrade'},
         'c': {'label': 'radarr'}, 'd': {'label': 'sonarr-upgrade'}})[0],
     [('core.queue_top', [['a', 'c']], 8), ('core.queue_bottom', [['b', 'd']], 11)]),

    # 4. Boundary: no priority-labeled torrents -> NO core.queue_top call at
    #    all, but the bottom move still happens (a fix that always posts
    #    queue_top with an empty list fails this) -- AND the "nothing to
    #    reprioritize" log must NOT fire, since the bottom half did run (an
    #    `and` mutated to `or` on that guard would fire it here).
    ("no priority torrents -> queue_top skipped, queue_bottom kept, no idle log",
     lambda: (lambda calls, infos: (calls, 'No torrents to reprioritize' in infos))
             (*_run_prioritize({'x': {'label': 'radarr-upgrade'}})),
     ([('core.queue_bottom', [['x']], 11)], False)),

    # 5. Boundary: no upgrade-labeled torrents -> NO core.queue_bottom call,
    #    top move still happens (symmetric half of case 4), no idle log.
    ("no upgrade torrents -> queue_bottom skipped, queue_top kept, no idle log",
     lambda: (lambda calls, infos: (calls, 'No torrents to reprioritize' in infos))
             (*_run_prioritize({'p': {'label': 'sonarr'}})),
     ([('core.queue_top', [['p']], 8)], False)),

    # 6. Neither priority- nor upgrade-labeled torrents present -> BOTH
    #    Deluge calls are skipped AND the "nothing to reprioritize" idle log
    #    DOES fire. This is the only case where both halves of the `if not
    #    priority_hashes and not bottom_hashes:` guard are True, so it is the
    #    one case that pins `and` (not `or`) and rules out the guard being
    #    forced to always-False.
    ("neither priority nor upgrade torrents -> no Deluge calls, idle log fires",
     lambda: (lambda calls, infos: (calls, 'No torrents to reprioritize' in infos))
             (*_run_prioritize({'z': {'label': 'unrelated'}})),
     ([], True)),
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
