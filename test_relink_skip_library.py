"""Adversarial fixture for: aw-relink-skip-library

deluge_error_repair's relink search must never relink an errored torrent onto
data it found inside a library root. Such a match is reported as
'library-match' (result['library_match']) instead -- even with relink=True,
and for lowercase, '..' and trailing-slash spellings of the library path.
Non-library matches keep today's behaviour (relink with relink=True, relink
candidate otherwise).
"""
import os
import sys
import json
import tempfile
import importlib.util

ACT = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ['ACTIVITY_LOG'] = ACT

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)
target.ACTIVITY_LOG = ACT

HASH = 'deadbeef'


class Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, torrents):
        self.torrents = torrents
        self.calls = []

    def post(self, url, json=None, timeout=None, **kw):
        m = json['method']
        self.calls.append(m)
        if m == 'core.get_torrents_status':
            return Resp({'result': self.torrents})
        return Resp({'result': None})


class LogRec:
    def _rec(self, msg, *a, **k):
        pass

    info = warning = error = debug = exception = critical = _rec


def run(path_map, found_local, relink=True, dry_run=False, reset=True, runs=1):
    """Run deluge_error_repair for one errored torrent (save_path in Downloads,
    data missing there) whose data the search 'finds' at found_local.
    Returns (moves posted, hashes in library_match, hashes in relink_candidates,
    action string, activity rows mentioning library-match, counts.library_match)."""
    torrents = {HASH: {'name': 'Show.S01E01.1080p', 'state': 'Error',
                       'message': 'Missing or invalid torrent data',
                       'save_path': '/data/Downloads/Complete/tv',
                       'label': 'sonarr', 'progress': 50.0}}
    target.session = FakeSession(torrents)
    target.log = LogRec()
    target.deluge_login = lambda *a, **k: None
    target.DELUGE_PATH_MAP = path_map
    found_deluge = target._local_to_deluge(found_local)
    target._deluge_path_exists = lambda p: p == found_deluge
    target._search_roots = lambda: ['/searchroot']
    target._find_torrent_data = lambda name, roots=None: found_local
    moves = []
    target.move_torrent_storage = lambda h, dest: moves.append((h, dest))
    target.deluge_force_recheck = lambda h: moves.append(('recheck', h))
    target.deluge_pause_torrent = lambda h: None
    if reset:
        target._reported_deluge_errors.clear()
        target._repair_rechecked.clear()
        open(ACT, 'w').close()
    res = None
    for _ in range(runs):
        res = target.deluge_error_repair(dry_run=dry_run, relink=relink)
    lm = res.get('library_match')
    lm_hashes = [e['hash'] for e in lm] if isinstance(lm, list) else None
    rc = [e['hash'] for e in res.get('relink_candidates', [])]
    action = ''
    for e in (lm or []) + res.get('relink_candidates', []):
        action = e.get('action', '')
    with open(ACT) as f:
        rows = [json.loads(l) for l in f if l.strip()]
    lm_rows = sum(1 for r in rows if 'library-match' in r.get('summary', ''))
    return (moves, lm_hashes, rc, action.split(':')[0], lm_rows,
            res.get('counts', {}).get('library_match'))


DEFAULT_MAP = [('/data/Downloads', '/dl'), ('/data/Media/Movies', '/media/movies'),
               ('/data/Media/TV Shows', '/media/tv')]
LOWER_MAP = [('/data/media/movies/', '/media/movies')]
DOTDOT_MAP = [('/data/Downloads/../Media/TV Shows', '/media/tv')]
ASIAN_MAP = [('/data/Media/TV Shows - Asian/', '/media/asian')]
LOOKALIKE_MAP = [('/data/Media/Moviesx', '/media/moviesx')]


CASES = [
    ("default map: data found in /media/tv with relink=True is NOT relinked -> library-match",
     lambda: run(DEFAULT_MAP, '/media/tv/Show/Season 01/Show.S01E01.1080p.mkv', relink=True),
     ([], [HASH], [], 'library-match', 1, 1)),
    ("lowercase deluge library path (trailing slash in map) is a library-match",
     lambda: run(LOWER_MAP, '/media/movies/Film (2020)/Film.mkv', relink=True),
     ([], [HASH], [], 'library-match', 1, 1)),
    ("'..' deluge path that normalises into TV Shows is a library-match",
     lambda: run(DOTDOT_MAP, '/media/tv/Show/Show.S01E01.1080p.mkv', relink=True),
     ([], [HASH], [], 'library-match', 1, 1)),
    ("Asian TV root with relink=False is a library-match, not a relink candidate",
     lambda: run(ASIAN_MAP, '/media/asian/Drama/ep.mkv', relink=False),
     ([], [HASH], [], 'library-match', 1, 1)),
    ("library-match is reported to the activity log only once across two runs",
     lambda: run(DEFAULT_MAP, '/media/movies/Film/Film.mkv', relink=True, runs=2)[4], 1),
    ("dry run: library-match listed, nothing moved, nothing reported",
     lambda: run(DEFAULT_MAP, '/media/movies/Film/Film.mkv', relink=True, dry_run=True),
     ([], [HASH], [], 'library-match', 0, 1)),
    # --- regressions: non-library matches keep today's behaviour ---
    ("Downloads match with relink=True is still relinked",
     lambda: run(DEFAULT_MAP, '/dl/Just4Seeding/Show.S01E01.1080p.mkv', relink=True)[:3],
     ([(HASH, '/data/Downloads/Just4Seeding')], [], [HASH])),
    ("Downloads match with relink=False stays a relink candidate (no move)",
     lambda: run(DEFAULT_MAP, '/dl/Just4Seeding/Show.S01E01.1080p.mkv', relink=False)[:4],
     ([], [], [HASH], 'relink candidate (set DELUGE_RELINK=1 or POST ?relink=1) → /data/Downloads/Just4Seeding')),
    ("look-alike /data/Media/Moviesx is not a library root -> relinked",
     lambda: run(LOOKALIKE_MAP, '/media/moviesx/F/F.mkv', relink=True)[:3],
     ([(HASH, '/data/Media/Moviesx/F')], [], [HASH])),
    ("result always carries an (empty) library_match list and count",
     lambda: run(DEFAULT_MAP, '/dl/x/Show.S01E01.1080p.mkv', relink=False)[1::4],
     ([], 0)),
    ("counts keep every existing bucket plus library_match",
     lambda: counts_for(DEFAULT_MAP, '/media/tv/Show/x.mkv'),
     {'rechecked': 0, 'relink_candidates': 0, 'missing': 0, 'errors': 0,
      'library': 0, 'resumed': 0, 'library_match': 1}),
]


def counts_for(path_map, found_local):
    run(path_map, found_local, relink=True)
    return target.deluge_error_repair(dry_run=True, relink=True).get('counts')


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE")
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
