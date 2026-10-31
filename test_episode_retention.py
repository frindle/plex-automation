"""Adversarial fixture for: aw-episode-retention (episode_retention.py, new module).

Single-episode Sonarr torrents go after SEED_DAYS; season packs, ambiguous
torrents and movies stay. Data is deleted only when every file is outside
/data/Media (lowercase/'..'/trailing slash/look-alike paths) AND the library
holds the episode. Dry-run default, paced, paused by any Error torrent.
"""
import sys
import importlib.util

import os
spec = importlib.util.spec_from_file_location(
    "target", os.path.join(os.path.dirname(os.path.abspath(__file__)), 'episode_retention.py'))
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

DAY = 86400
APPLY = {'EPISODE_RETENTION_APPLY': '1'}


def EP(name='Show.S01E05.1080p.WEB', label='sonarr', days=30, progress=100.0, state='Seeding',
       save_path='/data/Downloads/Complete', files=None, seconds=None):
    if files is None:
        files = [{'path': name + '/' + name + '.mkv'}, {'path': name + '/' + name + '.nfo'}]
    return {'name': name, 'label': label, 'state': state, 'progress': progress,
            'seeding_time': seconds if seconds is not None else days * DAY,
            'save_path': save_path, 'files': files}


def PACK(name='Show.S01.1080p.WEB', **kw):
    files = [{'path': '%s/Show.S01E%02d.mkv' % (name, i)} for i in range(1, 9)]
    return EP(name=name, files=files, **kw)


def run(torrents, env=None, has=lambda t: True, boom=None):
    calls = []

    def remove(h, remove_data):
        if boom and h == boom:
            raise RuntimeError('deluge down')
        calls.append((h, remove_data))
    r = target.episode_retention(torrents, remove, has, env=APPLY if env is None else env)
    return r, calls


def removed(torrents, **kw):
    return run(torrents, **kw)[1]


def c_kept_counts():
    r, calls = run({'a': PACK(), 'b': EP(label='radarr', name='Film.2020.S01E01'), 'c': EP(label=''),
                    'd': EP(label='library-seeds'), 'e': EP(progress=99.5), 'f': EP(days=3), 'g': EP()})
    return r['kept'], calls, [e['hash'] for e in r['removed']]


def c_dry_run_default():
    r, calls = run({'a': EP(), 'b': EP(save_path='/data/Media/TV Shows/Show')}, env={})
    return r['dry_run'], calls, r['planned'], r['removed']


def c_cap():
    torrents = {'e%d' % i: EP() for i in range(7)}
    r, calls = run(torrents)
    return [h for h, _ in calls], r['capped'], r['limit']


def c_cap_env():
    torrents = {'e%d' % i: EP() for i in range(7)}
    r, calls = run(torrents, env={'EPISODE_RETENTION_APPLY': '1', 'EPISODE_RETENTION_PER_RUN': '2'})
    return [h for h, _ in calls], r['capped']


def c_hard_max():
    torrents = {'e%02d' % i: EP() for i in range(30)}
    r, calls = run(torrents, env={'EPISODE_RETENTION_APPLY': '1', 'EPISODE_RETENTION_PER_RUN': '100'})
    return len(calls), r['limit'], r['capped']


def c_paused():
    r, calls = run({'a': EP(), 'z': PACK(state='Error')})
    return r['paused'], calls, r['removed']


def c_remove_raises():
    r, calls = run({'a': EP(), 'b': EP(), 'c': EP()}, boom='b')
    return calls, [e['hash'] for e in r['removed']], r['errors']


def c_has_raises():
    def has(t):
        raise RuntimeError('sonarr down')
    return removed({'a': EP()}, has=has)


def c_not_capped_when_under():
    r, _ = run({'a': EP(), 'b': EP()})
    return r['capped'], r['paused'], r['errors'], r['dry_run']


CASES = [
    ("season pack kept", lambda: removed({'p': PACK()}), []),
    ("episode < 21 days kept", lambda: removed({'e': EP(days=20)}), []),
    ("episode one second short of 21d kept", lambda: removed({'e': EP(seconds=21 * DAY - 1)}), []),
    ("episode exactly 21d removed, data deleted (outside library, library has it)",
     lambda: removed({'e': EP(seconds=21 * DAY)}), [('e', True)]),
    ("episode >= 21d removed", lambda: removed({'e': EP(days=40)}), [('e', True)]),
    ("movie untouched even with SxxEyy name",
     lambda: removed({'m': EP(label='radarr-upgrade', name='Film.S01E01')}), []),
    ("ambiguous: no files list -> season pack, kept", lambda: removed({'x': EP(files=[])}), []),
    ("ambiguous: two video files -> kept",
     lambda: removed({'x': EP(files=[{'path': 'a/Show.S01E05.mkv'}, {'path': 'a/Show.S01E06.mkv'}])}), []),
    ("ambiguous: episode name but file lacks SxxEyy -> kept",
     lambda: removed({'x': EP(files=[{'path': 'a/show-105.mkv'}])}), []),
    ("ambiguous: season name, one episode file -> kept",
     lambda: removed({'x': EP(name='Show.S01.1080p', files=[{'path': 'a/Show.S01E01.mkv'}])}), []),
    ("lowercase name/ext still an episode",
     lambda: removed({'e': EP(name='show.s02e10.720p', files=[{'path': 'show.s02e10.720p.MKV'}])}),
     [('e', True)]),
    ("sonarr-upgrade-recent label counts as sonarr",
     lambda: removed({'e': EP(label='Sonarr-Upgrade-Recent')}), [('e', True)]),
    ("unlabelled / non-sonarr labels never touched",
     lambda: removed({'a': EP(label=''), 'b': EP(label='library-seeds'), 'c': EP(label=None)}), []),
    ("incomplete episode kept", lambda: removed({'e': EP(progress=99.9)}), []),
    ("missing progress / seeding_time -> kept",
     lambda: removed({'a': EP(progress=None), 'b': dict(EP(), seeding_time=None)}), []),
    ("library-path episode removed with remove_data=False",
     lambda: removed({'e': EP(save_path='/data/Media/TV Shows/Show/Season 01')}), [('e', False)]),
    ("lowercase library path w/ trailing slash -> keep data",
     lambda: removed({'e': EP(save_path='/data/media/tv shows/')}), [('e', False)]),
    ("'..' into library -> keep data",
     lambda: removed({'e': EP(save_path='/data/Downloads/../Media/TV Shows')}), [('e', False)]),
    ("file path '..' into library -> keep data",
     lambda: removed({'e': EP(files=[{'path': '../../Media/TV Shows/Show.S01E05.mkv'}],
                              name='Show.S01E05', save_path='/data/Downloads/Complete')}), [('e', False)]),
    ("look-alike /data/Mediax is outside -> data may go",
     lambda: removed({'e': EP(save_path='/data/Mediax/TV')}), [('e', True)]),
    ("only copy (library lacks file) -> keep data",
     lambda: removed({'e': EP()}, has=lambda t: False), [('e', False)]),
    ("library check returns truthy non-True -> keep data",
     lambda: removed({'e': EP()}, has=lambda t: 'yes'), [('e', False)]),
    ("library check raising -> keep data", c_has_raises, [('a', False)]),
    ("kept counts by reason", c_kept_counts,
     ({'season-pack': 1, 'movie': 1, 'other': 2, 'incomplete': 1, 'too-young': 1}, [('g', True)], ['g'])),
    ("dry-run by default removes nothing, plans with remove_data", c_dry_run_default,
     (True, [], [{'hash': 'a', 'name': 'Show.S01E05.1080p.WEB', 'remove_data': True},
                 {'hash': 'b', 'name': 'Show.S01E05.1080p.WEB', 'remove_data': False}], [])),
    ("per-run cap default 5, sorted, capped flag", c_cap, (['e0', 'e1', 'e2', 'e3', 'e4'], True, 5)),
    ("EPISODE_RETENTION_PER_RUN=2 honored", c_cap_env, (['e0', 'e1'], True)),
    ("hard max 25", c_hard_max, (25, 25, True)),
    ("not capped/paused when under limit", c_not_capped_when_under, (False, False, [], False)),
    ("any Error torrent pauses the run", c_paused, (True, [], [])),
    ("remove raising stops the run", c_remove_raises,
     ([('a', True)], ['a'], [{'hash': 'b', 'error': 'deluge down'}])),
    ("per_run_limit bad/negative",
     lambda: (target.per_run_limit({'EPISODE_RETENTION_PER_RUN': 'x'}),
              target.per_run_limit({'EPISODE_RETENTION_PER_RUN': '-3'})), (5, 0)),
    ("constants", lambda: (target.SEED_DAYS, target.DEFAULT_PER_RUN, target.HARD_MAX_PER_RUN, target.FIELDS),
     (21, 5, 25, ['name', 'label', 'state', 'progress', 'seeding_time', 'save_path'])),
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
