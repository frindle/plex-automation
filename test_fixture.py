"""Adversarial fixture for: arr-webhook-yearly-upgrade-batches-s9-relabel-count

Proves relabel_radarr_upgrades / relabel_sonarr_upgrades return the exact
integer count of torrents relabeled this call on every code path (busy,
empty-torrents, no-matching-label, and exception), instead of the current
implicit `return None` everywhere. A plausible-but-wrong implementation --
one that stays void, returns a bool instead of the count, or lets the
except-branch propagate instead of returning 0 -- fails at least one case.

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
# '__dict__', so the fixture fails for a reason that has nothing to do with the
# task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch(**kw):
    """Monkeypatch target attributes; returns a restore callable."""
    orig = {k: getattr(target, k) for k in kw}
    for k, v in kw.items():
        setattr(target, k, v)

    def restore():
        for k, v in orig.items():
            setattr(target, k, v)
    return restore


def _radarr_case(torrents, movies_by_id, download_to_movie, raise_on_login=False):
    """Drive relabel_radarr_upgrades() with a fully faked Deluge/Radarr layer."""
    calls = {'set_label': [], 'queue_bottom': []}

    def fake_login():
        if raise_on_login:
            raise RuntimeError('deluge unreachable')

    def fake_get_all_torrents():
        return dict(torrents)

    def fake_get(url, headers=None, params=None, timeout=None):
        if str(url).endswith('/movie'):
            return _Resp(list(movies_by_id.values()))
        if str(url).endswith('/queue'):
            records = [{'downloadId': h, 'movieId': mid} for h, mid in download_to_movie.items()]
            return _Resp({'records': records})
        raise AssertionError('unexpected GET ' + str(url))

    def fake_set_torrent_label(h, label):
        calls['set_label'].append((h, label))

    def fake_ensure_label(label):
        pass

    class _FakeSession:
        def post(self, url, json=None, timeout=None):
            calls['queue_bottom'].append(json)
            return _Resp({})

    restore = _patch(
        deluge_login=fake_login,
        get_all_torrents=fake_get_all_torrents,
        requests=type('R', (), {'get': staticmethod(fake_get)}),
        set_torrent_label=fake_set_torrent_label,
        ensure_label_exists_named=fake_ensure_label,
        session=_FakeSession(),
    )
    try:
        result = target.relabel_radarr_upgrades()
    finally:
        restore()
    return result, calls


def _sonarr_case(torrents, episode_has_file, download_to_episode, raise_on_login=False):
    calls = {'set_label': [], 'queue_bottom': []}

    def fake_login():
        if raise_on_login:
            raise RuntimeError('deluge unreachable')

    def fake_get_all_torrents():
        return dict(torrents)

    def fake_get(url, headers=None, params=None, timeout=None):
        if str(url).endswith('/queue'):
            records = [{'downloadId': h, 'episodeId': eid} for h, eid in download_to_episode.items()]
            return _Resp({'records': records})
        for eid, has_file in episode_has_file.items():
            if str(url).endswith('/episode/{}'.format(eid)):
                return _Resp({'hasFile': has_file})
        raise AssertionError('unexpected GET ' + str(url))

    def fake_set_torrent_label(h, label):
        calls['set_label'].append((h, label))

    def fake_ensure_label(label):
        pass

    class _FakeSession:
        def post(self, url, json=None, timeout=None):
            calls['queue_bottom'].append(json)
            return _Resp({})

    restore = _patch(
        deluge_login=fake_login,
        get_all_torrents=fake_get_all_torrents,
        requests=type('R', (), {'get': staticmethod(fake_get)}),
        set_torrent_label=fake_set_torrent_label,
        ensure_label_exists_named=fake_ensure_label,
        session=_FakeSession(),
    )
    try:
        result = target.relabel_sonarr_upgrades()
    finally:
        restore()
    return result, calls


# --- radarr: 3 radarr-labeled torrents (+1 unrelated label, excluded), 2 of
# the 3 map to movies with hasFile=True -> exactly 2 relabeled.
_RADARR_TORRENTS = {
    'h1': {'label': 'radarr', 'name': 'Movie A'},
    'h2': {'label': 'radarr', 'name': 'Movie B'},
    'h3': {'label': 'radarr', 'name': 'Movie C'},
    'h4': {'label': 'sonarr', 'name': 'Show X'},
}
_RADARR_MOVIES = {
    1: {'id': 1, 'hasFile': True},
    2: {'id': 2, 'hasFile': False},
    3: {'id': 3, 'hasFile': True},
}
_RADARR_DL_TO_MOVIE = {'h1': 1, 'h2': 2, 'h3': 3}

# --- sonarr: 3 sonarr-labeled torrents, 2 episodes have hasFile=True.
_SONARR_TORRENTS = {
    's1': {'label': 'sonarr', 'name': 'Show A S01E01'},
    's2': {'label': 'sonarr', 'name': 'Show B S01E01'},
    's3': {'label': 'sonarr', 'name': 'Show C S01E01'},
}
_SONARR_EPISODES = {101: True, 102: False, 103: True}
_SONARR_DL_TO_EP = {'s1': 101, 's2': 102, 's3': 103}


CASES = [
    ("radarr: 2 of 3 relabeled -> returns int 2, not a bool, not None",
     lambda: (lambda r: (r[0], type(r[0]) is int))(
         _radarr_case(_RADARR_TORRENTS, _RADARR_MOVIES, _RADARR_DL_TO_MOVIE)),
     (2, True)),

    ("radarr: relabel_radarr_upgrades still moves relabeled torrents to queue bottom (regression)",
     lambda: len(_radarr_case(_RADARR_TORRENTS, _RADARR_MOVIES, _RADARR_DL_TO_MOVIE)[1]['queue_bottom']),
     1),

    ("radarr: zero torrents total -> returns int 0, not None, not False",
     lambda: (lambda r: (r[0], type(r[0]) is int))(_radarr_case({}, {}, {})),
     (0, True)),

    ("radarr: torrents present but none labeled 'radarr' -> returns int 0",
     lambda: (lambda r: (r[0], type(r[0]) is int))(
         _radarr_case({'h9': {'label': 'sonarr', 'name': 'x'}}, {}, {})),
     (0, True)),

    ("radarr: exception inside the try block (deluge_login raises) is swallowed and returns int 0, not raised",
     lambda: (lambda r: (r[0], type(r[0]) is int))(
         _radarr_case(_RADARR_TORRENTS, _RADARR_MOVIES, _RADARR_DL_TO_MOVIE, raise_on_login=True)),
     (0, True)),

    ("sonarr: 2 of 3 episodes have hasFile=True -> returns int 2, not a bool, not None",
     lambda: (lambda r: (r[0], type(r[0]) is int))(
         _sonarr_case(_SONARR_TORRENTS, _SONARR_EPISODES, _SONARR_DL_TO_EP)),
     (2, True)),

    ("sonarr: zero sonarr-labeled torrents -> returns int 0",
     lambda: (lambda r: (r[0], type(r[0]) is int))(_sonarr_case({}, {}, {})),
     (0, True)),

    ("sonarr: exception inside the try block (deluge_login raises) is swallowed and returns int 0, not raised",
     lambda: (lambda r: (r[0], type(r[0]) is int))(
         _sonarr_case(_SONARR_TORRENTS, _SONARR_EPISODES, _SONARR_DL_TO_EP, raise_on_login=True)),
     (0, True)),
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
