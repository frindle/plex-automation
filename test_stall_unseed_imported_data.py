"""Stalled-seed unseed deletes the download copy only when every file is verifiably imported (hardlinked)."""
import importlib.util
import os

spec = importlib.util.spec_from_file_location("aw_stall", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

T = {'name': 'Old.Show.S01', 'label': 'sonarr', 'state': 'Seeding', 'seeding_time': 999 * 86400,
     'total_uploaded': 1000, 'total_seeds': 9, 'save_path': '/data/Downloads/Complete'}


def run(monkeypatch, tmp_path, nlinks, save_path='/data/Downloads/Complete'):
    files = []
    for i, n in enumerate(nlinks):
        f = tmp_path / ('e%d.mkv' % i)
        f.write_bytes(b'x')
        for j in range(n - 1):
            os.link(f, tmp_path / ('link%d_%d' % (i, j)))
        files.append({'path': f.name})
    t = dict(T, save_path=save_path)
    monkeypatch.setattr(aw, 'SEED_STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda *a, **k: {'h': dict(t)})
    monkeypatch.setattr(aw, '_deluge_rpc', lambda m, p: {'files': files})
    monkeypatch.setattr(aw, '_deluge_to_local', lambda p: str(tmp_path / os.path.basename(p)))
    monkeypatch.setattr(aw, 'record_activity', lambda *a: None)
    calls = []
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: calls.append(remove_data) or True)
    aw.cleanup_stalled_seeds()  # baseline
    aw.cleanup_stalled_seeds()
    return calls


def test_all_files_imported_deletes_the_download_copy(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, [2, 3]) == [True]


def test_any_file_not_imported_keeps_the_files(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, [2, 1]) == [False]


def test_media_tree_torrent_keeps_its_files(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, [2], save_path=aw.episode_retention.MEDIA_ROOT + '/TV/Old') == [False]
