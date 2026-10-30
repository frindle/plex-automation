"""Adversarial fixture for: aw-library-audit (library_audit.py, a new module).

The audit is read-only: it gets a torrents dict and a notify callable, and
must flag Error/missing-data torrents and arr-labelled torrents seeding from
under /data/Media -- with lowercase, '..', trailing-slash and look-alike paths
-- and push each torrent at most once.
"""
import sys
import importlib.util

import os
spec = importlib.util.spec_from_file_location(
    "target", os.path.join(os.path.dirname(os.path.abspath(__file__)), 'library_audit.py'))
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)


def T(state='Seeding', label='sonarr', save_path='/data/Downloads/Complete', message='', name='Rel'):
    return {'name': name, 'state': state, 'label': label, 'save_path': save_path, 'message': message}


def reasons(t):
    return target.audit_reasons(t)


def run(torrents, reported=None, dry_run=False):
    sent = []
    rep = set() if reported is None else reported
    r = target.library_seed_audit(torrents, lambda title, msg: sent.append((title, msg)), rep,
                                  dry_run=dry_run)
    return r, sent, rep


def c_once_per_torrent():
    torrents = {'h1': T(state='Error', name='A'), 'h2': T(save_path='/data/Media/Movies/B', label='radarr', name='B')}
    rep = set()
    r1, s1, _ = run(torrents, rep)
    r2, s2, _ = run(torrents, rep)
    return r1['notified'], len(s1), r2['notified'], s2, sorted(rep), len(r2['flagged'])


def c_dry_run():
    torrents = {'h1': T(state='Error')}
    r, sent, rep = run(torrents, dry_run=True)
    return [f['hash'] for f in r['flagged']], sent, rep, r['notified']


def c_message_format():
    _, sent, _ = run({'h1': T(state='Error', label='sonarr', save_path='/data/Media/TV Shows/S', name='Show.S01')})
    return sent


def c_bulk_one_summary():
    # first tick over a big backlog: ONE push, 10 names, rest summarised
    torrents = {'h%02d' % i: T(state='Error', name='N%02d' % i) for i in range(12)}
    torrents['lib'] = T(save_path='/data/Media/Movies/X', label='radarr', name='Lib')
    rep = set()
    r, sent, _ = run(torrents, rep)
    lines = sent[0][1].split('\n')
    return (len(sent), sent[0][0], lines[0], lines[1], len(lines), lines[-1],
            len(r['notified']), len(rep))


def c_only_new_announced():
    rep = {'h1'}
    r, sent, _ = run({'h1': T(state='Error', name='Old'), 'h2': T(state='Error', name='New')}, rep)
    return r['notified'], sent, sorted(rep)


def c_none_new_no_push():
    r, sent, _ = run({'h1': T(state='Error')}, {'h1'})
    return r['notified'], sent


def c_counts_and_entry():
    torrents = {
        'a': T(state='Error'),
        'b': T(save_path='/data/Media/TV Shows/X', label='sonarr-upgrade'),
        'c': T(state='Error', save_path='/data/media/movies/Y', label='radarr'),
        'd': T(),
    }
    r, _, _ = run(torrents)
    return r['counts'], [f['hash'] for f in r['flagged']], r['flagged'][1]


def c_fetch():
    calls = []

    def rpc(m, p):
        calls.append((m, p))
        return {'h': {'state': 'Seeding'}}
    got = target.fetch_torrents(rpc)
    none = target.fetch_torrents(lambda m, p: None)
    return got, calls, none


CASES = [
    ("state Error -> error", lambda: reasons(T(state='Error')), ['error']),
    ("missing-data message (any case) -> error even when Paused",
     lambda: reasons(T(state='Paused', message='Missing or invalid torrent data!')), ['error']),
    ("missing-data lowercase message", lambda: reasons(T(message='missing or invalid torrent data')), ['error']),
    ("sonarr label under /data/Media/TV Shows -> flagged",
     lambda: reasons(T(save_path='/data/Media/TV Shows/Show/Season 01')), ['arr-label-in-library']),
    ("radarr-upgrade, lowercase path w/ trailing slash -> flagged",
     lambda: reasons(T(label='radarr-upgrade', save_path='/data/media/movies/')), ['arr-label-in-library']),
    ("upper-case label and path -> flagged",
     lambda: reasons(T(label='Sonarr-Upgrade-Recent', save_path='/DATA/MEDIA/TV Shows - Asian')),
     ['arr-label-in-library']),
    ("bare /data/Media/ root -> flagged", lambda: reasons(T(save_path='/data/Media/')), ['arr-label-in-library']),
    ("'..' into the library -> flagged",
     lambda: reasons(T(save_path='/data/Downloads/../Media/Movies/X')), ['arr-label-in-library']),
    ("'..' out of the library -> not flagged",
     lambda: reasons(T(save_path='/data/Media/../Downloads/Complete')), []),
    ("look-alike /data/Mediax -> not flagged", lambda: reasons(T(save_path='/data/Mediax/TV')), []),
    ("library-seed label in library is intentional -> not flagged",
     lambda: reasons(T(label='library-seed', save_path='/data/Media/Movies/X')), []),
    ("unlabelled torrent in library -> not flagged",
     lambda: reasons(T(label='', save_path='/data/Media/Movies/X')), []),
    ("healthy arr torrent in Downloads -> not flagged", lambda: reasons(T()), []),
    ("both reasons, fixed order",
     lambda: reasons(T(state='Error', label='radarr', save_path='/data/Media/Movies/X')),
     ['error', 'arr-label-in-library']),
    ("arr label with no save_path -> not flagged",
     lambda: (reasons(T(save_path=None)), reasons(T(save_path=''))), ([], [])),
    ("dry_run defaults to False (notifies)",
     lambda: target.library_seed_audit({'h': T(state='Error')}, lambda a, b: None, set())['notified'],
     ['h']),
    ("missing fields / None label / None message don't raise",
     lambda: reasons({'label': None, 'message': None, 'save_path': None}), []),
    ("one summary push per run, never re-announced", c_once_per_torrent,
     (['h1', 'h2'], 1, [], [], ['h1', 'h2'], 2)),
    ("backlog of 13: one push, 10 names, '... and 3 more'", c_bulk_one_summary,
     (1, 'Library seed audit: 13 newly flagged', 'error 12, arr-label-in-library 1',
      '- N00: error', 12, '... and 3 more', 13, 13)),
    ("only newly flagged torrents are in the push", c_only_new_announced,
     (['h2'], [('Library seed audit: 1 newly flagged', 'error 1, arr-label-in-library 0\n- New: error')],
      ['h1', 'h2'])),
    ("nothing new -> no push at all", c_none_new_no_push, ([], [])),
    ("dry run flags but never notifies or records", c_dry_run, (['h1'], [], set(), [])),
    ("notify title/message format", c_message_format,
     [('Library seed audit: 1 newly flagged',
       'error 1, arr-label-in-library 1\n- Show.S01: error, arr-label-in-library')]),
    ("counts, sorted hashes, entry shape", c_counts_and_entry,
     ({'error': 2, 'arr-label-in-library': 2}, ['a', 'b', 'c'],
      {'hash': 'b', 'name': 'Rel', 'label': 'sonarr-upgrade', 'save_path': '/data/Media/TV Shows/X',
       'state': 'Seeding', 'reasons': ['arr-label-in-library']})),
    ("fetch_torrents: one read-only call, None -> {}", c_fetch,
     ({'h': {'state': 'Seeding'}},
      [('core.get_torrents_status', [{}, ['name', 'state', 'message', 'save_path', 'label']])], {})),
]


def main():
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


def test_all_cases():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
