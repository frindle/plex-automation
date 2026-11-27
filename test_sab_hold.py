"""SABnzbd-sourced upgrades under the watch hold: a Usenet upgrade is classified on arrival, downloads in the hold
category and is imported through the arr only when nobody streams the item. Everything is faked; nothing reaches
SABnzbd, Plex or the arrs. Ids and paths are made up."""
import pytest

import sab_hold
import watch_hold as wh
from test_watch_hold import NEW, NOW, OLD, FakeIO, aw, meta, movie_world, speechy

NZO = 'sabnzbd_nzo_ab12cd'
DONE_DIR = '/sab/complete/watch-hold/Some.Movie.2001'


@pytest.fixture(scope='module')
def film():
    return speechy(1200, 1)


class UsenetIO(FakeIO):
    """FakeIO plus the SABnzbd and *arr calls a Usenet hold makes, recorded in order in .calls."""

    def set_label(self, h, label):
        if self.t[h].get('in_history'):
            raise RuntimeError('change_cat refused: not queued')
        self.t[h]['label'] = label
        self.calls.append(('cat', h, label))

    def resume(self, h):
        self.t[h]['paused'] = False
        self.t[h]['status'] = 'Downloading'
        self.calls.append(('resume', h))

    def import_usenet(self, h, who, row):
        self.calls.append(('import', h, who, row['storage']))
        self.imported = self.import_makes_history

    def arr_imported(self, who, h, e):
        return self.imported

    def forget_usenet(self, h):
        self.calls.append(('forget', h))
        self.t.pop(h, None)

    def mark_failed(self, who, h):
        self.calls.append(('failed', who, h))

    def acts(self, name):
        return [c for c in self.calls if c[0] == name]


def usenet_world(film, label='radarr'):
    """movie_world's upgrade as a paused SABnzbd job (nzo id), on an IO that records the SABnzbd and arr calls."""
    io = movie_world(film, label=label)
    io.__class__ = UsenetIO
    io.calls, io.imported, io.import_makes_history = [], False, False
    io.t = {NZO: {'label': label, 'progress': 0.0, 'is_finished': False, 'usenet': True, 'paused': True,
                  'status': 'Paused'}}
    io.q['Radarr'][0]['downloadId'] = 'SABnzbd_nzo_ab12cd'
    io.grabs.clear()
    return io


def finish(io, label=wh.HOLD_LABEL):
    """The job completes in SABnzbd's history, in the hold category."""
    io.t[NZO] = {'label': label, 'progress': 100.0, 'is_finished': True, 'in_history': True, 'usenet': True,
                 'status': 'completed', 'storage': DONE_DIR}
    io.new[NZO] = NEW
    io.q['Radarr'] = []                                 # the arr cannot see the held job


STREAM = [{'key': '100', 'files': [], 'state': 'playing'}]


# -- classification on arrival ----------------------------------------------------------------------------

def test_an_upgrade_is_moved_to_the_hold_category_while_queued_then_resumed(film):
    io, doc = usenet_world(film), {}
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['tracked'] == 1 and c['held'] == 1 and c['resumed'] == 1
    assert io.calls == [('cat', NZO, wh.HOLD_LABEL), ('resume', NZO)], 'category first, then resume: it never runs visible'
    assert doc['held'][NZO]['label'] == 'radarr' and doc['held'][NZO]['usenet'] and doc['held'][NZO]['resumed']
    assert doc['usenet'] == {NZO: 'hold'} and NZO in doc['tracked']
    assert doc['held'][NZO]['olds'] == {'m7': OLD}
    # classified once: nothing more happens on the next passes
    io.calls.clear()
    wh.tick(io, doc, wh.Cfg('on'), NOW + 15, set())
    assert io.calls == []


def test_a_job_that_is_not_an_upgrade_is_resumed_unchanged(film):
    io, doc, checked = usenet_world(film), {}, set()
    io.arr.clear()                                      # the movie has no file: a first download
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, checked)
    assert c['resumed'] == 1 and not c.get('held')
    assert io.calls == [('resume', NZO)] and io.t[NZO]['label'] == 'radarr'
    assert doc['held'] == {} and doc['tracked'] == {} and doc['usenet'] == {NZO: 'resume'}
    io.calls.clear()
    wh.tick(io, doc, wh.Cfg('on'), NOW + 15, checked)
    assert io.calls == []


def test_a_job_the_arr_does_not_list_yet_stays_paused_and_is_asked_again(film):
    io, doc = usenet_world(film), {}
    io.q['Radarr'] = []
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['classify_wait'] == 1 and io.calls == [] and io.t[NZO]['paused']
    io.q['Radarr'] = [{'downloadId': 'SABnzbd_nzo_ab12cd', 'movieId': 7, 'trackedDownloadState': 'downloading'}]
    assert wh.tick(io, doc, wh.Cfg('on'), NOW + 15, set())['held'] == 1


def test_a_failed_category_change_leaves_nothing_held_and_is_retried(film):
    io, doc = usenet_world(film), {}
    ok = io.set_label

    def refuse(h, label):
        raise RuntimeError('down')
    io.set_label = refuse
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['hold_failed'] == 1 and doc['held'] == {} and doc['usenet'] == {} and io.t[NZO]['paused']
    io.set_label = ok
    assert wh.tick(io, doc, wh.Cfg('on'), NOW + 15, set())['held'] == 1


def test_a_failed_resume_after_the_category_change_is_retried(film):
    io, doc = usenet_world(film), {}
    real = io.resume
    io.resume = lambda h: (_ for _ in ()).throw(RuntimeError('down'))
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['held'] == 1 and c['resume_failed'] == 1 and not doc['held'][NZO]['resumed']
    io.resume = real
    wh.tick(io, doc, wh.Cfg('on'), NOW + 15, set())
    assert doc['held'][NZO]['resumed'] and not io.t[NZO]['paused']


def test_a_job_already_in_the_history_is_only_counted(film):
    io, doc = usenet_world(film), {}
    finish(io, label='radarr')
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c['usenet_late'] == 1 and io.calls == [] and doc['held'] == {}
    assert wh.tick(io, doc, wh.Cfg('on'), NOW + 15, set()).get('usenet_late') is None, 'counted once'


def test_a_custom_category_name_is_classified(film):
    io, doc = usenet_world(film, label='movies-usenet'), {}
    assert wh.tick(io, doc, wh.Cfg('on'), NOW, set(), base_labels=('movies-usenet',))['held'] == 1
    assert doc['held'][NZO]['label'] == 'movies-usenet'


def test_a_torrent_is_unchanged_by_the_usenet_rules(film):
    io = movie_world(film, progress=5.0)
    doc = {}
    assert not wh.tick(io, doc, wh.Cfg('on'), NOW, set()).get('held'), 'a torrent still waits for half way'


# -- dry mode ---------------------------------------------------------------------------------------------

def test_dry_mode_counts_and_changes_nothing(film):
    io, doc = usenet_world(film), {}
    c = wh.tick(io, doc, wh.Cfg('dry'), NOW, set())
    assert c['would_hold'] == 1 and io.calls == [] and doc['held'] == {} and doc['usenet'] == {}
    io2, doc2 = usenet_world(film), {}
    io2.arr.clear()
    c = wh.tick(io2, doc2, wh.Cfg('dry'), NOW, set())
    assert c['would_resume'] == 1 and io2.calls == [] and io2.t[NZO]['paused']


def test_dry_mode_never_imports_or_marks_failed(film):
    io = usenet_world(film)
    finish(io)
    doc = {'held': {NZO: {'label': 'radarr', 'who': 'Radarr', 'usenet': True, 'resumed': True}}, 'tracked': {}, 'seen': {}}
    c = wh.tick(io, doc, wh.Cfg('dry'), NOW, set())
    assert c['would_release'] == 1 and io.calls == []
    io.t[NZO].update(failed=True, is_finished=False, status='failed')
    wh.tick(io, doc, wh.Cfg('dry'), NOW + 15, set())
    assert io.calls == [] and NZO in doc['held']


# -- the release is an import, and never under a stream ---------------------------------------------------

def held_job(film, now=NOW):
    io, doc, cfg = usenet_world(film), {}, wh.Cfg('on')
    wh.tick(io, doc, cfg, now, set())
    io.calls.clear()
    return io, doc, cfg


def test_a_queued_held_job_keeps_downloading_in_the_hold_category(film):
    io, doc, cfg = held_job(film)
    io.t[NZO].update(label=wh.HOLD_LABEL, paused=False, status='Downloading', progress=40.0)
    c = wh.tick(io, doc, cfg, NOW + 600, set())
    assert c['kept_downloading'] == 1 and io.calls == [], 'the arr would import it unguarded if it saw it now'


def test_import_is_blocked_while_the_item_streams_even_at_the_cap(film):
    io, doc, cfg = held_job(film)
    finish(io)
    io.sess = list(STREAM)
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert c['kept_playing'] == 1 and not io.acts('import')
    cap = cfg.max_s + 3600
    c = wh.tick(io, doc, cfg, NOW + cap, set())
    assert c['kept_playing'] == 1 and not io.acts('import'), 'the cap skips the mapping, never the stream check'
    io.sess = [{'key': '100', 'files': [], 'state': 'paused'}]
    assert wh.tick(io, doc, cfg, NOW + cap + 15, set()).get('kept_playing') == 1 and not io.acts('import')
    io.sess = []
    c = wh.tick(io, doc, cfg, NOW + cap + 30, set())
    assert c['kept_recent'] == 1 and not io.acts('import'), 'a viewer who just stopped may press play again'
    c = wh.tick(io, doc, cfg, NOW + cap + 30 + 601, set())
    assert c['import_sent'] == 1 and len(io.acts('import')) == 1


def test_sessions_are_read_again_right_before_the_import(film):
    io, doc, cfg = held_job(film)
    finish(io)
    reads = []

    def sessions():
        reads.append(1)
        return [] if len(reads) == 1 else list(STREAM)   # quiet at the start of the pass, streaming a moment later
    io.sessions = sessions
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert len(reads) >= 2 and not io.acts('import') and c.get('kept_playing') == 1


def test_unreadable_sessions_never_import(film):
    io, doc, cfg = held_job(film)
    finish(io)
    calls = []

    def sessions():
        calls.append(1)
        if len(calls) > 1:
            raise OSError('plex down')
        return []
    io.sessions = sessions
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert c['kept_sessions_unreadable'] == 1 and not io.acts('import')


def test_resume_points_are_mapped_on_the_new_file_before_the_import(film):
    io, doc, cfg = held_job(film)
    finish(io)
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    m = doc['tracked'][NZO]['items']['m7']['maps']['kid']
    assert m['ok'] and abs(m['new_ms'] - 607_300) < 50 and c['mapped_ok'] == 1
    assert c['import_sent'] == 1


def test_import_command_shape_and_verify_then_delete_the_history_entry(film):
    io, doc, cfg = held_job(film)
    finish(io)
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert io.acts('import') == [('import', NZO, 'Radarr', DONE_DIR)] and c['import_sent'] == 1
    assert not io.acts('forget') and NZO in doc['held'], 'nothing is deleted before the arr shows the import'
    c = wh.tick(io, doc, cfg, NOW + 75, set())
    assert c['kept_verifying'] == 1 and not io.acts('import') and not io.acts('forget')
    io.imported = True                                  # the arr's history shows the import
    c = wh.tick(io, doc, cfg, NOW + 90, set())
    assert c['imported'] == 1 and io.acts('forget') == [('forget', NZO)] and doc['held'] == {}


def test_an_import_not_seen_in_the_history_is_sent_again_then_gives_up(film):
    io, doc, cfg = held_job(film)
    finish(io)
    t = NOW + 60
    for _ in range(wh.IMPORT_MAX_TRIES):
        wh.tick(io, doc, cfg, t, set())
        t += wh.IMPORT_RETRY_S + 1
    assert len(io.acts('import')) == wh.IMPORT_MAX_TRIES
    c = wh.tick(io, doc, cfg, t, set())
    assert c['import_stuck'] == 1 and len(io.acts('import')) == wh.IMPORT_MAX_TRIES and not io.acts('forget')


def test_a_retry_is_also_blocked_by_a_stream(film):
    io, doc, cfg = held_job(film)
    finish(io)
    wh.tick(io, doc, cfg, NOW + 60, set())
    io.sess = list(STREAM)
    wh.tick(io, doc, cfg, NOW + 60 + wh.IMPORT_RETRY_S + 1, set())
    assert len(io.acts('import')) == 1


def test_the_hold_is_put_back_by_category_when_the_feature_is_switched_off(film):
    io, doc, cfg = held_job(film)
    io.t[NZO].update(label=wh.HOLD_LABEL, paused=False, status='Downloading', progress=40.0)
    c = wh.tick(io, doc, wh.Cfg('off'), NOW + 60, set())
    assert c['released'] == 1 and io.calls == [('cat', NZO, 'radarr')] and doc['held'] == {}


# -- a held job that fails --------------------------------------------------------------------------------

def test_a_failed_held_job_is_marked_failed_in_the_arr(film):
    io, doc, cfg = held_job(film)
    io.t[NZO] = {'label': wh.HOLD_LABEL, 'progress': 100.0, 'is_finished': False, 'in_history': True, 'usenet': True,
                 'status': 'failed', 'failed': True, 'storage': ''}
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert c['failed_marked'] == 1
    assert io.calls == [('failed', 'Radarr', NZO), ('forget', NZO)]
    assert doc['held'] == {} and NZO not in doc['tracked'] and not io.acts('import')


def test_a_failed_mark_is_retried_and_the_job_is_not_forgotten_first(film):
    io, doc, cfg = held_job(film)
    io.t[NZO] = {'label': wh.HOLD_LABEL, 'is_finished': False, 'in_history': True, 'usenet': True,
                 'status': 'failed', 'failed': True, 'storage': ''}
    real = io.mark_failed

    def boom(who, h):
        raise OSError('arr down')
    io.mark_failed = boom
    c = wh.tick(io, doc, cfg, NOW + 60, set())
    assert c['mark_failed_error'] == 1 and NZO in doc['held'] and not io.acts('forget')
    io.mark_failed = real
    assert wh.tick(io, doc, cfg, NOW + 75, set())['failed_marked'] == 1


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


QUEUE = {'queue': {'slots': [{'nzo_id': 'SABnzbd_nzo_AbC123', 'cat': 'radarr', 'percentage': '42', 'status': 'Paused'}]}}
HISTORY = {'history': {'slots': [
    {'nzo_id': 'SABnzbd_nzo_Done01', 'category': 'sonarr', 'status': 'Completed', 'storage': '/sab/complete/sonarr/Show.S01E01'},
    {'nzo_id': 'SABnzbd_nzo_Bad03', 'category': 'sonarr', 'status': 'Failed', 'storage': ''},
    {'nzo_id': 'SABnzbd_nzo_Busy02', 'category': 'sonarr', 'status': 'Extracting', 'storage': ''}]}}


def sab(**docs):
    return sab_hold.Sab('http://sab.invalid:8080/', 'KEY123', http=Http({'queue': QUEUE, 'history': HISTORY,
                                                                          'change_cat': {'status': True}, **docs}))


def test_rows_map_the_queue_and_history_onto_the_torrent_shape():
    r = sab_hold.rows(sab())
    q = r['sabnzbd_nzo_abc123']
    assert q == {'label': 'radarr', 'is_finished': False, 'progress': 42.0, 'usenet': True, 'status': 'Paused',
                 'paused': True}
    d = r['sabnzbd_nzo_done01']
    assert d['is_finished'] and d['in_history'] and d['progress'] == 100.0 and d['label'] == 'sonarr'
    assert d['storage'] == '/sab/complete/sonarr/Show.S01E01' and not d['failed']
    assert r['sabnzbd_nzo_bad03']['failed'] and not r['sabnzbd_nzo_bad03']['is_finished']
    b = r['sabnzbd_nzo_busy02']
    assert not b['is_finished'] and b['in_history']


def test_change_cat_resume_and_delete_send_the_id_as_sabnzbd_spelled_it():
    s = sab()
    sab_hold.rows(s)
    s.change_cat('sabnzbd_nzo_abc123', 'watch-hold')
    s.resume('sabnzbd_nzo_abc123')
    s.delete_history('sabnzbd_nzo_abc123')
    (_, p1), (_, p2), (_, p3) = s._http.calls[-3:]
    assert p1['mode'] == 'change_cat' and p1['value'] == 'SABnzbd_nzo_AbC123' and p1['value2'] == 'watch-hold'
    assert p2['mode'] == 'queue' and p2['name'] == 'resume' and p2['value'] == 'SABnzbd_nzo_AbC123'
    assert p3['mode'] == 'history' and p3['name'] == 'delete' and p3['value'] == 'SABnzbd_nzo_AbC123' and p3['del_files'] == 1
    assert not hasattr(s, 'pause'), 'the hold never pauses a job'


def test_errors_and_repr_never_carry_the_key_or_url():
    s = sab(queue=OSError('connect to http://sab.invalid:8080/api?apikey=KEY123'))
    with pytest.raises(sab_hold.SabError) as e:
        s.queue()
    assert 'KEY123' not in str(e.value) and 'sab.invalid' not in str(e.value)
    assert 'KEY123' not in repr(s) and 'sab.invalid' not in repr(s)
    s2 = sab(change_cat={'status': False})
    with pytest.raises(sab_hold.SabError):
        s2.change_cat('sabnzbd_nzo_x', 'watch-hold')


def test_is_usenet_id_and_path_map():
    assert sab_hold.is_usenet_id('SABnzbd_nzo_x') and not sab_hold.is_usenet_id('a' * 40)
    pairs = sab_hold.parse_path_map('/sab/complete=/data/Downloads,/sab=/other')
    assert sab_hold.to_local('/sab/complete/radarr/x', pairs) == '/data/Downloads/radarr/x'
    assert sab_hold.to_local('/sab/other', pairs) == '/other/other'
    assert sab_hold.to_local('/elsewhere/x', pairs) == '/elsewhere/x'


def test_to_arr_swaps_the_complete_folder_prefix_only():
    f = sab_hold.to_arr
    assert f('/sab/complete/watch-hold/M', '/sab/complete', '/data/dl') == '/data/dl/watch-hold/M'
    assert f('/sab/complete', '/sab/complete/', '/data/dl/') == '/data/dl'
    assert f('/sab/complete/x', '/sab/complete', '/sab/complete') == '/sab/complete/x'
    assert f('/sab/complete/x', '', '/data/dl') == '/sab/complete/x'
    assert f('/sab/completely/x', '/sab/complete', '/data/dl') == '/sab/completely/x'
    assert f('/elsewhere/x', '/sab/complete', '/data/dl') == '/elsewhere/x'


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


class Posts:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        return Resp({})


def arr_io(monkeypatch, sab_client=None, get=None):
    monkeypatch.setattr(aw, '_sab_client', sab_client or sab())
    posts = Posts()
    monkeypatch.setattr(aw.requests, 'post', posts)
    io = aw.WatchIO({})
    io._arrs = {'Radarr': ('http://radarr.invalid', 'k'), 'Sonarr': ('http://sonarr.invalid', 'k')}
    if get:
        io._get = lambda who, path, params=None: get(who, path, params)
    return io, posts


def test_watchio_merges_sabnzbd_rows_and_routes_set_label_to_change_cat(monkeypatch):
    FakeDeluge(monkeypatch, {'a' * 40: {'label': 'radarr'}})
    s = sab()
    monkeypatch.setattr(aw, '_sab_client', s)
    io = aw.WatchIO({})
    t = io.torrents()
    assert 'a' * 40 in t and 'sabnzbd_nzo_abc123' in t
    io.set_label('sabnzbd_nzo_abc123', 'watch-hold')
    assert s._http.calls[-1][1]['mode'] == 'change_cat' and s._http.calls[-1][1]['value2'] == 'watch-hold'
    io.resume('sabnzbd_nzo_abc123')
    assert s._http.calls[-1][1]['name'] == 'resume'
    io.forget_usenet('sabnzbd_nzo_abc123')
    assert s._http.calls[-1][1]['name'] == 'delete' and s._http.calls[-1][1]['del_files'] == 1


def test_import_command_for_radarr_and_sonarr(monkeypatch):
    monkeypatch.setattr(aw, 'SABNZBD_COMPLETE_DIR', '/sab/complete')
    monkeypatch.setattr(aw, 'SABNZBD_ARR_VISIBLE_DIR', '/data/dl')
    s = sab()
    sab_hold.rows(s)
    io, posts = arr_io(monkeypatch, s)
    row = {'storage': '/sab/complete/watch-hold/Some.Movie.2001'}
    io.import_usenet('sabnzbd_nzo_abc123', 'Radarr', row)
    io.import_usenet('sabnzbd_nzo_abc123', 'Sonarr', row)
    (u1, b1), (u2, b2) = posts.calls
    assert u1 == 'http://radarr.invalid/api/v3/command' and u2 == 'http://sonarr.invalid/api/v3/command'
    assert b1 == {'name': 'DownloadedMoviesScan', 'path': '/data/dl/watch-hold/Some.Movie.2001',
                  'downloadClientId': 'SABnzbd_nzo_AbC123', 'importMode': 'Move'}
    assert b2['name'] == 'DownloadedEpisodesScan' and b2['importMode'] == 'Move'
    with pytest.raises(RuntimeError):
        io.import_usenet('sabnzbd_nzo_abc123', 'Radarr', {'storage': ''})


def test_arr_imported_by_history_or_by_the_old_file_having_changed(monkeypatch):
    state = {'hist': [], 'path': '/m/old.mkv'}

    def get(who, path, params=None):
        return {'records': state['hist']}
    io, _ = arr_io(monkeypatch, get=get)
    io.items = lambda who, ids: {f'm{i}': {'old': state['path']} for i in ids}
    e = {'olds': {'m7': '/m/old.mkv'}}
    assert not io.arr_imported('Radarr', 'sabnzbd_nzo_abc123', e)
    state['path'] = '/m/new.mkv'                        # the scan dropped the id from history but swapped the file
    assert io.arr_imported('Radarr', 'sabnzbd_nzo_abc123', e)
    state['path'] = '/m/old.mkv'
    state['hist'] = [{'eventType': 'downloadFolderImported', 'downloadId': 'SABNZBD_NZO_ABC123'}]
    assert io.arr_imported('Radarr', 'sabnzbd_nzo_abc123', {})


def test_mark_failed_posts_the_grab_records_id(monkeypatch):
    recs = [{'id': 5, 'eventType': 'downloadFolderImported', 'downloadId': 'X'},
            {'id': 9, 'eventType': 'grabbed', 'downloadId': 'SABnzbd_nzo_AbC123'},
            {'id': 4, 'eventType': 'grabbed', 'downloadId': 'OTHER'}]
    io, posts = arr_io(monkeypatch, get=lambda who, path, params=None: {'records': recs})
    assert io.mark_failed('Radarr', 'sabnzbd_nzo_abc123') is True
    assert posts.calls == [('http://radarr.invalid/api/v3/history/failed/9', None)]
    recs.clear()
    assert io.mark_failed('Radarr', 'sabnzbd_nzo_abc123') is False and len(posts.calls) == 1


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
