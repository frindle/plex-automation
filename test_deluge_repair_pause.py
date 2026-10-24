"""Tests for pause-then-recheck and library torrent guard in deluge_error_repair."""

import os
import sys
import json
import tempfile
import requests

ACT = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
os.environ['ACTIVITY_LOG'] = ACT
aw = __import__('arr-webhook')


class Resp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


MUTATIONS = ('core.pause_torrent', 'core.force_recheck', 'core.resume_torrent',
             'core.move_storage', 'core.remove_torrent', 'label.set_torrent')


class FakeDeluge:
    def __init__(self, torrents, paths=(), fail=()):
        self.torrents, self.paths, self.fail, self.calls = torrents, set(paths), set(fail), []
    def post(self, url, json=None, timeout=None, **kw):
        m, p = json['method'], json.get('params')
        self.calls.append((m, p))
        if m in self.fail:
            raise requests.ConnectionError(f'{m} failed')
        t = self.torrents
        if m == 'auth.login':
            res = True
        elif m == 'core.get_torrents_status':
            ids = p[0].get('id') or list(t)
            res = {h: {f: t[h][f] for f in p[1] if f in t[h]} for h in ids if h in t}
        elif m == 'core.get_path_size':
            res = 100 if p[0] in self.paths else -1
        else:
            res = None
        return Resp({'result': res})


FAKE = None
def _post(self, url, json=None, timeout=None, **kw):
    return FAKE.post(url, json=json, timeout=timeout, **kw)
requests.Session.post = _post


def setup(torrents, paths=(), fail=()):
    global FAKE
    FAKE = FakeDeluge(torrents, paths, fail)
    open(ACT, 'w').close()
    return FAKE


def acts():
    with open(ACT) as f:
        return [json.loads(line)['category'] for line in f if line.strip()]


def muts():
    return [(m, p) for m, p in FAKE.calls if m in MUTATIONS]


def reset_calls():
    FAKE.calls.clear()
    open(ACT, 'w').close()


def main():
    results = {}
    tests = [test_library_error_torrent,
             test_library_error_dry_run,
             test_library_second_pass_quiet,
             test_download_present,
             test_resume_after_100,
             test_partial_stays_paused,
             test_still_checking_then_resume,
             test_untouched_paused_not_resumed,
             test_pause_failure_blocks_recheck,
             test_download_dry_run,
             test_resume_pass_dry_run,
             ]
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            results[name] = 'pass'
            print(f'OK {name}')
        except (AssertionError, Exception) as e:
            import traceback
            traceback.print_exc()
            results[name] = f'FAIL {name}: {type(e).__name__}: {e}'
            print(results[name])
    passed = sum(1 for v in results.values() if v == 'pass')
    total = len(results)
    if passed == total:
        print(f'ALL PASS ({total}/{total})')
    else:
        print(f'{passed}/{total} passed')
        sys.exit(1)


# Case 1: Library Error torrent (pause only, no recheck)
def test_library_error_torrent():
    setup({'L1': {'name': 'Show.S01', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Media/TV Shows/Show/Season 01', 'label': 'sonarr',
                  'progress': 40.0}}, paths={'/data/Media/TV Shows/Show/Season 01'})
    r = aw.deluge_error_repair()
    assert muts() == [('core.pause_torrent', [['L1']])]
    assert acts() == ['library-error']
    assert [e['hash'] for e in r['library']] == ['L1']
    assert r['rechecked'] == []
    assert [p for m, p in FAKE.calls if m == 'core.get_path_size'] == []


# Case 2: Library Error torrent dry run (pause not issued, library listed)
def test_library_error_dry_run():
    setup({'L2': {'name': 'Dune.2021', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Media/Movies/Dune (2021)', 'label': 'radarr',
                  'progress': 50.0}}, paths={'/data/Media/Movies/Dune (2021)'})
    r = aw.deluge_error_repair(dry_run=True)
    assert muts() == []
    assert acts() == []
    assert [e['hash'] for e in r['library']] == ['L2']


# Case 3: Library torrent, second pass is quiet (reported once only)
def test_library_second_pass_quiet():
    setup({'L3': {'name': 'Asian.Show.S01', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Media/TV Shows - Asian/Show/Season 01', 'label': 'sonarr',
                  'progress': 30.0}})
    r = aw.deluge_error_repair()
    assert muts() == [('core.pause_torrent', [['L3']])]
    assert [e['hash'] for e in r['library']] == ['L3']
    reset_calls()
    # Second pass: torrent is now Paused (not Error), so no Error handling,
    # but it's in _repair_rechecked? No — library torrents are never added there.
    # The torrent state is still 'Error' though, so it will be processed again.
    # But _report_once should suppress the second report.
    aw.deluge_error_repair()
    assert muts() == [('core.pause_torrent', [['L3']])]
    assert acts() == []  # reported once only
    assert [p for m, p in FAKE.calls if m == 'core.force_recheck'] == []


# Case 4: Downloads Error torrent, data present (pause + recheck)
def test_download_present():
    setup({'D1': {'name': 'Movie.2020', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Movie.2020'})
    r = aw.deluge_error_repair()
    assert muts() == [('core.pause_torrent', [['D1']]), ('core.force_recheck', [['D1']])]
    assert [e['hash'] for e in r['rechecked']] == ['D1']
    assert acts() == []


# Case 5: Resume after 100%
def test_resume_after_100():
    setup({'D2': {'name': 'Series.S01', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Series.S01'})
    r = aw.deluge_error_repair()
    assert [e['hash'] for e in r['rechecked']] == ['D2']
    # Now simulate Deluge finishing the recheck
    FAKE.torrents['D2']['state'] = 'Paused'
    FAKE.torrents['D2']['progress'] = 100.0
    reset_calls()
    r2 = aw.deluge_error_repair()
    assert muts() == [('core.resume_torrent', [['D2']])]
    assert acts() == ['deluge-repair-resumed']
    assert [e['hash'] for e in r2['resumed']] == ['D2']
    reset_calls()
    # Third pass: torrent is gone from _repair_rechecked, nothing happens
    aw.deluge_error_repair()
    assert muts() == []


# Case 6: Partial recheck stays paused
def test_partial_stays_paused():
    setup({'D3': {'name': 'Film.2019', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Film.2019'})
    r = aw.deluge_error_repair()
    assert [e['hash'] for e in r['rechecked']] == ['D3']
    # Simulate partial completion
    FAKE.torrents['D3']['state'] = 'Paused'
    FAKE.torrents['D3']['progress'] = 60.0
    reset_calls()
    r2 = aw.deluge_error_repair()
    assert muts() == []
    assert acts() == ['deluge-error']
    assert r2['resumed'] == []


# Case 7: Still checking then resume
def test_still_checking_then_resume():
    setup({'D4': {'name': 'Doc.S01', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Doc.S01'})
    r = aw.deluge_error_repair()
    assert [e['hash'] for e in r['rechecked']] == ['D4']
    # Simulate still checking
    FAKE.torrents['D4']['state'] = 'Checking'
    FAKE.torrents['D4']['progress'] = 70.0
    reset_calls()
    aw.deluge_error_repair()
    assert muts() == []
    # Now it finishes checking and becomes Paused at 100%
    FAKE.torrents['D4']['state'] = 'Paused'
    FAKE.torrents['D4']['progress'] = 100.0
    reset_calls()
    aw.deluge_error_repair()
    assert muts() == [('core.resume_torrent', [['D4']])]


# Case 8: A torrent this function never touched is never resumed
def test_untouched_paused_not_resumed():
    setup({'P1': {'name': 'Old.Show.S01', 'state': 'Paused', 'message': '',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 100.0}})
    r = aw.deluge_error_repair()
    assert muts() == []
    assert r['resumed'] == []


# Case 9: Pause failure blocks the recheck
def test_pause_failure_blocks_recheck():
    setup({'D5': {'name': 'Bad.Movie', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Bad.Movie'},
          fail={'core.pause_torrent'})
    r = aw.deluge_error_repair()
    assert [m for m, p in FAKE.calls if m == 'core.force_recheck'] == []
    assert [e['hash'] for e in r['errors']] == ['D5']
    assert r['rechecked'] == []


# Case 10: Downloads Error torrent dry run (no mutations, listed as rechecked)
def test_download_dry_run():
    setup({'D6': {'name': 'Dry.Movie', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Dry.Movie'})
    r = aw.deluge_error_repair(dry_run=True)
    assert muts() == []
    assert [e['hash'] for e in r['rechecked']] == ['D6']


# Case 11: a dry run never resumes, records nothing, and keeps the hash for the real pass
def test_resume_pass_dry_run():
    setup({'D7': {'name': 'Dry.Resume', 'state': 'Error', 'message': 'file error',
                  'save_path': '/data/Downloads/Complete', 'label': '',
                  'progress': 40.0}}, paths={'/data/Downloads/Complete/Dry.Resume'})
    aw.deluge_error_repair()
    FAKE.torrents['D7']['state'] = 'Paused'
    FAKE.torrents['D7']['progress'] = 100.0
    reset_calls()
    r2 = aw.deluge_error_repair(dry_run=True)
    assert muts() == []
    assert acts() == []
    assert r2['resumed'] == []
    reset_calls()
    r3 = aw.deluge_error_repair()
    assert muts() == [('core.resume_torrent', [['D7']])]
    assert [e['hash'] for e in r3['resumed']] == ['D7']


if __name__ == '__main__':
    main()
