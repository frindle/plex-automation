"""remove_torrent's seed floor with missing or None status fields: a field nobody knows is never read as 0, a
missing seed time is fetched, and a None from the caller does not hide the value Deluge reports. An unknown
save_path keeps the files (it could be a library path), and a queued-superseded target never turns an unknown
seed time into 0.
Run: python3 test_sf_unknown_fields.py"""
import json
import os
import sys
import tempfile

import requests

_TMP = tempfile.mkdtemp()
os.environ['ACTIVITY_LOG'] = os.path.join(_TMP, 'activity.jsonl')
os.environ['REMOVAL_LOG'] = os.path.join(_TMP, 'removal-audit.jsonl')
os.environ['UNPACK_STATE_PATH'] = os.path.join(_TMP, 'unpack.json')
os.environ['SEED_STATE_PATH'] = os.path.join(_TMP, 'seed.json')
aw = __import__('arr-webhook')

DAY = 86400
RPC = []
STATE = {'one': {}, 'fail': False, 'paths': {}}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_post(self, url, json=None, timeout=None, **kw):
    """Fake Deluge: 'one' is the per-torrent status, 'fail' makes the status fetch raise."""
    method, params = json['method'], json.get('params')
    RPC.append((method, params))
    if method == 'auth.login':
        return Resp({'result': True})
    if method == 'core.get_torrent_status':
        if STATE['fail']:
            raise requests.ConnectionError('deluge down')
        return Resp({'result': dict(STATE['one'].get(params[0], {}))})
    if method == 'core.get_torrents_status':  # _torrent_save_path's probe
        return Resp({'result': {h: {'save_path': STATE['paths'][h]} for h in params[0]['id'] if h in STATE['paths']}})
    return Resp({'result': None})


requests.Session.post = fake_post


def setup():
    for k in ('REMOVAL_LOG', 'ACTIVITY_LOG'):
        if os.path.exists(os.environ[k]):
            os.remove(os.environ[k])
    del RPC[:]
    STATE['one'] = {}
    STATE['fail'] = False
    STATE['paths'] = {}
    aw._snapshots.clear()


def _rows(key):
    try:
        with open(os.environ[key]) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def removed_hashes():
    return [p[0] for m, p in RPC if m == 'core.remove_torrent']


def status_calls():
    return [p for m, p in RPC if m == 'core.get_torrent_status']


def summaries():
    return [r['summary'] for r in _rows('ACTIVITY_LOG') if r['category'] == 'seed-floor']


def full(seeding_time, **kw):
    info = {'name': 'Some.Show.S01E01.1080p', 'seeding_time': seeding_time, 'progress': 100.0,
            'total_uploaded': 5_000_000, 'is_finished': True, 'save_path': '/data/Downloads/Complete'}
    info.update(kw)
    return info


def test_missing_seed_time_is_fetched():
    # The caller knows progress, upload and finished state but not the seed time: ask Deluge, do not defer forever.
    setup()
    STATE['one']['H1'] = full(30 * DAY)
    info = full(None)
    del info['seeding_time']
    assert aw.remove_torrent('H1', remove_data=False, info=info) is True
    assert len(status_calls()) == 1 and removed_hashes() == ['H1'], RPC
    assert _rows('REMOVAL_LOG')[-1]['seeding_days'] == 30.0, _rows('REMOVAL_LOG')


def test_missing_upload_count_is_not_never_seeded():
    # Seed time 0 and unfinished, but Deluge does not report the upload count: it may have uploaded, keep it.
    setup()
    status = full(0, progress=40.0, is_finished=False)
    del status['total_uploaded']
    STATE['one']['H2'] = status
    assert aw.remove_torrent('H2', remove_data=False) == aw.DEFERRED
    assert removed_hashes() == [], RPC
    setup()
    STATE['fail'] = True
    partial = full(0, progress=40.0, is_finished=False)
    del partial['total_uploaded']
    assert aw.remove_torrent('H2', remove_data=False, info=partial) == aw.DEFERRED
    assert removed_hashes() == [], RPC


def test_none_progress_defers_without_crashing():
    setup()
    STATE['fail'] = True
    info = full(0, progress=None, total_uploaded=0, is_finished=False)
    for rd in (False, True):
        assert aw.remove_torrent('H3', remove_data=rd, info=info) == aw.DEFERRED
    assert removed_hashes() == [], RPC


def test_caller_none_does_not_hide_deluge_value():
    # A None from the caller is unknown: Deluge's real progress (never-seeded partial) is used, so it may go.
    setup()
    STATE['one']['H4'] = full(0, name='From.Deluge', progress=12.0, total_uploaded=0, is_finished=False)
    assert aw.remove_torrent('H4', remove_data=False, info={'name': None, 'seeding_time': 0, 'progress': None}) is True
    assert removed_hashes() == ['H4'], RPC
    assert _rows('REMOVAL_LOG')[-1]['name'] == 'From.Deluge', _rows('REMOVAL_LOG')


def test_deferral_message_says_unknown():
    setup()
    STATE['one']['H5'] = {'name': 'No.Seed.Time'}
    assert aw.remove_torrent('H5', remove_data=False) == aw.DEFERRED
    assert summaries() and 'unknown' in summaries()[-1] and 'None' not in summaries()[-1], summaries()
    setup()
    assert aw.remove_torrent('H6', remove_data=False, info=full(5 * DAY)) == aw.DEFERRED
    assert '5.0d' in summaries()[-1], summaries()


def removals():
    return [tuple(p) for m, p in RPC if m == 'core.remove_torrent']


def guard_notes():
    return [r['summary'] for r in _rows('ACTIVITY_LOG') if r['category'] == 'library-guard']


def test_unknown_save_path_keeps_files():
    # Seeded long enough and data-delete asked, but nobody knows where the files are: they could be in the
    # library, so the entry goes and the files stay.
    for hsh, info in (('H7', full(30 * DAY, save_path=None)), ('H8', {k: v for k, v in full(30 * DAY).items() if k != 'save_path'})):
        setup()
        STATE['fail'] = True
        assert aw.remove_torrent(hsh, remove_data=True, info=info) is True
        assert removals() == [(hsh, False)], RPC
        assert guard_notes() and 'save_path unknown' in guard_notes()[-1], guard_notes()


def test_save_path_probe_fills_a_none():
    # A None save_path from the caller is looked up: a download path found that way still deletes its files,
    # a library path found that way keeps them.
    for path, want in (('/data/Downloads/Complete', True), ('/data/Media/Movies/X (2020)', False)):
        setup()
        STATE['fail'] = True
        STATE['paths']['H9'] = path
        assert aw.remove_torrent('H9', remove_data=True, info=full(30 * DAY, save_path=None)) is True
        assert removals() == [('H9', want)], (path, RPC)


def test_purge_does_not_read_unknown_seed_time_as_zero():
    # A queued superseded target whose seed time the snapshot did not carry: Deluge says it seeded 3 days, so
    # the purge defers it (read as 0, the never-seeded exemption would have deleted it).
    setup()
    STATE['one']['HA'] = full(3 * DAY, progress=0.0, total_uploaded=0, is_finished=False)
    removed, failed = aw.purge_queued_superseded([{'hash': 'HA', 'name': 'Queued.Show', 'seeding_time': None}])
    assert (removed, failed) == (0, []) and removals() == [], RPC


def test_queued_target_keeps_unknown_seed_time_unknown():
    # Selection still treats absent fields as never started (test_purge_guard, test_maintenance); the target
    # carries None, not 0, so remove_torrent asks Deluge instead of taking the never-seeded exemption on a guess.
    base = {'label': aw.SUPERSEDED_LABEL, 'state': 'Queued', 'seeding_time': 0, 'active_time': 0, 'total_uploaded': 0,
            'is_finished': False, 'progress': 0.0, 'name': 'n'}
    absent = {k: v for k, v in base.items() if k != 'seeding_time'}
    torrents = {'never': dict(base), 'absent': absent, 'none': dict(base, seeding_time=None),
                'done': dict(base, seeding_time=30 * DAY, total_uploaded=9, is_finished=True)}
    got = {t['hash']: t['seeding_time'] for t in aw.queued_superseded_targets(torrents)}
    assert got == {'never': 0, 'absent': None, 'none': None, 'done': 30 * DAY}, got


TESTS = [
    test_missing_seed_time_is_fetched,
    test_missing_upload_count_is_not_never_seeded,
    test_none_progress_defers_without_crashing,
    test_caller_none_does_not_hide_deluge_value,
    test_deferral_message_says_unknown,
    test_unknown_save_path_keeps_files,
    test_save_path_probe_fills_a_none,
    test_purge_does_not_read_unknown_seed_time_as_zero,
    test_queued_target_keeps_unknown_seed_time_unknown,
]

if __name__ == '__main__':
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f'RESULT {t.__name__} PASS')
        except Exception as e:
            failed += 1
            print(f'RESULT {t.__name__} FAIL {type(e).__name__}: {e}')
    print(f'{len(TESTS) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
