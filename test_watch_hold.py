"""Watch hold (invisible upgrades): the audio mapping on synthetic sound, the import gate, stream matching, the
hold/prep/release pass, the post-import writes, and the webhook wiring. Everything is faked; nothing reaches
Plex, Deluge, Sonarr, Radarr or ffmpeg. Names and paths are made up."""
import copy as copy_mod
import importlib.util

import numpy as np
import pytest

import watch_hold as wh

spec = importlib.util.spec_from_file_location("aw_watch", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

H1, H2 = 'a' * 40, 'b' * 40
NOW = 1_800_000_000.0
RATE = wh.RATE
OLD = '/data/Media/Movies/Some Movie (2001)/Some Movie (2001).mkv'
NEW = '/data/Downloads/Some.Movie.2001.2160p/Some.Movie.2001.2160p.mkv'
NEW_LIB = '/data/Media/Movies/Some Movie (2001)/Some Movie (2001) 2160p.mkv'


# ── synthetic sound ───────────────────────────────────────────────────────────

def speechy(seconds, seed):
    """Noise under a loudness that changes every 100 ms, with pauses: the shape the mapping keys on."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    seg = int(0.1 * RATE)
    amps = np.exp(rng.normal(0, 1.2, n // seg + 1))
    amps[rng.random(len(amps)) < 0.3] *= 0.02
    x = rng.normal(0, 1, n) * np.repeat(amps, seg)[:n]
    return (x / np.abs(x).max() * 20000).astype('<i2')


def reader(arr):
    def read(s, d):
        a, b = max(int(s * RATE), 0), max(int((s + d) * RATE), 0)
        return arr[a:b].tobytes()
    return read


def speed(arr, factor):
    """Play `arr` `factor` times faster (PAL speed-up: 25/23.976)."""
    n = int(len(arr) / factor)
    return np.interp(np.arange(n) * factor, np.arange(len(arr)), arr.astype(np.float64)).astype('<i2')


@pytest.fixture(scope='module')
def film():
    return speechy(1200, 1)


# ── the audio mapping ─────────────────────────────────────────────────────────

def test_map_longer_intro(film):
    new = np.concatenate((speechy(7.3, 2), film))
    r = wh.map_offset(600, reader(film), reader(new), 1200, len(new) / RATE)
    assert r['ok'] and abs(r['new_s'] - 607.3) < 0.05 and r['rate'] == 1.0


def test_map_shorter_intro_early_point(film):
    new = film[int(12.5 * RATE):]
    r = wh.map_offset(100, reader(film), reader(new), 1200, len(new) / RATE)   # confirm window after the point
    assert r['ok'] and abs(r['new_s'] - 87.5) < 0.05


def test_map_pal_speedup(film):
    new = np.concatenate((speechy(3, 3), speed(film, wh.PAL)))
    r = wh.map_offset(600, reader(film), reader(new), 1200, len(new) / RATE)
    assert r['ok'] and abs(r['rate'] - 1 / wh.PAL) < 1e-9
    assert abs(r['new_s'] - (3 + 600 / wh.PAL)) < 0.1


def test_map_unrelated_sound_maps_nothing(film):
    other = speechy(1200, 9)
    r = wh.map_offset(600, reader(film), reader(other), 1200, 1200)
    assert not r['ok'] and r['new_s'] is None and r['why'] in ('no_match', 'disagree')


def test_map_cut_between_the_windows_is_not_trusted(film):
    # an extended cut: 20 s added between the confirm window and the point; the two windows disagree
    new = np.concatenate((speechy(5, 4), film[:500 * RATE], speechy(20, 5), film[500 * RATE:]))
    r = wh.map_offset(600, reader(film), reader(new), 1200, len(new) / RATE)
    assert not r['ok'] and r['why'] == 'disagree'


def test_map_read_failure_and_edges(film):
    def boom(s, d):
        raise OSError('gone')
    assert wh.map_offset(600, boom, reader(film), 1200, 1200)['why'] == 'read_failed'
    assert wh.map_offset(1199, reader(film), reader(film), 1200, 1200)['ok']          # the window is clipped
    assert wh.map_offset(1215, reader(film), reader(film), 1200, 1200)['why'] == 'edge'
    assert wh.map_offset(600, lambda s, d: b'', reader(film), 1200, 1200)['why'] == 'short_read'


def test_rates():
    assert wh.rates(1200, 1200) == [1.0]
    assert wh.rates(None, 1200) == [1.0]
    r = wh.rates(1200, 1200 / wh.PAL)
    assert 1 / wh.PAL in r and len(r) == 3
    assert wh.rates(1200, 1300) == [1.0]                               # 8% off: another cut, not a speed


# ── one user's view, stream matching ──────────────────────────────────────────

def meta(file, offset=0, dur=1_200_000, viewed=None, audio_sel=0, subs_sel=None, rk='100', ids=(11, 12, 13, 14)):
    aud = [{'id': ids[0], 'streamType': 2, 'index': 1, 'languageCode': 'eng', 'codec': 'truehd', 'channels': 8,
            'default': True},
           {'id': ids[1], 'streamType': 2, 'index': 2, 'languageCode': 'eng', 'codec': 'ac3', 'channels': 2,
            'title': 'Commentary'}]
    sub = [{'id': ids[2], 'streamType': 3, 'index': 3, 'languageCode': 'eng', 'codec': 'srt'},
           {'id': ids[3], 'streamType': 3, 'index': 4, 'languageCode': 'spa', 'codec': 'srt'}]
    aud[audio_sel]['selected'] = True
    if subs_sel is not None:
        sub[subs_sel]['selected'] = True
    m = {'ratingKey': rk, 'viewOffset': offset, 'duration': dur,
         'Media': [{'Part': [{'id': 900 if file == OLD else 901, 'file': file, 'Stream': aud + sub}]}]}
    if viewed:
        m['lastViewedAt'] = viewed
    return m


def test_user_view_keeps_only_real_choices():
    m = meta('/x/Some Movie (2001).mkv', offset=600_000, audio_sel=1, subs_sel=1)
    _m, _med, p = wh.find_part([m], OLD)                               # another mount prefix, same file name
    v = wh.user_view(m, p)
    assert v['offset_ms'] == 600_000 and v['audio']['title'] == 'commentary' and v['subs']['lang'] == 'spa'
    v = wh.user_view(m, wh.find_part([meta(OLD)], OLD)[2])
    assert v['audio'] is None and v['subs'] == 'off'                   # the default track is not carried over


def test_needs_map():
    assert wh.needs_map({'offset_ms': 600_000, 'dur_ms': 1_200_000, 'viewed_at': NOW - 86400}, NOW, 30 * 86400)
    assert not wh.needs_map({'offset_ms': 20_000, 'dur_ms': 1_200_000}, NOW, 30 * 86400)          # first 30 s
    assert not wh.needs_map({'offset_ms': 1_150_000, 'dur_ms': 1_200_000}, NOW, 30 * 86400)       # past 95%
    assert not wh.needs_map({'offset_ms': 600_000, 'dur_ms': 1_200_000, 'viewed_at': NOW - 40 * 86400}, NOW,
                            30 * 86400)                                                           # stale


def test_plan_picks_matches_language_commentary_and_off():
    old = wh.find_part([meta(OLD, audio_sel=1, subs_sel=1)], OLD)
    v = wh.user_view(old[0], old[2])
    new = wh.find_part([meta(NEW_LIB, ids=(21, 22, 23, 24), subs_sel=0)], NEW_LIB)[2]
    assert wh.plan_picks(v, new) == {'audio': 22, 'subs': 24}
    same = wh.find_part([meta(NEW_LIB, ids=(21, 22, 23, 24), audio_sel=1, subs_sel=1)], NEW_LIB)[2]
    assert wh.plan_picks(v, same) == {}                                # Plex already has the same picks
    off = {'audio': None, 'subs': 'off'}
    assert wh.plan_picks(off, new) == {'subs': 0}
    forced = wh.find_part([meta(NEW_LIB, ids=(21, 22, 23, 24), subs_sel=0)], NEW_LIB)[2]
    forced['Stream'][2]['forced'] = True
    assert wh.plan_picks(off, forced) == {}                            # forced subtitles stay on


def test_match_stream_never_crosses_language_or_commentary():
    cands = [{'id': 1, 'streamType': 2, 'languageCode': 'fra', 'codec': 'ac3', 'channels': 6},
             {'id': 2, 'streamType': 2, 'languageCode': 'eng', 'codec': 'ac3', 'channels': 2, 'title': 'Commentary'}]
    assert wh.match_stream({'lang': 'eng', 'title': '', 'codec': 'ac3', 'channels': 6}, cands) is None
    assert wh.match_stream({'lang': 'eng', 'title': 'director commentary'}, cands)['id'] == 2
    assert wh.match_stream({'lang': '', 'title': ''}, cands)['id'] == 1


def test_probe_audio_prefers_the_same_language():
    doc = {'streams': [{'index': 0, 'codec_type': 'video'},
                       {'index': 1, 'codec_type': 'audio', 'tags': {'language': 'jpn'}, 'disposition': {'default': 1}},
                       {'index': 2, 'codec_type': 'audio', 'tags': {'language': 'eng'}}],
           'format': {'duration': '1207.3'}}
    assert wh.probe_audio(doc, 'eng') == (2, 1207.3)
    assert wh.probe_audio(doc, 'ger') == (1, 1207.3)
    assert wh.probe_audio({'streams': []}, 'eng') == (None, None)


def test_parse_sessions_and_matching():
    doc = {'MediaContainer': {'Metadata': [
        {'ratingKey': '100', 'Player': {'state': 'paused'}, 'User': {'title': 'someone'},
         'Media': [{'Part': [{'file': '/plex/Movies/Some Movie (2001)/Some Movie (2001).mkv'}]}]}]}}
    s = wh.parse_sessions(doc)
    assert s == [{'key': '100', 'files': ['/plex/Movies/Some Movie (2001)/Some Movie (2001).mkv'], 'state': 'paused'}]
    entry = {'items': {'m7': {'old': OLD, 'plex': None}}}
    assert wh.streamed(entry, s) and not wh.playing(entry, s)          # matched by file name alone
    assert wh.parse_sessions(None) == []


# ── the gate ──────────────────────────────────────────────────────────────────

def entry_with(snap=None, maps=None, done_at=NOW - 60, snap_at=NOW - 60, since=NOW - 3600):
    return {'who': 'Radarr', 'since': since, 'done_at': done_at, 'multi': False, 'remote': False,
            'items': {'m7': {'old': OLD, 'plex': '100', 'snap': snap or {}, 'snap_at': snap_at, 'maps': maps or {}}}}


KID = {'offset_ms': 600_000, 'dur_ms': 1_200_000, 'viewed_at': NOW - 3600, 'audio': None, 'subs': None}
PLAYING = [{'key': '100', 'files': [], 'state': 'playing'}]
PAUSED = [{'key': '100', 'files': [], 'state': 'paused'}]


def test_ready_rules():
    cfg = wh.Cfg('on')
    mapped = {'kid': {'old_ms': 600_000, 'ok': True, 'new_ms': 607_300}}
    assert wh.ready(entry_with(), {}, [], NOW, cfg) == (True, 'ok')
    assert wh.ready(entry_with(), {}, PLAYING, NOW, cfg) == (False, 'playing')
    assert wh.ready(entry_with(), {}, PAUSED, NOW, cfg) == (False, 'recent')
    assert wh.ready(entry_with(), {'rk:100': NOW - 300}, [], NOW, cfg) == (False, 'recent')
    assert wh.ready(entry_with(snap_at=NOW - 800), {'rk:100': NOW - 700}, [], NOW, cfg) == (False, 'snapshot')
    assert wh.ready(entry_with(snap_at=NOW - 2000), {}, [], NOW, cfg) == (False, 'snapshot')       # too old
    assert wh.ready(entry_with(snap={'kid': KID}), {}, [], NOW, cfg) == (False, 'prep')
    assert wh.ready(entry_with(snap={'kid': KID}, maps=mapped), {}, [], NOW, cfg) == (True, 'ok')
    moved = dict(KID, offset_ms=650_000)                               # watched on since the mapping
    assert wh.ready(entry_with(snap={'kid': moved}, maps=mapped), {}, [], NOW, cfg) == (False, 'prep')


def test_ready_caps():
    cfg = wh.Cfg('on', max_hours=48)
    old = entry_with(snap={'kid': KID}, done_at=NOW - 49 * 3600)
    assert wh.ready(old, {}, [], NOW, cfg) == (True, 'max_hours')
    assert wh.ready(old, {}, PAUSED, NOW, cfg) == (False, 'recent')    # never under a paused stream either
    assert wh.ready(old, {}, PLAYING, NOW, cfg) == (False, 'playing')  # never under a playing stream
    assert wh.ready(old, {'rk:100': NOW - 300}, [], NOW, cfg) == (False, 'recent')   # stopped 5 min ago
    assert wh.ready(old, {'rk:100': NOW - 700}, [], NOW, cfg) == (True, 'max_hours')
    stuck = entry_with(snap={'kid': KID}, done_at=None, since=NOW - 10 * 86400)
    assert wh.ready(stuck, {}, [], NOW, cfg) == (True, 'max_hours')    # never seen finished: a longer cap
    stale = dict(KID, viewed_at=NOW - 60 * 86400)
    assert wh.ready(entry_with(snap={'kid': stale}), {}, [], NOW, cfg) == (True, 'ok')


def test_season_packs_and_remote_downloads_are_not_mapped():
    e = entry_with(snap={'kid': KID})
    e['multi'] = True
    assert not wh.needs_prep(e, NOW, 30 * 86400)
    e['multi'], e['remote'] = False, True
    assert not wh.needs_prep(e, NOW, 30 * 86400)


def test_mode_parsing():
    assert wh.parse_mode('ON') == 'on' and wh.parse_mode('') == 'dry' and wh.parse_mode('maybe') == 'dry'
    assert wh.Cfg('on').acts and not wh.Cfg('dry').acts and not wh.Cfg('off').acts


# ── the pass, end to end over a fake world ────────────────────────────────────

class FakeIO:
    glacier_label = 'glacier-hold'

    def __init__(self, film=None):
        self.t, self.q, self.arr, self.keys, self.meta = {}, {'Sonarr': [], 'Radarr': []}, {}, {}, {}
        self.sess, self.pcm, self.new, self.grabs = [], {}, {}, {}
        self.labels, self.refreshed, self.writes, self.saves = [], [], [], 0
        self.us = [{'label': 'owner', 'token': 'tok-o'}, {'label': 'kid', 'token': 'tok-k'}]
        self.probe_doc = {'streams': [{'index': 1, 'codec_type': 'audio', 'tags': {'language': 'eng'},
                                       'disposition': {'default': 1}}], 'format': {'duration': '1207.3'}}
        self.found = '100'
        self.reads = 0

    @staticmethod
    def to_local(p):
        return p

    def services(self):
        return ['Sonarr', 'Radarr']

    def queue(self, who):
        return list(self.q[who])

    def torrents(self):
        return {h: dict(v) for h, v in self.t.items()}

    def sessions(self):
        return list(self.sess)

    def users(self):
        return self.us

    def user_meta(self, token, key):
        return self.meta.get((token, key))

    def items(self, who, ids):
        p = 'e' if who == 'Sonarr' else 'm'
        return {f'{p}{i}': dict(self.arr[(who, i)]) for i in ids if (who, i) in self.arr}

    def plex_key(self, who, info):
        return self.keys.get(info['old'])

    def who_of(self, h, label):
        return 'Radarr' if label.startswith('radarr') else 'Sonarr' if label.startswith('sonarr') else None

    def grab_ids(self, who, h):
        return self.grabs.get(h, ([], False))

    def can_read_old(self, entry):
        return True

    def new_file(self, h):
        return self.new.get(h)

    def probe(self, path):
        return self.probe_doc

    def read_pcm(self, path, idx, s, d):
        self.reads += 1
        return reader(self.pcm[path])(s, d)

    def set_label(self, h, label):
        self.t[h]['label'] = label
        self.labels.append((h, label))

    def refresh(self, who):
        self.refreshed.append(who)

    def save(self, doc):
        self.saves += 1

    def find_new(self, item, new_path):
        return self.found

    def set_progress(self, token, key, ms):
        self.writes.append(('progress', token, key, ms))

    def set_streams(self, token, part_id, audio=None, subs=None):
        self.writes.append(('streams', token, part_id, audio, subs))


def movie_world(film, label='radarr', progress=60.0):
    io = FakeIO()
    io.t[H1] = {'label': label, 'progress': progress, 'is_finished': False}
    io.q['Radarr'] = [{'downloadId': H1.upper(), 'movieId': 7, 'trackedDownloadState': 'downloading'}]
    io.arr[('Radarr', 7)] = {'old': OLD, 'title': 'Some Movie', 'who': 'Radarr'}
    io.keys[OLD] = '100'
    io.meta[('tok-o', '100')] = meta(OLD)
    io.meta[('tok-k', '100')] = meta(OLD, offset=600_000, viewed=NOW - 3600, audio_sel=1, subs_sel=1)
    io.pcm[OLD] = film
    io.pcm[NEW] = np.concatenate((speechy(7.3, 2), film))
    return io


def test_tick_holds_maps_and_releases(film):
    io, doc, cfg, checked = movie_world(film), {}, wh.Cfg('on'), set()
    c = wh.tick(io, doc, cfg, NOW, checked)
    assert c['tracked'] == 1 and c['held'] == 1
    assert io.t[H1]['label'] == wh.HOLD_LABEL and doc['held'][H1]['label'] == 'radarr'
    item = doc['tracked'][H1]['items']['m7']
    assert set(item['snap']) == {'owner', 'kid'} and item['plex'] == '100'
    # finished: mapped, then released in the same pass
    io.t[H1].update(progress=100.0, is_finished=True)
    io.new[H1] = NEW
    c = wh.tick(io, doc, cfg, NOW + 15, checked)
    m = doc['tracked'][H1]['items']['m7']['maps']['kid']
    assert m['ok'] and abs(m['new_ms'] - 607_300) < 50 and c['mapped_ok'] == 1
    assert c['released'] == 1 and io.t[H1]['label'] == 'radarr' and io.refreshed == ['Radarr']
    assert doc['held'] == {}
    # nothing more to do on the next passes
    n = io.reads
    c = wh.tick(io, doc, cfg, NOW + 30, checked, discover_now=False)
    assert io.reads == n and not c.get('held')


def test_dry_run_never_relabels(film):
    io, doc = movie_world(film), {}
    c = wh.tick(io, doc, wh.Cfg('dry'), NOW, set())
    assert c['would_hold'] == 1 and io.labels == [] and doc['held'] == {}
    io.t[H1].update(progress=100.0, is_finished=True)
    io.new[H1] = NEW
    c = wh.tick(io, doc, wh.Cfg('dry'), NOW + 15, set())
    assert c['mapped_ok'] == 1 and io.labels == []                    # the reads still run


def test_streaming_holds_early_and_waits_for_quiet(film):
    io, doc, cfg, checked = movie_world(film, progress=5.0), {}, wh.Cfg('on'), set()
    io.sess = [{'key': '100', 'files': [], 'state': 'playing'}]
    assert wh.tick(io, doc, cfg, NOW, checked)['held'] == 1            # streamed: held at 5%
    io.t[H1].update(progress=100.0, is_finished=True)
    io.new[H1] = NEW
    c = wh.tick(io, doc, cfg, NOW + 600, checked)
    assert c['kept_playing'] == 1 and not c.get('mapped_ok')           # no reads under a stream
    # they stop 10 s into the next minute, a bit further on
    io.sess = []
    io.meta[('tok-k', '100')] = meta(OLD, offset=640_000, viewed=NOW + 600, audio_sel=1, subs_sel=1)
    c = wh.tick(io, doc, cfg, NOW + 615, checked)
    assert c['kept_recent'] == 1
    c = wh.tick(io, doc, cfg, NOW + 615 + 601, checked)                # quiet: snapshot again, map, release
    m = doc['tracked'][H1]['items']['m7']['maps']['kid']
    assert c['snapshots'] == 1 and m['old_ms'] == 640_000 and abs(m['new_ms'] - 647_300) < 50
    assert c['released'] == 1 and io.t[H1]['label'] == 'radarr'


def test_lane_download_is_tracked_not_held(film):
    io, doc = movie_world(film, label='radarr-upgrade', progress=100.0), {}
    io.q['Radarr'] = []                                                # the lane is hidden from the arr
    io.grabs[H1] = ([7], False)
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set(), lane_labels=('radarr-upgrade',))
    assert c['tracked'] == 1 and not c.get('held') and io.labels == []
    assert wh.ready(doc['tracked'][H1], doc['seen'], [], NOW, wh.Cfg('on'))[1] == 'prep'


def test_not_an_upgrade_is_checked_once(film):
    io, doc, checked = movie_world(film), {}, set()
    io.arr.clear()                                                     # no current file: a first download
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, checked)
    assert c['not_upgrade'] == 1 and H1 in checked and doc['tracked'] == {}
    assert not wh.tick(io, doc, wh.Cfg('on'), NOW + 15, checked).get('not_upgrade')


def test_sessions_unreadable_does_nothing(film):
    io, doc = movie_world(film), {'held': {H1: {'label': 'radarr', 'who': 'Radarr'}}}

    def boom():
        raise OSError('plex down')
    io.sessions = boom
    io.t[H1]['label'] = wh.HOLD_LABEL
    c = wh.tick(io, doc, wh.Cfg('on'), NOW, set())
    assert c == {'sessions_unreadable': 1} and io.labels == []


def test_mode_off_releases_everything(film):
    io = movie_world(film)
    io.t[H1]['label'] = wh.HOLD_LABEL
    doc = {'held': {H1: {'label': 'radarr', 'who': 'Radarr'}}, 'tracked': {H1: entry_with(snap={'kid': KID})}}
    c = wh.tick(io, doc, wh.Cfg('off'), NOW, set())
    assert c['released'] == 1 and io.t[H1]['label'] == 'radarr'


def test_release_drops_holds_gone_or_relabelled(film):
    io = movie_world(film)
    io.t[H1]['label'] = 'radarr'                                       # someone else changed it
    doc = {'held': {H1: {'label': 'radarr', 'who': 'Radarr'}, H2: {'label': 'sonarr', 'who': 'Sonarr'}},
           'tracked': {}, 'seen': {}}
    c = wh.Counts()
    wh.release_pass(io, doc, [], NOW, wh.Cfg('on'), c)
    assert c['relabelled_elsewhere'] == 1 and c['gone'] == 1 and doc['held'] == {}
    assert wh.reconcile(io, {'held': {}}) == {}


# ── after the import ──────────────────────────────────────────────────────────

def mapped_entry():
    e = entry_with(snap={'kid': wh.user_view(*[x for i, x in enumerate(wh.find_part(
        [meta(OLD, offset=600_000, viewed=NOW - 3600, audio_sel=1, subs_sel=1)], OLD)) if i != 1])},
        maps={'kid': {'old_ms': 600_000, 'ok': True, 'new_ms': 607_300}})
    return e


def test_after_import_writes_the_mapped_point_and_picks():
    io = FakeIO()
    io.meta[('tok-k', '100')] = meta(NEW_LIB, offset=600_000, ids=(21, 22, 23, 24))
    c = wh.after_import(io, mapped_entry(), ['m7'], NEW_LIB, wh.Cfg('on'))
    assert ('progress', 'tok-k', '100', 607_300) in io.writes
    assert ('streams', 'tok-k', 901, 22, 24) in io.writes
    assert c['shift'] == 1 and c['picks'] == 1


def test_after_import_leaves_a_point_that_moved_since():
    io = FakeIO()
    io.meta[('tok-k', '100')] = meta(NEW_LIB, offset=900_000, ids=(21, 22, 23, 24))   # they watched on, elsewhere
    c = wh.after_import(io, mapped_entry(), ['m7'], NEW_LIB, wh.Cfg('on'))
    assert not [w for w in io.writes if w[0] == 'progress'] and c['watched_since'] == 1
    assert ('streams', 'tok-k', 901, 22, 24) in io.writes              # the choices still carry over


def test_after_import_new_item_gets_the_old_point_back():
    io = FakeIO()
    io.found = '555'                                                   # Plex made a new item for the new file
    io.meta[('tok-k', '555')] = meta(NEW_LIB, offset=0, rk='555', ids=(21, 22, 23, 24))
    e = mapped_entry()
    e['items']['m7']['maps'] = {}
    wh.after_import(io, e, ['m7'], NEW_LIB, wh.Cfg('on'))
    assert ('progress', 'tok-k', '555', 600_000) in io.writes


def test_after_import_dry_run_writes_nothing():
    io = FakeIO()
    io.meta[('tok-k', '100')] = meta(NEW_LIB, offset=600_000, ids=(21, 22, 23, 24))
    c = wh.after_import(io, mapped_entry(), ['m7'], NEW_LIB, wh.Cfg('dry'))
    assert io.writes == [] and c['would_write_offset'] == 1 and c['would_write_picks'] == 1


def test_after_import_plex_never_lists_it():
    io = FakeIO()
    io.found = None
    assert wh.after_import(io, mapped_entry(), ['m7'], NEW_LIB, wh.Cfg('on'))['plex_missing'] == 1


def test_item_keys_and_drop_items():
    assert wh.item_keys_of('Sonarr', {'episodes': [{'id': 1}, {'id': 2}]}) == ['e1', 'e2']
    assert wh.item_keys_of('Radarr', {'movie': {'id': 7}}) == ['m7']
    doc = {'tracked': {H1: {'items': {'e1': {}, 'e2': {}}}}}
    wh.drop_items(doc, H1, ['e1'])
    assert list(doc['tracked'][H1]['items']) == ['e2']
    wh.drop_items(doc, H1, ['e2'])
    assert doc['tracked'] == {}


# ── the webhook wiring ────────────────────────────────────────────────────────

class GateIO:
    def __init__(self, sessions):
        self.s = sessions

    def sessions(self):
        return self.s


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, 'WATCH_STATE', str(tmp_path / 'watch.json'))
    aw._watch_doc.clear()
    aw._watch_doc.update({'held': {}, 'tracked': {H1: entry_with()}, 'seen': {}})
    aw._watch_last.clear()
    yield aw
    aw._watch_doc.clear()


def test_gate_waits_only_when_on(wired, monkeypatch):
    monkeypatch.setattr(aw, '_watch_io', lambda: GateIO(PLAYING))
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('dry'))
    assert aw.watch_gate(H1.upper(), now=NOW) is None                  # dry: logged, never waits
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('on'))
    assert aw.watch_gate(H1, now=NOW) == 'playing'
    assert aw.watch_gate(H2, now=NOW) is None                          # not tracked
    monkeypatch.setattr(aw, '_watch_io', lambda: GateIO([]))
    assert aw.watch_gate(H1, now=NOW) is None
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('off'))
    monkeypatch.setattr(aw, '_watch_io', lambda: GateIO(PLAYING))
    assert aw.watch_gate(H1, now=NOW) is None


def test_label_guard_records_the_label_for_release(wired, monkeypatch):
    aw._watch_doc['held'][H1] = {'label': 'radarr', 'who': 'Radarr'}

    def no_network(*a, **k):
        raise AssertionError('reached Deluge')
    monkeypatch.setattr(aw, '_ensure_deluge_label', no_network)
    aw.set_torrent_label(H1, 'radarr-upgrade')
    assert aw._watch_doc['held'][H1]['label'] == 'radarr-upgrade'
    assert aw._watch_held_hashes() == {H1}
    assert not aw._watch_label_guard(H1, wh.HOLD_LABEL)               # its own label goes through
    assert not aw._watch_label_guard(H2, 'radarr')


def test_on_import_carries_over_and_forgets(wired, monkeypatch):
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('on'))
    aw._watch_doc['tracked'][H1] = mapped_entry()
    io = FakeIO()
    io.meta[('tok-k', '100')] = meta(NEW_LIB, offset=600_000, ids=(21, 22, 23, 24))
    out = aw.watch_on_import({'downloadId': H1.upper(), 'movie': {'id': 7}, 'movieFile': {'path': NEW_LIB}},
                             'Radarr', io=io)
    assert out['shift'] == 1 and ('progress', 'tok-k', '100', 607_300) in io.writes
    assert aw._watch_doc['tracked'] == {}
    assert aw.watch_on_import({'downloadId': H2, 'movie': {'id': 7}, 'movieFile': {'path': NEW_LIB}},
                              'Radarr', io=io) == {}


def test_tick_can_leave_the_mapping_to_the_caller(film):
    io, doc, cfg = movie_world(film, progress=100.0), {}, wh.Cfg('on')
    io.t[H1]['is_finished'] = True
    io.new[H1] = NEW
    asked = []
    c = wh.tick(io, doc, cfg, NOW, set(), prep=lambda h, e: asked.append(h) or True)
    assert asked == [H1] and io.reads == 0 and not c.get('mapped_ok') and not c.get('released')
    copy = copy_mod.deepcopy(doc['tracked'][H1])
    wh.prep_entry(io, H1, copy, NOW, cfg, wh.Counts())
    assert wh.merge_maps(doc, H1, copy) == 1
    c = wh.tick(io, doc, cfg, NOW + 15, set(), discover_now=False)
    assert c['released'] == 1


def test_merge_maps_drops_points_that_moved_meanwhile(film):
    doc = {'tracked': {H1: entry_with(snap={'kid': dict(KID, offset_ms=650_000)})}}
    done = entry_with(snap={'kid': KID}, maps={'kid': {'old_ms': 600_000, 'ok': True, 'new_ms': 607_300}})
    assert wh.merge_maps(doc, H1, done) == 0 and doc['tracked'][H1]['items']['m7']['maps'] == {}
    assert wh.merge_maps(doc, H2, done) == 0


def test_webhook_maps_outside_its_lock_then_releases(wired, monkeypatch, film):
    import threading
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('on'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, '_watch_checked', set())
    aw._watch_doc['tracked'].clear()
    io = movie_world(film, progress=100.0)
    io.t[H1]['is_finished'] = True
    io.new[H1] = NEW
    free, read = [], io.read_pcm

    def try_lock():
        ok = aw._watch_lock.acquire(timeout=2)
        free.append(ok)
        if ok:
            aw._watch_lock.release()

    def read_pcm(*a):
        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        return read(*a)
    io.read_pcm = read_pcm
    c = aw.watch_tick(io=io, now=NOW)
    assert c['held'] == 1 and c['mapped_ok'] == 1 and c['maps_kept'] == 1 and free and all(free)
    assert aw._watch_doc['tracked'][H1]['items']['m7']['maps']['kid']['ok']
    c = aw.watch_tick(io=io, now=NOW + 15, discover_now=False)
    assert c['released'] == 1 and io.t[H1]['label'] == 'radarr'
    assert aw._watch_prep_lock.acquire(blocking=False)
    aw._watch_prep_lock.release()


def test_webhook_a_second_pass_does_not_map_while_one_is(wired, monkeypatch, film):
    monkeypatch.setattr(aw, 'WATCH_CFG', wh.Cfg('on'))
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, '_watch_checked', set())
    aw._watch_doc['tracked'].clear()
    io = movie_world(film, progress=100.0)
    io.t[H1]['is_finished'] = True
    io.new[H1] = NEW
    assert aw._watch_prep_lock.acquire(blocking=False)
    try:
        c = aw.watch_tick(io=io, now=NOW)
    finally:
        aw._watch_prep_lock.release()
    assert c['tracked'] == 1 and io.reads == 0 and not c.get('mapped_ok')
