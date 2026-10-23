"""Tests for library-seed guard in supersede_torrent and remove_torrent."""

import os, sys, json, tempfile, requests

ACT = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ['ACTIVITY_LOG'] = ACT          # BEFORE the import: record_activity appends JSON lines here
aw = __import__('arr-webhook')


class Resp:
    """Tiny response wrapper for the fake."""
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload
    def raise_for_status(self):
        pass


class FakeDeluge:
    def __init__(self, torrents):
        self.torrents, self.calls = torrents, []
    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        self.calls.append((m, p))
        t = self.torrents
        if m == 'core.get_torrents_status':
            ids = p[0].get('id') or list(t)
            res = {h: {f: t[h][f] for f in p[1] if f in t[h]} for h in ids if h in t}
        elif m == 'core.get_torrent_status':
            res = {f: t.get(p[0], {})[f] for f in p[1] if f in t.get(p[0], {})}
        elif m == 'label.get_labels':
            res = []
        elif m == 'core.move_storage':          # a real move updates save_path
            for h in p[0]:
                t.setdefault(h, {})['save_path'] = p[1]
            res = None
        else:
            res = None
        return Resp({'result': res})


FAKE = None
def _post(self, url, json=None, timeout=None, **kw):   # replaces requests.Session.post
    return FAKE.post(url, json=json, timeout=timeout, **kw)
requests.Session.post = _post


def setup(torrents):          # call at the start of every test
    global FAKE
    FAKE = FakeDeluge(torrents)
    open(ACT, 'w').close()    # empty the activity log
    return FAKE


def acts():                   # categories recorded so far, in order
    with open(ACT) as f:
        return [json.loads(l)['category'] for l in f if l.strip()]


def called(method):           # params of every call to one Deluge method, in order
    return [p for m, p in FAKE.calls if m == method]


# ── is_library_path tests (section 1 of TASK.md) ──────────────────────

def test_is_library_path_root():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/Movies') is True

def test_is_library_path_root_trailing_slash():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/Movies/') is True

def test_is_library_path_child():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/Movies/Dune (2021)') is True

def test_is_library_path_tv_shows():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/TV Shows/Foo/Season 01') is True

def test_is_library_path_asian():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/TV Shows - Asian/Bar/Season 2') is True

def test_is_library_path_case_insensitive():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/media/tv shows/Foo') is True

def test_is_library_path_normpath():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Downloads/../Media/Movies/X') is True

def test_is_library_path_downloads_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Downloads/Complete') is False

def test_is_library_path_seeding_dir_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Downloads/Just4Seeding/X') is False

def test_is_library_path_prefix_no_match():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/Movies2/X') is False

def test_is_library_path_exact_root_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media') is False

def test_is_library_path_music_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('/data/Media/Music/X') is False

def test_is_library_path_relative_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('data/Media/Movies/X') is False

def test_is_library_path_empty_string_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path('') is False

def test_is_library_path_none_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path(None) is False

def test_is_library_path_int_false():
    aw = __import__('arr-webhook')
    assert aw.is_library_path(123) is False


# ── remove_torrent tests ───────────────────────────────────────────────

def test_remove_library_seed_info_none():
    """remove_torrent library-seed, info=None, fixture save_path under library."""
    setup({'H': {'save_path': '/data/Media/TV Shows/Foo/Season 01',
                 'seeding_time': 60*86400,
                 'tracker_status': 'Error: Unregistered torrent'}})
    aw.remove_torrent('H')
    removes = called('core.remove_torrent')
    assert removes == [['H', False]]
    assert acts() == ['library-guard']
    get_cs = called('core.get_torrent_status')
    assert get_cs == [['H', ['tracker_status', 'seeding_time', 'save_path']]]

def test_remove_library_seed_caller_info_no_save_path():
    """remove_torrent library-seed, caller passes info without save_path."""
    setup({'H': {'save_path': '/data/Media/Movies/Dune (2021)',
                 'seeding_time': 60*86400,
                 'tracker_status': 'Error: Unregistered torrent'}})
    caller_info = {'seeding_time': 60*86400, 'tracker_status': 'Error: Unregistered torrent'}
    orig = dict(caller_info)
    aw.remove_torrent('H', info=caller_info)
    removes = called('core.remove_torrent')
    assert removes == [['H', False]]
    assert acts() == ['library-guard']
    # Caller's dict must NOT be mutated
    assert caller_info == orig

def test_remove_library_seed_caller_info_has_save_path():
    """remove_torrent library-seed, caller info already has save_path."""
    setup({'H': {'save_path': '/data/Media/Movies/X (2020)',
                 'seeding_time': 60*86400,
                 'tracker_status': 'Error: Unregistered torrent'}})
    caller_info = {'save_path': '/data/Media/Movies/X (2020)',
                   'seeding_time': 60*86400,
                   'tracker_status': 'Error: Unregistered torrent'}
    aw.remove_torrent('H', info=caller_info)
    removes = called('core.remove_torrent')
    assert removes == [['H', False]]
    get_cs = called('core.get_torrent_status')
    assert get_cs == []
    get_ts = called('core.get_torrents_status')
    assert get_ts == []

def test_remove_downloads_info_none():
    """remove_torrent Downloads torrent, info=None, seeding_time high enough."""
    setup({'H': {'save_path': '/data/Downloads/Complete',
                 'seeding_time': 60*86400,
                 'tracker_status': 'Error: Unregistered torrent'}})
    aw.remove_torrent('H')
    removes = called('core.remove_torrent')
    assert removes == [['H', True]]
    assert acts() == []

def test_remove_downloads_low_seed_time():
    """remove_torrent Downloads torrent, seeded 1 day (below threshold)."""
    setup({'H': {'save_path': '/data/Downloads/Complete',
                 'seeding_time': 86400,
                 'tracker_status': 'Error: Unregistered torrent'}})
    aw.remove_torrent('H')
    removes = called('core.remove_torrent')
    assert removes == [['H', False]]
    assert acts() == ['hnr-guard']

def test_remove_library_seed_remove_data_false():
    """remove_torrent with remove_data=False on a library path."""
    setup({'H': {'save_path': '/data/Media/Movies/X (2020)'}})
    aw.remove_torrent('H', remove_data=False)
    removes = called('core.remove_torrent')
    assert removes == [['H', False]]
    assert acts() == []
    get_cs = called('core.get_torrent_status')
    assert get_cs == []
    get_ts = called('core.get_torrents_status')
    assert get_ts == []


# ── supersede_torrent tests ────────────────────────────────────────────

def test_supersede_library_seed_complete():
    """supersede_torrent library-seed complete torrent."""
    setup({'H': {'save_path': '/data/Media/TV Shows/Foo/Season 01',
                 'state': 'Seeding', 'progress': 100.0, 'total_done': 5000}})
    aw.supersede_torrent('H')
    labels = called('label.set_torrent')
    assert labels == [['H', aw.SUPERSEDED_LABEL]]
    moves = called('core.move_storage')
    assert moves == []
    assert acts() == ['supersede-library-seed']
    resume = called('core.resume_torrent')
    assert resume == []
    opts = called('core.set_torrent_options')
    assert opts == []
    removes = called('core.remove_torrent')
    assert removes == []

def test_supersede_library_seed_incomplete():
    """supersede_torrent library-seed incomplete torrent."""
    setup({'H': {'save_path': '/data/Media/Movies/Dune (2021)',
                 'state': 'Paused', 'progress': 40.0, 'total_done': 100}})
    aw.supersede_torrent('H')
    moves = called('core.move_storage')
    assert moves == []
    assert acts() == ['supersede-library-seed']
    resume = called('core.resume_torrent')
    assert resume == []
    opts = called('core.set_torrent_options')
    assert opts == []

def test_supersede_downloads_complete():
    """supersede_torrent Downloads torrent, complete."""
    setup({'H': {'save_path': '/data/Downloads/Complete',
                 'state': 'Seeding', 'progress': 100.0, 'total_done': 5000}})
    aw.supersede_torrent('H')
    moves = called('core.move_storage')
    assert moves == [[['H'], aw.SEEDING_DIR]]
    assert acts() == []

def test_supersede_no_save_path_key():
    """supersede_torrent with NO save_path key in fixture."""
    setup({'H': {'state': 'Seeding', 'progress': 100.0, 'total_done': 5000}})
    aw.supersede_torrent('H')
    moves = called('core.move_storage')
    assert moves == [[['H'], aw.SEEDING_DIR]]
    assert acts() == []

def test_supersede_downloads_incomplete():
    """supersede_torrent Downloads torrent, incomplete (inflight)."""
    setup({'H': {'save_path': '/data/Downloads/Complete',
                 'state': 'Paused', 'progress': 40.0, 'total_done': 100}})
    aw.supersede_torrent('H')
    moves = called('core.move_storage')
    assert moves == []
    resume = called('core.resume_torrent')
    assert resume == [[['H']]]
    assert acts() == ['supersede-inflight-released']


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and k.startswith('test_')]
    failed = []
    for t in tests:
        try:
            t()
            print(f'OK {t.__name__}')
        except Exception as e:
            print(f'FAIL {t.__name__}: {e}')
            failed.append(t.__name__)
    if failed:
        print(f'\n{len(failed)} test(s) FAILED:')
        for n in failed:
            print(f'  - {n}')
        sys.exit(1)
    else:
        print('ALL PASS')
