"""Keep-data removals delete the torrent's sample files (samples only matter
while seeding); data-delete removals, cross-seeds, failed removals and
sample-only torrents leave them alone. Run: python test_remove_torrent_deletes_samples.py"""
import importlib.util
import os
import tempfile

import episode_retention

# A private copy of the module: other tests stub remove_torrent and globals on
# the shared one, and this test must neither see nor leak those changes.
_spec = importlib.util.spec_from_file_location(
    'arr_webhook_samples', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arr-webhook.py'))
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)
H = 'h1'
SP = '/data/Downloads/Complete'
FILES = ['Show.S01E05/Show.S01E05.mkv', 'Show.S01E05/Sample/show.s01e05.sample.mkv', 'Show.S01E05/show.nfo']
OLD = {'name': 'Show.S01E05', 'seeding_time': 40 * 86400, 'save_path': SP, 'progress': 100.0,
       'total_uploaded': 5, 'is_finished': True}


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def _session(files, remove_result=True, others=None):
    calls = []

    class S:
        def post(self, url, json=None, timeout=None, **kw):
            m, params = json['method'], json['params']
            calls.append(m)
            if m == 'core.get_torrent_status':
                if 'files' in params[1]:
                    return _Resp({'result': {'name': 'Show.S01E05', 'save_path': SP,
                                             'files': [{'path': f} for f in files]}})
                return _Resp({'result': dict(OLD)})
            if m == 'core.get_torrents_status':
                return _Resp({'result': others or {H: {'name': 'Show.S01E05', 'save_path': SP}}})
            if m == 'core.remove_torrent':
                return _Resp({'result': remove_result, 'error': None})
            return _Resp({'result': None})
    return S(), calls


def _mk(d, files):
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'wb').close()
    aw.DELUGE_PATH_MAP = [(SP, d)]


def run():
    aw.log_removal = lambda *a, **k: None
    aw.record_activity = lambda *a, **k: None
    sample = 'Show.S01E05/Sample/show.s01e05.sample.mkv'

    with tempfile.TemporaryDirectory() as d:
        _mk(d, FILES)
        aw.session, calls = _session(FILES)
        aw._invalidate_snapshot()
        assert aw.remove_torrent(H, remove_data=False, info=dict(OLD)) is True
        assert not os.path.exists(os.path.join(d, sample))
        assert not os.path.exists(os.path.join(d, 'Show.S01E05/Sample'))  # emptied dir removed
        assert os.path.exists(os.path.join(d, FILES[0])) and os.path.exists(os.path.join(d, FILES[2]))
        assert calls.index('core.remove_torrent') > calls.index('core.get_torrent_status')

    with tempfile.TemporaryDirectory() as d:
        # Deluge reports failure -> sample kept
        _mk(d, FILES)
        aw.session, _ = _session(FILES, remove_result=False)
        aw._invalidate_snapshot()
        aw.remove_torrent(H, remove_data=False, info=dict(OLD))
        assert os.path.exists(os.path.join(d, sample))

    with tempfile.TemporaryDirectory() as d:
        # cross-seed with same name+save_path still loaded -> sample kept
        _mk(d, FILES)
        others = {H: {'name': 'Show.S01E05', 'save_path': SP}, 'h2': {'name': 'Show.S01E05', 'save_path': SP + '/'}}
        aw.session, _ = _session(FILES, others=others)
        aw._invalidate_snapshot()
        aw.remove_torrent(H, remove_data=False, info=dict(OLD))
        assert os.path.exists(os.path.join(d, sample))

    with tempfile.TemporaryDirectory() as d:
        # only "sample" videos, no main video -> nothing deleted
        only = ['X/sample-x.mkv']
        _mk(d, only)
        aw.session, _ = _session(only)
        aw._invalidate_snapshot()
        aw.remove_torrent(H, remove_data=False, info=dict(OLD))
        assert os.path.exists(os.path.join(d, only[0]))

    with tempfile.TemporaryDirectory() as d:
        # seed floor defers -> nothing removed, nothing deleted, no sample lookup
        _mk(d, FILES)
        aw.session, calls = _session(FILES)
        aw._invalidate_snapshot()
        r = aw.remove_torrent(H, remove_data=False, info=dict(OLD, seeding_time=86400))
        assert r == aw.DEFERRED and 'core.remove_torrent' not in calls
        assert os.path.exists(os.path.join(d, sample))

    with tempfile.TemporaryDirectory() as d:
        # data-delete removal: Deluge deletes everything; no extra sample lookup
        _mk(d, FILES)
        aw.session, calls = _session(FILES)
        aw._invalidate_snapshot()
        aw.remove_torrent(H, remove_data=True, info=dict(OLD))
        assert calls.count('core.get_torrent_status') == 0, calls

    assert episode_retention.is_sample('A/Sample/a.mkv')
    assert episode_retention.is_sample('A/Samples/a.jpg')
    assert episode_retention.is_sample('A/a-sample.mkv')
    assert episode_retention.is_sample('A/sample.mkv')
    assert not episode_retention.is_sample('A/A.mkv')
    assert not episode_retention.is_sample('A/sample.nfo')
    print('test_remove_torrent_deletes_samples: all assertions passed')


def test_remove_torrent_deletes_samples():
    run()


if __name__ == '__main__':
    run()
