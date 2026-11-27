"""SABnzbd-sourced upgrades under the watch hold: a Usenet upgrade never cuts or switches a live stream.
Everything is faked; nothing reaches SABnzbd, Plex or the arrs. Ids and paths are made up."""
import pytest

import sab_hold
import watch_hold as wh
from test_watch_hold import NEW, NOW, OLD, FakeIO, aw, meta, movie_world, speechy

NZO = 'sabnzbd_nzo_ab12cd'


@pytest.fixture(scope='module')
def film():
    return speechy(1200, 1)


def usenet_world(film, progress=40.0, label='radarr'):
    """movie_world with the download moved from a torrent hash to a SABnzbd id."""
    io = movie_world(film, label=label, progress=progress)
    h1 = next(iter(io.t))
    io.t = {NZO: dict(io.t.pop(h1), usenet=True)}
    io.q['Radarr'][0]['downloadId'] = 'SABnzbd_nzo_ab12cd'
    io.grabs.clear()
    return io


# -- the hold and release over the fake world -------------------------------------------------------------

def test_streamed_usenet_upgrade_is_paused_then_resumed_after_the_stream(film):
    io, doc, cfg, checked = usenet_world(film), {}, wh.Cfg('on'), set()
    io.sess = [{'key': '100', 'files': [], 'state': 'playing'}]
    c = wh.tick(io, doc, cfg, NOW, checked)
    assert c['tracked'] == 1 and c['held'] == 1
    assert io.t[NZO]['label'] == wh.HOLD_LABEL and doc['held'][NZO]['label'] == 'radarr'
    # paused, it cannot finish: nothing is imported while the stream goes on
    c = wh.tick(io, doc, cfg, NOW + 600, checked)
    assert c['kept_playing'] == 1 and io.t[NZO]['label'] == wh.HOLD_LABEL and not c.get('released')
    io.sess = []
    c = wh.tick(io, doc, cfg, NOW + 615, checked)
    assert c['kept_recent'] == 1 and io.t[NZO]['label'] == wh.HOLD_LABEL
    c = wh.tick(io, doc, cfg, NOW + 615 + 601, checked)
    assert c['usenet_unmapped'] == 1
    assert c['released'] == 1 and io.t[NZO]['label'] == 'radarr' and io.refreshed == ['Radarr']
    assert doc['held'] == {}


def test_release_of_a_paused_job_resumes_it_and_never_changes_the_category():
    s = sab(queue={'queue': {'slots': [{'nzo_id': 'SABnzbd_nzo_AbC123', 'cat': 'radarr', 'percentage': '42', 'status': 'Paused'}]}})
    r = sab_hold.rows(s)['sabnzbd_nzo_abc123']
    assert r['label'] == wh.HOLD_LABEL and r['usenet'] and not r['is_finished']
    doc = {'held': {'sabnzbd_nzo_abc123': {'label': 'radarr', 'who': 'Radarr'}}, 'tracked': {}, 'seen': {}}

    class Io:
        def __init__(self):
            self.saved = 0

        def torrents(self):
            return sab_hold.rows(s)

        def set_label(self, h, label):
            (s.pause if label == wh.HOLD_LABEL else s.resume)(h)

        def save(self, d):
            self.saved += 1

        def refresh(self, who):
            pass

        def queue(self, who):
            return [{'downloadId': 'SABnzbd_nzo_AbC123'}]

    c = wh.release_pass(Io(), doc, [], NOW, wh.Cfg('on'), wh.Counts(), force=True)
    assert c['released'] == 1 and doc['held'] == {}
    modes = [p for _, p in s._http.calls if p['mode'] == 'queue' and p.get('name')]
    assert modes[-1] == {'mode': 'queue', 'output': 'json', 'apikey': 'KEY123', 'name': 'resume', 'value': 'SABnzbd_nzo_AbC123'}
    assert not [1 for _, p in s._http.calls if p['mode'] == 'change_cat']


def test_held_at_once_not_at_half_way(film):
    io, doc = usenet_world(film, progress=1.0), {}
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['held'] == 1, 'a fast Usenet download may finish within a pass: no waiting for 50 percent'


def test_a_download_already_in_the_history_cannot_be_paused_and_is_left_alone(film):
    io, doc = usenet_world(film, progress=100.0), {}
    io.t[NZO].update(is_finished=True, in_history=True)
    io.sess = [{'key': '100', 'files': [], 'state': 'playing'}]
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert not c.get('held') and not c.get('hold_failed') and io.labels == []


def test_post_processing_row_in_history_is_not_held_either(film):
    io, doc = usenet_world(film, progress=0.0), {}
    io.t[NZO].update(in_history=True, is_finished=False)               # Extracting, Repairing...
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert not c.get('held') and io.labels == []


def test_a_custom_category_name_is_still_held(film):
    io, doc = usenet_world(film, label='movies-usenet'), {}
    io.sess = [{'key': '100', 'files': [], 'state': 'playing'}]
    assert wh.tick(io, doc, wh.Cfg('on'), NOW, set())['held'] == 1


def test_a_torrent_is_unchanged_by_the_usenet_rules(film):
    io = movie_world(film, progress=5.0)
    doc = {}
    assert not wh.tick(io, doc, wh.Cfg('on'), NOW, set()).get('held'), 'a torrent still waits for half way'


def test_dry_run_never_pauses(film):
    io, doc = usenet_world(film), {}
    io.sess = [{'key': '100', 'files': [], 'state': 'playing'}]
    c = wh.tick(io, doc, wh.Cfg('dry'), NOW, set())
    assert c['would_hold'] == 1 and io.labels == []


# -- the SABnzbd client -----------------------------------------------------------------------------------

class Resp:
    def __init__(self, doc, code=200):
        self.doc, self.code = doc, code

    def raise_for_status(self):
        if self.code >= 400:
            raise OSError('http ' + str(self.code) + ' secret-url-part')

    def json(self):
        return self.doc


class Http:
    def __init__(self, docs):
        self.docs, self.calls = docs, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        d = self.docs[params['mode']]
        if isinstance(d, Exception):
            raise d
        return Resp(d)


QUEUE = {'queue': {'slots': [{'nzo_id': 'SABnzbd_nzo_AbC123', 'cat': 'radarr', 'percentage': '42', 'status': 'Downloading'}]}}
HISTORY = {'history': {'slots': [
    {'nzo_id': 'SABnzbd_nzo_Done01', 'category': 'sonarr', 'status': 'Completed', 'storage': '/sab/complete/sonarr/Show.S01E01'},
    {'nzo_id': 'SABnzbd_nzo_Busy02', 'category': 'sonarr', 'status': 'Extracting', 'storage': ''}]}}


def sab(**docs):
    return sab_hold.Sab('http://sab.invalid:8080/', 'KEY123', http=Http({'queue': QUEUE, 'history': HISTORY, **docs}))


def test_rows_map_the_queue_and_history_onto_the_torrent_shape():
    r = sab_hold.rows(sab())
    q = r['sabnzbd_nzo_abc123']
    assert q == {'label': 'radarr', 'is_finished': False, 'progress': 42.0, 'usenet': True, 'status': 'Downloading'}
    d = r['sabnzbd_nzo_done01']
    assert d['is_finished'] and d['in_history'] and d['progress'] == 100.0 and d['label'] == 'sonarr'
    b = r['sabnzbd_nzo_busy02']
    assert not b['is_finished'] and b['in_history']


def test_pause_and_resume_send_the_id_as_sabnzbd_spelled_it():
    s = sab()
    sab_hold.rows(s)
    s.pause('sabnzbd_nzo_abc123')
    s.resume('sabnzbd_nzo_abc123')
    (_, p1), (_, p2) = s._http.calls[-2:]
    assert p1['mode'] == 'queue' and p1['name'] == 'pause' and p1['value'] == 'SABnzbd_nzo_AbC123'
    assert p2['name'] == 'resume' and p2['value'] == 'SABnzbd_nzo_AbC123'


def test_errors_and_repr_never_carry_the_key_or_url():
    s = sab(queue=OSError('connect to http://sab.invalid:8080/api?apikey=KEY123'))
    with pytest.raises(sab_hold.SabError) as e:
        s.queue()
    assert 'KEY123' not in str(e.value) and 'sab.invalid' not in str(e.value)
    assert 'KEY123' not in repr(s) and 'sab.invalid' not in repr(s)
    s2 = sab(queue={'status': False})
    with pytest.raises(sab_hold.SabError):
        s2.pause('sabnzbd_nzo_x')


def test_is_usenet_id_and_path_map():
    assert sab_hold.is_usenet_id('SABnzbd_nzo_x') and not sab_hold.is_usenet_id('a' * 40)
    pairs = sab_hold.parse_path_map('/sab/complete=/data/Downloads,/sab=/other')
    assert sab_hold.to_local('/sab/complete/radarr/x', pairs) == '/data/Downloads/radarr/x'
    assert sab_hold.to_local('/sab/other', pairs) == '/other/other'
    assert sab_hold.to_local('/elsewhere/x', pairs) == '/elsewhere/x'


def test_biggest_video_only_for_a_completed_download(tmp_path):
    d = tmp_path / 'complete' / 'radarr' / 'Movie.2001'
    d.mkdir(parents=True)
    (d / 'small.mkv').write_bytes(b'x' * 10)
    (d / 'big.mkv').write_bytes(b'x' * 100)
    (d / 'notes.nfo').write_bytes(b'x' * 1000)
    docs = {'history': {'history': {'slots': [
        {'nzo_id': 'SABnzbd_nzo_Done01', 'category': 'radarr', 'status': 'Completed', 'storage': '/sab/complete/radarr/Movie.2001'},
        {'nzo_id': 'SABnzbd_nzo_Busy02', 'category': 'radarr', 'status': 'Extracting', 'storage': '/sab/complete/radarr/Movie.2001'}]}}}
    s = sab(**docs)
    pairs = sab_hold.parse_path_map(f'/sab/complete={tmp_path}/complete')
    assert sab_hold.biggest_video(s, 'sabnzbd_nzo_done01', pairs) == str(d / 'big.mkv')
    assert sab_hold.biggest_video(s, 'sabnzbd_nzo_busy02', pairs) is None
    assert sab_hold.biggest_video(s, 'sabnzbd_nzo_unknown', pairs) is None


# -- WatchIO wiring in arr-webhook.py ---------------------------------------------------------------------

class FakeDeluge:
    def __init__(self, monkeypatch, torrents):
        monkeypatch.setattr(aw, 'deluge_login', lambda: None)
        monkeypatch.setattr(aw, 'get_all_torrents', lambda *a, **k: dict(torrents))


def test_watchio_merges_sabnzbd_rows_and_routes_set_label_to_pause_and_resume(monkeypatch):
    FakeDeluge(monkeypatch, {'a' * 40: {'label': 'radarr'}})
    s = sab()
    monkeypatch.setattr(aw, '_sab_client', s)
    io = aw.WatchIO({})
    t = io.torrents()
    assert 'a' * 40 in t and 'sabnzbd_nzo_abc123' in t
    io.set_label('sabnzbd_nzo_abc123', 'watch-hold')
    assert s._http.calls[-1][1]['name'] == 'pause'
    io.set_label('sabnzbd_nzo_abc123', 'radarr')
    assert s._http.calls[-1][1]['name'] == 'resume'


def test_sabnzbd_down_without_usenet_holds_leaves_deluge_alone(monkeypatch):
    FakeDeluge(monkeypatch, {'a' * 40: {'label': 'radarr'}})
    monkeypatch.setattr(aw, '_sab_client', sab(queue=OSError('down')))
    monkeypatch.setattr(aw, '_watch_doc', {'held': {}, 'tracked': {}, 'seen': {}})
    assert set(aw.WatchIO({}).torrents()) == {'a' * 40}


def test_sabnzbd_down_with_a_held_usenet_download_fails_the_pass_instead_of_dropping_it(monkeypatch):
    FakeDeluge(monkeypatch, {})
    monkeypatch.setattr(aw, '_sab_client', sab(queue=OSError('down')))
    monkeypatch.setattr(aw, '_watch_doc', {'held': {NZO: {'label': 'radarr'}}, 'tracked': {}, 'seen': {}})
    with pytest.raises(sab_hold.SabError):
        aw.WatchIO({}).torrents()


def test_without_sabnzbd_configured_nothing_changes(monkeypatch):
    FakeDeluge(monkeypatch, {'a' * 40: {'label': 'radarr'}})
    monkeypatch.setattr(aw, '_sab_client', None)
    monkeypatch.setattr(aw, 'SABNZBD_URL', '')
    assert set(aw.WatchIO({}).torrents()) == {'a' * 40}


def test_watchio_new_file_for_a_usenet_download(monkeypatch, tmp_path):
    d = tmp_path / 'radarr' / 'Movie'
    d.mkdir(parents=True)
    (d / 'm.mkv').write_bytes(b'x' * 5)
    s = sab(history={'history': {'slots': [{'nzo_id': 'SABnzbd_nzo_AbC123', 'category': 'radarr', 'status': 'Completed',
                                            'storage': '/sab/radarr/Movie'}]}})
    monkeypatch.setattr(aw, '_sab_client', s)
    monkeypatch.setattr(aw, 'SABNZBD_PATH_PAIRS', sab_hold.parse_path_map(f'/sab={tmp_path}'))
    assert aw.WatchIO({}).new_file('sabnzbd_nzo_abc123') == str(d / 'm.mkv')
