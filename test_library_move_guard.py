"""Adversarial fixture for: aw-library-move-guard

move_torrent_storage(hash, dest) and the /torrent-move endpoint must refuse
any dest where is_library_path(dest) is True -- including lowercase, '..'
and trailing-slash spellings -- without ever posting core.move_storage (or
pause/resume) to Deluge. Non-library destinations (including the look-alike
/data/Media/Music and /data/Media/Moviesx) must still move.
"""
import os
import sys
import tempfile
import importlib.util

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)


class Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeSession:
    """Stands in for target.session (the Deluge JSON-RPC session)."""

    def __init__(self):
        self.calls = []
        self.paths = {}

    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        self.calls.append((m, p))
        res = None
        if m == 'core.move_storage':
            for h in p[0]:
                self.paths[h] = p[1]
        elif m == 'core.get_torrents_status':
            res = {h: {'save_path': sp} for h, sp in self.paths.items()}
        return Resp({'result': res, 'error': None})

    def methods(self):
        return [m for m, _ in self.calls]


class LogRec:
    def __init__(self):
        self.lines = []

    def _rec(self, msg, *a, **k):
        self.lines.append(str(msg))

    info = warning = error = debug = exception = critical = _rec


def _fresh():
    fake = FakeSession()
    rec = LogRec()
    target.session = fake
    target.log = rec
    target.deluge_login = lambda *a, **k: None
    return fake, rec


def mts(dest):
    """(return value, move_storage was posted, refusal logged)."""
    fake, rec = _fresh()
    rv = target.move_torrent_storage('abc123', dest)
    return (rv, 'core.move_storage' in fake.methods(),
            any('library-move-refused' in l for l in rec.lines))


def endpoint(moves):
    """POST /torrent-move; returns ([result per move], deluge calls, refusal logged)."""
    fake, rec = _fresh()
    client = target.app.test_client()
    r = client.post('/torrent-move', json={'moves': moves})
    body = r.get_json() or {}
    results = [e.get('result') for e in body.get('results', [])]
    return (results, fake.calls,
            any('library-move-refused' in l for l in rec.lines))


def endpoint_methods_for(moves, h):
    _, calls, _ = endpoint(moves)
    return [m for m, p in calls if p and isinstance(p[0], list) and h in p[0]]


CASES = [
    # --- move_torrent_storage: refusals (tricky spellings) ---
    ("lowercase library path with trailing slash is refused",
     lambda: mts('/data/media/movies/Some Movie (2020)/'), (False, False, True)),
    ("'..' that normalises into TV Shows is refused",
     lambda: mts('/data/Downloads/../Media/TV Shows/Show'), (False, False, True)),
    ("Asian TV root with trailing slash is refused",
     lambda: mts('/data/Media/TV Shows - Asian/'), (False, False, True)),
    ("exact library root is refused",
     lambda: mts('/data/Media/Movies'), (False, False, True)),
    ("upper/mixed-case TV Shows subfolder is refused",
     lambda: mts('/DATA/MEDIA/tv shows/Show/Season 01'), (False, False, True)),
    # --- move_torrent_storage: regressions (must still move, no refusal) ---
    ("seeding dir still moves and returns True",
     lambda: mts('/data/Downloads/Just4Seeding'), (True, True, False)),
    ("/data/Media/Music is not a library root -> moves",
     lambda: mts('/data/Media/Music/Album'), (True, True, False)),
    ("look-alike /data/Media/Moviesx is not a library root -> moves",
     lambda: mts('/data/Media/Moviesx/x'), (True, True, False)),
    ("'..' that escapes the library to Downloads -> moves",
     lambda: mts('/data/Media/Movies/../../Downloads/Complete'), (True, True, False)),
    # --- /torrent-move endpoint ---
    ("endpoint refuses a lowercase/trailing-slash library dest",
     lambda: endpoint([{'hash': 'AAA', 'dest': '/data/media/tv shows/Show/'}])[0],
     ['library-move-refused']),
    ("endpoint posts NOTHING to Deluge for a library dest (no pause/move/resume)",
     lambda: [m for m, _ in endpoint([{'hash': 'aaa', 'dest': '/data/Downloads/../Media/Movies/X'}])[1]],
     []),
    ("endpoint logs library-move-refused",
     lambda: endpoint([{'hash': 'aaa', 'dest': '/data/Media/Movies/X'}])[2], True),
    ("endpoint: library dest refused, non-library dest in same batch still moved + resumed",
     lambda: endpoint([{'hash': 'aaa', 'dest': '/data/Media/TV Shows - Asian/'},
                       {'hash': 'bbb', 'dest': '/data/Downloads/Complete/tv'}])[0],
     ['library-move-refused', 'moved + resumed']),
    ("endpoint: non-library dest posts pause, move_storage, resume for that hash",
     lambda: endpoint_methods_for([{'hash': 'bbb', 'dest': '/data/Media/Music/x'}], 'bbb'),
     ['core.pause_torrent', 'core.move_storage', 'core.resume_torrent']),
    ("endpoint: refused hash gets no Deluge call while the other moves",
     lambda: endpoint_methods_for([{'hash': 'aaa', 'dest': '/data/media/movies'},
                                   {'hash': 'bbb', 'dest': '/data/Downloads/x'}], 'aaa'),
     []),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
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
