"""Adversarial fixture for: arr-webhook-recent-upgrade-priority

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

Integration shape: we drive the REAL app functions (relabel_radarr_upgrades /
relabel_sonarr_upgrades / prioritize_normal_torrents) with their external
boundaries faked -- target.session (the Deluge HTTP client) and
target.requests.get (Radarr/Sonarr APIs) are swapped for recorders that return
canned API bodies. We then assert on the ACTUAL Deluge calls made: which label
was set on which torrent, and whether it was swept to core.queue_top or
core.queue_bottom. That is the observable contract of this slice.
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

from datetime import datetime, timezone


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Records every Deluge JSON-RPC call; answers label.get_labels with []
    and core.get_torrents_status with the torrents dict it was given."""

    def __init__(self, torrents=None):
        self.calls = []  # list of (method, params)
        self.torrents = torrents or {}

    def post(self, url, json=None, timeout=None):
        method = (json or {}).get('method')
        params = (json or {}).get('params')
        self.calls.append((method, params))
        if method == 'label.get_labels':
            return _Resp({'result': []})
        if method == 'core.get_torrents_status':
            return _Resp({'result': self.torrents})
        return _Resp({'result': True})


def _install(session):
    """Point the module's Deluge client and API getter at our fakes."""
    target.session = session
    target.deluge_login = lambda: None


def _radarr_get(movies, queue_records):
    def get(url, *a, **k):
        if '/movie' in url:
            return _Resp(movies)
        if '/queue' in url:
            return _Resp({'records': queue_records})
        raise AssertionError('unexpected Radarr GET ' + url)
    return get


def _sonarr_get(queue_records, episodes):
    def get(url, *a, **k):
        if '/episode/' in url:
            ep_id = int(url.rsplit('/', 1)[-1])
            return _Resp(episodes[ep_id])
        if '/queue' in url:
            return _Resp({'records': queue_records})
        raise AssertionError('unexpected Sonarr GET ' + url)
    return get


def _labels_set(session):
    """{torrent_hash: label} from the recorded label.set_torrent calls."""
    out = {}
    for method, params in session.calls:
        if method == 'label.set_torrent':
            h, lbl = params[0], params[1]
            out[h] = lbl
    return out


def _queue_calls(session):
    """{method: [hashes]} for core.queue_top / core.queue_bottom."""
    out = {}
    for method, params in session.calls:
        if method in ('core.queue_top', 'core.queue_bottom'):
            out.setdefault(method, []).extend(params[0])
    return out


NOW_YEAR = datetime.now(timezone.utc).year

# ── Radarr relabel cases ─────────────────────────────────────────────────────

def case_radarr_recent_this_year():
    s = FakeSession({'H1': {'label': 'radarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _radarr_get(
        movies=[{'id': 1, 'hasFile': True, 'year': NOW_YEAR}],
        queue_records=[{'downloadId': 'H1', 'movieId': 1}],
    )
    n = target.relabel_radarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_radarr_recent_last_year():
    s = FakeSession({'H1': {'label': 'radarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _radarr_get(
        movies=[{'id': 1, 'hasFile': True, 'year': NOW_YEAR - 1}],
        queue_records=[{'downloadId': 'H1', 'movieId': 1}],
    )
    n = target.relabel_radarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_radarr_old_year():
    s = FakeSession({'H1': {'label': 'radarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _radarr_get(
        movies=[{'id': 1, 'hasFile': True, 'year': NOW_YEAR - 2}],
        queue_records=[{'downloadId': 'H1', 'movieId': 1}],
    )
    n = target.relabel_radarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_radarr_missing_year():
    s = FakeSession({'H1': {'label': 'radarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _radarr_get(
        movies=[{'id': 1, 'hasFile': True}],  # no year key at all
        queue_records=[{'downloadId': 'H1', 'movieId': 1}],
    )
    n = target.relabel_radarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_radarr_nonnumeric_year():
    s = FakeSession({'H1': {'label': 'radarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _radarr_get(
        movies=[{'id': 1, 'hasFile': True, 'year': 'not-a-year'}],
        queue_records=[{'downloadId': 'H1', 'movieId': 1}],
    )
    n = target.relabel_radarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


# ── Sonarr relabel cases ─────────────────────────────────────────────────────

def case_sonarr_recent_airdate():
    s = FakeSession({'H1': {'label': 'sonarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _sonarr_get(
        queue_records=[{'downloadId': 'H1', 'episodeId': 7}],
        episodes={7: {'hasFile': True, 'airDateUtc': f'{NOW_YEAR}-03-14'}},
    )
    n = target.relabel_sonarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_sonarr_old_airdate():
    s = FakeSession({'H1': {'label': 'sonarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _sonarr_get(
        queue_records=[{'downloadId': 'H1', 'episodeId': 7}],
        episodes={7: {'hasFile': True, 'airDateUtc': f'{NOW_YEAR - 2}-03-14'}},
    )
    n = target.relabel_sonarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_sonarr_airdate_fallback():
    # airDateUtc absent -> must fall back to airDate (recent) and still fast-track.
    s = FakeSession({'H1': {'label': 'sonarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _sonarr_get(
        queue_records=[{'downloadId': 'H1', 'episodeId': 7}],
        episodes={7: {'hasFile': True, 'airDate': f'{NOW_YEAR - 1}-05-02'}},
    )
    n = target.relabel_sonarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


def case_sonarr_no_airdate():
    # No airDateUtc AND no airDate -> not recent -> throttled lane.
    s = FakeSession({'H1': {'label': 'sonarr', 'name': 'x'}})
    _install(s)
    target.requests.get = _sonarr_get(
        queue_records=[{'downloadId': 'H1', 'episodeId': 7}],
        episodes={7: {'hasFile': True}},
    )
    n = target.relabel_sonarr_upgrades()
    got = (n, _labels_set(s), sorted(_queue_calls(s).get('core.queue_top', [])),
           sorted(_queue_calls(s).get('core.queue_bottom', [])))
    return got


# ── prioritize_normal_torrents: the hourly re-enforcement pass ───────────────

def case_prioritize_recent_never_bottomed():
    s = FakeSession({
        'H1': {'label': target.RADARR_UPG_PRIORITY_LABEL},   # recent radarr upgrade
        'H2': {'label': target.SONARR_UPG_PRIORITY_LABEL},   # recent sonarr upgrade
        'H3': {'label': 'radarr'},                            # normal
        'H4': {'label': 'sonarr'},                            # normal
        'H5': {'label': target.RADARR_UPG_LABEL},             # old radarr upgrade
        'H6': {'label': target.SONARR_UPG_LABEL},             # old sonarr upgrade
    })
    _install(s)
    target.prioritize_normal_torrents()
    qc = _queue_calls(s)
    got = (sorted(qc.get('core.queue_top', [])), sorted(qc.get('core.queue_bottom', [])))
    return got


CASES = [
    ("radarr upgrade of a THIS-YEAR movie -> priority label + queue_top, never bottom",
     case_radarr_recent_this_year,
     (1, {'H1': target.RADARR_UPG_PRIORITY_LABEL}, ['H1'], [])),

    ("radarr upgrade of a LAST-YEAR movie (boundary) -> priority label + queue_top",
     case_radarr_recent_last_year,
     (1, {'H1': target.RADARR_UPG_PRIORITY_LABEL}, ['H1'], [])),

    ("radarr upgrade of an OLDER movie (year-2) -> throttled label + queue_bottom only",
     case_radarr_old_year,
     (1, {'H1': target.RADARR_UPG_LABEL}, [], ['H1'])),

    ("radarr movie with NO year field -> not recent -> throttled lane, no crash",
     case_radarr_missing_year,
     (1, {'H1': target.RADARR_UPG_LABEL}, [], ['H1'])),

    ("radarr movie with NON-NUMERIC year -> not recent -> throttled lane, no crash",
     case_radarr_nonnumeric_year,
     (1, {'H1': target.RADARR_UPG_LABEL}, [], ['H1'])),

    ("sonarr upgrade of a RECENT episode (airDateUtc this year) -> priority + queue_top",
     case_sonarr_recent_airdate,
     (1, {'H1': target.SONARR_UPG_PRIORITY_LABEL}, ['H1'], [])),

    ("sonarr upgrade of an OLD episode (airDateUtc year-2) -> throttled + queue_bottom",
     case_sonarr_old_airdate,
     (1, {'H1': target.SONARR_UPG_LABEL}, [], ['H1'])),

    ("sonarr episode with airDateUtc ABSENT but recent airDate -> falls back, fast-tracks",
     case_sonarr_airdate_fallback,
     (1, {'H1': target.SONARR_UPG_PRIORITY_LABEL}, ['H1'], [])),

    ("sonarr episode with NO air date at all -> not recent -> throttled lane",
     case_sonarr_no_airdate,
     (1, {'H1': target.SONARR_UPG_LABEL}, [], ['H1'])),

    ("prioritize_normal_torrents: recent-upgrade labels swept to TOP only; old upgrades to BOTTOM",
     case_prioritize_recent_never_bottomed,
     (['H1', 'H2', 'H3', 'H4'], ['H5', 'H6'])),
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
