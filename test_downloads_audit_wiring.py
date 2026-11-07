"""Wiring of downloads_audit into arr-webhook: GET /downloads-audit is a dry
run (no Pushover, no state file), the tick runs once per interval, notifies
once and persists what it reported, and the 6h scheduler calls the tick.
Run: python test_downloads_audit_wiring.py"""
import inspect
import os
import tempfile


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def test_wiring():
    aw = __import__('arr-webhook')
    root = tempfile.mkdtemp()
    dl = os.path.join(root, 'dl')
    os.makedirs(os.path.join(dl, 'Complete', 'Gone'))
    orphan = os.path.join(dl, 'Complete', 'Gone', 'b.mkv')
    with open(orphan, 'wb') as f:
        f.write(b'x' * 10)
    os.utime(orphan, (1, 1))
    calls, pushes = [], []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            calls.append((json['method'], json['params'], timeout))
            return _Resp({'result': {'u1': {'name': 'U', 'label': '', 'save_path': '/data/Downloads/Complete',
                                            'progress': 100.0, 'files': []}}})

    saved = (aw.session, aw.deluge_login, aw.send_pushover, aw.DELUGE_PATH_MAP, aw.DOWNLOADS_AUDIT_STATE,
             aw._downloads_audit_last[0])
    state = os.path.join(root, 'reported.json')
    try:
        aw.session = S()
        aw.deluge_login = lambda: None
        aw.send_pushover = lambda title, msg, url=None: pushes.append((title, msg))
        aw.DELUGE_PATH_MAP = [('/data/Downloads', dl)]
        aw.DOWNLOADS_AUDIT_STATE = state
        aw._downloads_audit_last[0] = 0.0

        with aw.app.test_client() as c:
            r = c.get('/downloads-audit')
        body = r.get_json()
        assert r.status_code == 200 and body['dry_run'] is True, body
        assert body['counts']['orphan_files'] == 1 and body['counts']['unlabeled'] == 1, body['counts']
        assert pushes == [] and not os.path.exists(state)
        assert calls[0] == ('core.get_torrents_status', [{}, aw.downloads_audit.FIELDS], 180), calls[0]
        assert 'files' in aw.downloads_audit.FIELDS

        t0 = 10_000_000.0
        r1 = aw.downloads_audit_tick(now=t0)
        assert len(r1['notified']) == 2 and len(pushes) == 1 and os.path.exists(state)
        assert aw.downloads_audit_tick(now=t0 + 3600) is None           # inside the interval
        r3 = aw.downloads_audit_tick(now=t0 + aw.DOWNLOADS_AUDIT_INTERVAL)
        assert r3['notified'] == [] and len(pushes) == 1                # persisted: not re-announced
        assert os.path.exists(orphan)                                   # report-only
        assert 'downloads_audit_tick()' in inspect.getsource(aw.maintenance_scheduler)
    finally:
        (aw.session, aw.deluge_login, aw.send_pushover, aw.DELUGE_PATH_MAP, aw.DOWNLOADS_AUDIT_STATE,
         aw._downloads_audit_last[0]) = saved


if __name__ == '__main__':
    test_wiring()
    print('ok')
