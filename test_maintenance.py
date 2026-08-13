"""Self-checks for the 6-hourly maintenance pass: the Deluge error-repair
branch decision (recheck vs relink vs missing -- picking wrong here means
moving real torrent data), the duplicate-folder name key (must not collapse
two different release years into one false dupe), the stalled-seed
swarm-safety gate (missing/unknown swarm seed data must never be treated as
safe to remove), and the shared queued-superseded filter.
No framework. Run: python test_maintenance.py"""

import os
import tempfile


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _stub_deluge(aw, torrents, existing_paths):
    """Fake Deluge: `torrents` is the get_torrents_status result, and
    `existing_paths` is the set of paths get_path_size should report as
    present. Records every move_storage / force_recheck call."""
    calls = []

    def post(url, json=None, timeout=None, **kw):
        method = json.get('method')
        if method == 'auth.login':
            return _Resp({'result': True})
        if method == 'core.get_torrents_status':
            return _Resp({'result': torrents})
        if method == 'core.get_path_size':
            path = json['params'][0]
            return _Resp({'result': 100 if path in existing_paths else -1})
        if method == 'core.force_recheck':
            calls.append(('recheck', json['params'][0][0]))
            return _Resp({'result': None})
        if method == 'core.move_storage':
            calls.append(('move', json['params'][0][0], json['params'][1]))
            return _Resp({'result': None})
        return _Resp({'result': None})

    aw.session = type('S', (), {'post': staticmethod(post)})()
    aw.record_activity = lambda *a, **k: None
    aw._reported_deluge_errors.clear()  # digest dedup is per-process, not per-case
    return calls


def run():
    aw = __import__('arr-webhook')

    # ── duplicate-folder key ────────────────────────────────────────────
    k = aw._dup_folder_key
    # collision suffixes collapse
    assert k('Movie Name (2020)') == k('Movie Name (2020) (1)')
    assert k('Season 01') == k('Season 01 (2)')
    assert k('Show') == k('Show - Copy')
    assert k('Show') == k('Show_1')
    # ...but a 4-digit year is NOT a collision suffix: two different years of
    # the same title are two different films, never one duplicate group.
    assert k('Movie Name (2019)') != k('Movie Name (2020)')
    assert k('The Thing (1982)') != k('The Thing (2011)')

    # ── duplicate-folder scan over a real tree ──────────────────────────
    with tempfile.TemporaryDirectory() as root:
        for d in ('Movie A (2020)', 'Movie A (2020) (1)', 'Movie B (2021)',
                  'The Thing (1982)', 'The Thing (2011)'):
            os.makedirs(os.path.join(root, d))
        with open(os.path.join(root, 'Movie A (2020)', 'f.mkv'), 'wb') as f:
            f.write(b'x' * 1024)
        aw.MEDIA_ROOTS = [root]
        findings = aw.find_duplicate_media_folders()
        assert len(findings) == 1, findings
        paths = sorted(os.path.basename(x['path']) for x in findings[0]['folders'])
        assert paths == ['Movie A (2020)', 'Movie A (2020) (1)'], paths

    # ── Deluge error repair: data still at save_path → recheck, no move ──
    torrents = {'h1': {'name': 'Rel.Name.2020.1080p', 'state': 'Error',
                       'save_path': '/data/Downloads', 'message': 'file error'}}
    calls = _stub_deluge(aw, torrents, {'/data/Downloads/Rel.Name.2020.1080p'})
    res = aw.deluge_error_repair(relink=True)
    assert calls == [('recheck', 'h1')], calls
    assert res['counts']['rechecked'] == 1, res

    # dry_run must not touch anything
    calls = _stub_deluge(aw, torrents, {'/data/Downloads/Rel.Name.2020.1080p'})
    res = aw.deluge_error_repair(dry_run=True, relink=True)
    assert calls == [], calls

    # ── data missing at save_path but found elsewhere ────────────────────
    # The two containers disagree on paths: Deluge calls the TV library
    # /data/Media/TV Shows, this container mounts it at /media/tv. A found
    # path must be translated back before Deluge is told to move anything.
    with tempfile.TemporaryDirectory() as tv_local:
        deluge_tv = '/data/Media/TV Shows'
        aw.DELUGE_PATH_MAP = [(deluge_tv, tv_local)]
        season = os.path.join(tv_local, 'Some Show', 'Season 01')
        os.makedirs(os.path.join(season, 'Rel.Name.2020.1080p'))
        deluge_season = f'{deluge_tv}/Some Show/Season 01'
        deluge_found = f'{deluge_season}/Rel.Name.2020.1080p'

        assert aw._local_to_deluge(season) == deluge_season
        assert aw._deluge_to_local(deluge_season) == season

        # relink disabled → reported as a candidate, nothing moved
        calls = _stub_deluge(aw, torrents, {deluge_found})
        res = aw.deluge_error_repair(relink=False)
        assert calls == [], calls
        assert res['counts']['relink_candidates'] == 1, res
        # destination must be in DELUGE's namespace, not the container's
        assert res['relink_candidates'][0]['relink_dest'] == deluge_season, res

        # relink enabled → move_storage to the translated parent dir
        calls = _stub_deluge(aw, torrents, {deluge_found})
        res = aw.deluge_error_repair(relink=True)
        assert calls == [('move', 'h1', deluge_season)], calls

        # translation that Deluge can't confirm → refuse to move
        calls = _stub_deluge(aw, torrents, set())
        res = aw.deluge_error_repair(relink=True)
        assert calls == [], calls
        assert res['counts']['errors'] == 1, res

        # ── data nowhere to be found → reported, never moved ─────────────
        gone = {'h2': {'name': 'Vanished.2019.720p', 'state': 'Error',
                       'save_path': '/data/Downloads', 'message': 'No such file'}}
        calls = _stub_deluge(aw, gone, set())
        res = aw.deluge_error_repair(relink=True)
        assert calls == [], calls
        assert res['counts']['missing'] == 1, res

    # ── non-Error torrents are never touched ─────────────────────────────
    ok = {'h3': {'name': 'Fine', 'state': 'Seeding', 'save_path': '/data/Downloads'}}
    calls = _stub_deluge(aw, ok, set())
    res = aw.deluge_error_repair(relink=True)
    assert calls == [], calls
    assert not any(res['counts'].values()), res

    # ── stalled-seed swarm-safety gate ───────────────────────────────────
    # h1: stale + negligible upload + plenty of swarm seeds -> removed
    # h2: stale + negligible upload but swarm seeds unknown (-1) -> kept
    # h3: stale + negligible upload but swarm seeds at/below the gate -> kept
    # h4: stale + negligible upload + swarm seeds missing entirely -> kept
    seed_torrents = {
        'h1': {'name': 'Safe To Remove', 'state': 'Seeding', 'seeding_time': 999 * 86400,
               'total_uploaded': 1000, 'total_seeds': 6},
        'h2': {'name': 'Unknown Swarm', 'state': 'Seeding', 'seeding_time': 999 * 86400,
               'total_uploaded': 1000, 'total_seeds': -1},
        'h3': {'name': 'Too Few Seeds', 'state': 'Seeding', 'seeding_time': 999 * 86400,
               'total_uploaded': 1000, 'total_seeds': 5},
        'h4': {'name': 'No Seed Field', 'state': 'Seeding', 'seeding_time': 999 * 86400,
               'total_uploaded': 1000},
    }
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        state_path = f.name
    try:
        aw.SEED_STATE_PATH = state_path
        aw.STALL_SEED_MIN_DAYS = 21
        aw.STALL_UPLOAD_THRESHOLD_BYTES = 10 * 1024 * 1024
        aw.STALL_MIN_SWARM_SEEDS = 5
        removed_hashes = []
        aw.remove_torrent = lambda h: removed_hashes.append(h)

        # first pass: only establishes the baseline, nothing removed
        _stub_deluge(aw, seed_torrents, set())
        candidates = aw.cleanup_stalled_seeds(dry_run=False)
        assert candidates == [], candidates
        assert removed_hashes == [], removed_hashes

        # second pass (upload unchanged since baseline): gate decides
        _stub_deluge(aw, seed_torrents, set())
        candidates = aw.cleanup_stalled_seeds(dry_run=False)
        assert removed_hashes == ['h1'], removed_hashes
        assert [c['hash'] for c in candidates] == ['h1'], candidates
        assert candidates[0]['swarm_seeds'] == 6, candidates
    finally:
        os.unlink(state_path)

    # ── shared queued-superseded filter ──────────────────────────────────
    targets = aw.queued_superseded_targets({
        'a': {'label': aw.SUPERSEDED_LABEL, 'state': 'Queued', 'name': 'a', 'progress': 0},
        'b': {'label': aw.SUPERSEDED_LABEL, 'state': 'Seeding', 'name': 'b', 'progress': 100},
        'c': {'label': 'radarr', 'state': 'Queued', 'name': 'c', 'progress': 0},
    })
    assert [t['hash'] for t in targets] == ['a'], targets

    print('test_maintenance: all assertions passed')


if __name__ == '__main__':
    run()
