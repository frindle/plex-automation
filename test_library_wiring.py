"""Wiring of library_audit / episode_retention into arr-webhook: both GET
routes are report-only (no Pushover, no removal, even with
EPISODE_RETENTION_APPLY=1), and _library_has_file only says True for files
that are verifiably hardlinked. Run: python test_library_wiring.py"""
import os
import tempfile


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


EP = {'name': 'Show.S01E05.1080p', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
      'seeding_time': 40 * 86400, 'save_path': '/data/Downloads/Complete',
      'files': [{'path': 'Show.S01E05.1080p/Show.S01E05.1080p.mkv'}]}
LIB = {'name': 'Rel', 'label': 'sonarr', 'state': 'Seeding', 'save_path': '/data/Media/TV Shows/X', 'message': ''}


def run():
    aw = __import__('arr-webhook')
    calls = []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            calls.append(json['method'])
            if json['method'] == 'core.get_torrents_status':
                return _Resp({'result': {'e1': EP, 'l1': LIB}})
            return _Resp({'result': None})

    aw.session = S()
    aw.deluge_login = lambda: None
    pushed, removed = [], []
    aw.send_pushover = lambda *a, **k: pushed.append(a)
    aw.remove_torrent = lambda h, remove_data=True, info=None: removed.append((h, remove_data))
    aw._reported_library_audit.clear()
    os.environ['EPISODE_RETENTION_APPLY'] = '1'
    try:
        c = aw.app.test_client()
        r = c.get('/library-seed-audit').get_json()
        assert [f['hash'] for f in r['flagged']] == ['l1'], r
        assert r['notified'] == [] and pushed == [], (r, pushed)
        r = c.get('/episode-retention').get_json()
        assert r['dry_run'] is True and removed == [], (r, removed)
        assert [e['hash'] for e in r['planned']] == ['e1'], r
        assert r['planned'][0]['remove_data'] is False, r  # not provably hardlinked -> keep data
    finally:
        del os.environ['EPISODE_RETENTION_APPLY']
    assert set(calls) == {'core.get_torrents_status'}, calls

    # the tick path (not dry-run) sends one summary push, never re-announced
    aw.library_seed_audit()
    aw.library_seed_audit()
    assert len(pushed) == 1, pushed
    assert pushed[0][0] == 'Library seed audit: 1 newly flagged', pushed

    # the retention tick logs one readable INFO line with the dry-run plan
    lines = []
    real_info = aw.log.info
    aw.log.info = lambda msg, *a, **k: lines.append(msg)
    try:
        r = aw.episode_retention_tick()
    finally:
        aw.log.info = real_info
    assert r['dry_run'] is True and removed == [], (r, removed)
    plan = [m for m in lines if m.startswith('Episode retention')]
    assert len(plan) == 1, lines
    assert plan[0].startswith('Episode retention dry-run: planned 1/5,'), plan
    assert 'paused=False' in plan[0] and 'Show.S01E05.1080p (keep data)' in plan[0], plan

    # _library_has_file: hardlinked -> True; single link / missing -> False
    with tempfile.TemporaryDirectory() as d:
        aw.DELUGE_PATH_MAP = [('/data/Downloads/Complete', d)]
        os.makedirs(os.path.join(d, 'Show.S01E05.1080p'))
        f = os.path.join(d, 'Show.S01E05.1080p', 'Show.S01E05.1080p.mkv')
        open(f, 'wb').close()
        assert aw._library_has_file(EP) is False
        os.link(f, os.path.join(d, 'lib-copy.mkv'))
        assert aw._library_has_file(EP) is True
        assert aw._library_has_file(dict(EP, files=[])) is False
        assert aw._library_has_file(dict(EP, files=[{'path': 'nope.mkv'}])) is False

    print('test_library_wiring: all assertions passed')


def test_library_wiring():
    run()


if __name__ == '__main__':
    run()
