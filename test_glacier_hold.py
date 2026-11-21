"""Glacier disk hold: window, spin log, index, hold/release, the stopgap link, restart safety, and the tick wiring.
Everything is faked; nothing reaches Deluge, Sonarr, Radarr or a disk. Names and paths are made up."""
import importlib.util
import os
from datetime import datetime

import glacier_hold as gh

spec = importlib.util.spec_from_file_location("aw_glacier", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

H1, H2, H3 = 'a' * 40, 'b' * 40, 'c' * 40
NOW = 1_800_000_000.0
PREFIXES = frozenset({'data/Media/TV Shows/Cold Show', 'data/Media/Movies/Cold Movie (2001)'})


# ── window / spin / index ─────────────────────────────────────────────────────

def test_window_wraps_midnight():
    f = lambda h, m: gh.in_window(datetime(2026, 1, 1, h, m), '22:00-06:00')  # noqa: E731
    assert f(22, 0) and f(23, 59) and f(0, 0) and f(5, 59)
    assert not f(6, 0) and not f(12, 0) and not f(21, 59)


def test_window_same_day_and_bad_spec():
    assert gh.in_window(datetime(2026, 1, 1, 3, 0), '02:00-04:00')
    assert not gh.in_window(datetime(2026, 1, 1, 4, 0), '02:00-04:00')
    assert gh.in_window(datetime(2026, 1, 1, 23, 0), 'garbage')       # falls back to the default window
    assert not gh.in_window(datetime(2026, 1, 1, 12, 0), '05:00-05:00')


def write_spin(tmp_path, name, rows):
    (tmp_path / name).write_text(''.join('\t'.join(str(c) for c in r) + '\n' for r in rows))


def test_spin_newest_disk1_row_wins_and_ignores_other_disks(tmp_path):
    write_spin(tmp_path, 'spin-2026-01-01.tsv', [(100, 'disk1', 'up'), (200, 'disk1', 'down'), (250, 'disk2', 'up')])
    write_spin(tmp_path, 'spin-2026-01-02.tsv', [(300, 'disk1', 'up'), ('bad', 'disk1', 'up'), (310, 'disk1', 'weird')])
    assert gh.newest_disk1_state(str(tmp_path)) == (300.0, True)
    write_spin(tmp_path, 'spin-2026-01-03.tsv', [(400, 'disk1', 'down')])
    assert gh.newest_disk1_state(str(tmp_path)) == (400.0, False)


def test_spin_missing_dir_and_no_rows(tmp_path):
    assert gh.newest_disk1_state(str(tmp_path / 'nope')) is None
    assert gh.newest_disk1_state('') is None
    write_spin(tmp_path, 'spin-x.tsv', [(1, 'disk2', 'up')])
    assert gh.newest_disk1_state(str(tmp_path)) is None


def test_disk1_up_spin_or_window(tmp_path):
    noon, night = datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 23, 0)
    assert gh.disk1_up(str(tmp_path), '22:00-06:00', noon) == (False, 'down')
    assert gh.disk1_up(str(tmp_path), '22:00-06:00', night) == (True, 'window')
    write_spin(tmp_path, 'spin-a.tsv', [(5, 'disk1', 'up')])
    assert gh.disk1_up(str(tmp_path), '22:00-06:00', noon) == (True, 'spin')
    write_spin(tmp_path, 'spin-a.tsv', [(5, 'disk1', 'up'), (6, 'disk1', 'down')])
    assert gh.disk1_up(str(tmp_path), '22:00-06:00', noon) == (False, 'down')


def test_index_missing_stale_empty_and_fresh(tmp_path):
    p = tmp_path / 'idx'
    assert gh.load_index(str(p), NOW) is None
    p.write_text('# comment\n\ndata/Media/TV Shows/Cold Show\n/data/Media/Movies/Cold Movie (2001)/\n')
    os.utime(p, (NOW - 3600, NOW - 3600))
    assert gh.load_index(str(p), NOW) == PREFIXES
    os.utime(p, (NOW - 3.1 * 86400, NOW - 3.1 * 86400))
    assert gh.load_index(str(p), NOW) is None                          # older than 3 days: hold inactive
    p.write_text('# only a comment\n')
    os.utime(p, (NOW, NOW))
    assert gh.load_index(str(p), NOW) is None
    assert gh.load_index('', NOW) is None


def test_on_disk1_matches_whole_components_only():
    f = lambda p: gh.on_disk1(p, PREFIXES)  # noqa: E731
    assert f('/data/Media/TV Shows/Cold Show/Season 1/e01.mkv')
    assert f('/data/Media/Movies/Cold Movie (2001)/Cold Movie (2001).mkv')
    assert not f('data/Media/TV Shows/Cold Show')        # the unit itself: only paths below it count
    assert gh.on_disk1('/mnt/x/Media/TV Shows/Cold Show/e.mkv', PREFIXES, ('/mnt/x', 'data'))   # container to share map
    assert not f('/data/Media/TV Shows/Cold Show 2/Season 1/e01.mkv')  # sibling with the same stem
    assert not f('/data/Media/TV Shows/Warm Show/e01.mkv')
    assert not f('')
    assert not gh.on_disk1('/data/x', frozenset())


# ── fake world: a Deluge, and an *arr that tracks only what its category shows ─

class World:
    """Models the part of Sonarr/Radarr that matters: the download client lists torrents under the arr's
    category label only; a tracked download is (re)built from the Grabbed history row of its downloadId;
    a torrent with no Grabbed row would be an unknown file."""

    def __init__(self):
        self.torrents = {}                 # hash -> info
        self.history = {}                  # (who, hash) -> list of eventTypes
        self.files = {}                    # (who, hash) -> [old file paths]
        self.state = {}                    # (who, hash) -> trackedDownloadState
        self.category = {'Sonarr': {'sonarr', 'sonarr-upgrade'}, 'Radarr': {'radarr', 'radarr-upgrade'}}
        self.who_of = {}
        self.saved = []
        self.commands = []
        self.events = []                   # ordered effects: ('link', dst) ('unlink', dst) ('label', h, l) ('refresh', who) ('scan', folder)
        self.fs = {}                       # path -> disk name; a hardlink lands on its source's disk
        self.scans = []
        self.fail_link = False
        self.fail_unlink = False
        self.fail_scan = False
        self.fail_label = set()
        self.fail_cmd = set()
        self.tracked = {}                  # (who, hash) -> 'upgrade' | 'unknown'
        self.hash_unreadable = set()

    def add(self, h, who, label, files, grabbed=True, state='downloading', finished=True):
        self.torrents[h] = {'label': label, 'is_finished': finished, 'progress': 100 if finished else 10,
                            'state': 'Seeding' if finished else 'Downloading'}
        self.history[(who, h)] = ['grabbed'] if grabbed else []
        self.files[(who, h)] = files
        self.state[(who, h)] = state
        self.who_of[h] = who
        self.fs[self.src_of(h)] = 'disk2'
        for p in files:
            self.fs[p] = 'disk1' if 'Cold' in p else 'disk3'
        self.refresh_tracking(who)

    @staticmethod
    def out_of(h):
        return f'/data/Downloads/{h[:6]}'

    def src_of(self, h):
        return f'{self.out_of(h)}/ep.mkv'

    def refresh_tracking(self, who):
        """The arr's RefreshMonitoredDownloads: StopTrackingMissing, then TrackDownload per client item."""
        for key in [k for k in self.tracked if k[0] == who]:
            if self.torrents.get(key[1], {}).get('label') not in self.category[who]:
                del self.tracked[key]
        for h, t in self.torrents.items():
            if self.who_of.get(h) == who and t['label'] in self.category[who]:
                self.tracked[(who, h)] = 'upgrade' if 'grabbed' in self.history[(who, h)] else 'unknown'


class FakeIO:
    def __init__(self, world, services=('Sonarr', 'Radarr'), lookup_fails=False):
        self.w = world
        self._services = list(services)
        self.lookup_fails = lookup_fails
        self.t = NOW

    def services(self):
        return self._services

    def now(self):
        return self.t

    def torrents(self):
        return {h: dict(t) for h, t in self.w.torrents.items()}

    def queue(self, who):
        if who in self.w.hash_unreadable:
            raise ConnectionError('down')
        return [{'downloadId': h.upper(), 'trackedDownloadState': self.w.state[(who, h)],
                 'episodeId': 1, 'movieId': 1, 'outputPath': self.w.out_of(h)}
                for (w, h) in self.w.tracked if w == who]

    def old_paths(self, who, recs):
        if self.lookup_fails:
            raise ConnectionError('boom')
        h = recs[0]['downloadId'].lower()
        return self.w.files[(who, h)]

    def has_grab(self, who, h):
        return 'grabbed' in self.w.history[(who, h)]

    def set_label(self, h, label):
        if h in self.w.fail_label:
            raise ConnectionError('boom')
        self.w.torrents[h]['label'] = label
        self.w.events.append(('label', h, label))

    def pick_source(self, output_path):
        p = output_path + '/ep.mkv'
        return p if p in self.w.fs else None

    def link(self, src, dst):
        if self.w.fail_link:
            raise OSError('boom')
        assert dst not in self.w.fs
        self.w.fs[dst] = self.w.fs[src]          # same inode: same disk as the source
        self.w.events.append(('link', dst))

    def unlink(self, dst):
        if self.w.fail_unlink:
            raise OSError('boom')
        if dst not in self.w.fs:
            raise FileNotFoundError(dst)
        del self.w.fs[dst]
        self.w.events.append(('unlink', dst))

    def plex_scan(self, folder):
        if self.w.fail_scan:
            raise RuntimeError('boom')
        self.w.scans.append(folder)
        self.w.events.append(('scan', folder))

    def refresh(self, who):
        if who in self.w.fail_cmd:
            raise ConnectionError('boom')
        self.w.commands.append(('refresh', who))
        self.w.events.append(('refresh', who))
        self.w.refresh_tracking(who)

    def save(self, held):
        self.w.saved.append({h: dict(v) for h, v in held.items()})


COLD_EP = ['/data/Media/TV Shows/Cold Show/Season 1/e01.mkv']
WARM_EP = ['/data/Media/TV Shows/Warm Show/Season 1/e01.mkv']
LABELS = {'sonarr', 'radarr', 'sonarr-upgrade', 'radarr-upgrade'}


def run_hold(w, state=None, checked=None, **kw):
    io = FakeIO(w, **kw)
    st = {} if state is None else state
    return gh.hold_pass(io, st, PREFIXES, LABELS, set() if checked is None else checked), st, io


# ── hold / release ────────────────────────────────────────────────────────────

def test_hold_then_release_keeps_the_grab_match():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    assert w.tracked == {('Sonarr', H1): 'upgrade'}
    c, st, io = run_hold(w)
    assert c['held'] == 1
    assert w.torrents[H1]['label'] == gh.HOLD_LABEL
    assert st[H1]['label'] == 'sonarr-upgrade' and st[H1]['who'] == 'Sonarr' and st[H1]['since'] == NOW
    assert w.saved[0][H1]['label'] == 'sonarr-upgrade'                 # state written before the relabel
    w.refresh_tracking('Sonarr')
    assert w.tracked == {}                                             # the arr does not see it: no import, no wake
    # release: same hash, same label back, the arr re-tracks it and still matches it to its grab history
    r = gh.release_pass(io, st)
    assert r['released'] == 1 and r['rematched'] == 1 and r['unmatched'] == 0
    assert w.torrents[H1]['label'] == 'sonarr-upgrade'
    assert w.tracked == {('Sonarr', H1): 'upgrade'}                    # an upgrade of its grab, not an unknown file
    assert w.history[('Sonarr', H1)] == ['grabbed']                    # history untouched
    assert ('refresh', 'Sonarr') in w.commands
    assert st == {} and w.saved[-1] == {}


def test_radarr_hold_and_release_restores_each_original_label():
    w = World()
    w.add(H1, 'Radarr', 'radarr-upgrade', ['/data/Media/Movies/Cold Movie (2001)/Cold Movie (2001).mkv'])
    w.add(H2, 'Sonarr', 'sonarr', COLD_EP)
    c, st, io = run_hold(w)
    assert c['held'] == 2
    gh.release_pass(io, st)
    assert (w.torrents[H1]['label'], w.torrents[H2]['label']) == ('radarr-upgrade', 'sonarr')
    assert ('refresh', 'Radarr') in w.commands and ('refresh', 'Sonarr') in w.commands


def test_no_hold_when_old_file_is_elsewhere_or_not_an_upgrade_or_unfinished():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', WARM_EP)
    w.add(H2, 'Sonarr', 'sonarr', [])                                   # first import, nothing to replace
    w.add(H3, 'Sonarr', 'sonarr-upgrade', COLD_EP, finished=False)      # still downloading
    c, st, io = run_hold(w)
    assert c['old_file_elsewhere'] == 1 and c['not_upgrade'] == 1 and not c.get('held')
    assert st == {} and all(t['label'] != gh.HOLD_LABEL for t in w.torrents.values())


def test_no_hold_without_grab_history():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP, grabbed=False)
    c, st, io = run_hold(w)
    assert c['no_grab_history'] == 1 and st == {} and w.torrents[H1]['label'] == 'sonarr-upgrade'


def test_no_hold_once_import_is_underway():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP, state='importing')
    c, st, io = run_hold(w)
    assert c['past_import_stage'] == 1 and st == {}


def test_import_pending_is_still_held():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP, state='importPending')
    c, st, io = run_hold(w)
    assert c['held'] == 1


def test_each_download_is_judged_once_but_a_failed_lookup_is_retried():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', WARM_EP)
    checked = set()
    run_hold(w, checked=checked)
    assert H1 in checked
    c, _, _ = run_hold(w, checked=checked)
    assert c == {}                                                      # nothing left to look at
    w.add(H2, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    c, st, _ = run_hold(w, checked=checked, lookup_fails=True)
    assert c['lookup_failed'] == 1 and H2 not in checked and st == {}
    c, st, _ = run_hold(w, checked=checked)
    assert c['held'] == 1


def test_unreadable_queue_never_marks_a_download_checked():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    w.hash_unreadable.add('Sonarr')
    checked = set()
    c, st, _ = run_hold(w, checked=checked)
    assert c['queue_unreadable'] == 1 and not checked and st == {}


def test_hold_failure_leaves_nothing_in_state():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    w.fail_label.add(H1)
    c, st, _ = run_hold(w)
    assert c['hold_failed'] == 1 and st == {} and w.saved[-1] == {}
    assert w.torrents[H1]['label'] == 'sonarr-upgrade'


def test_release_gone_torrent_relabelled_elsewhere_and_failure():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    w.add(H2, 'Sonarr', 'sonarr', COLD_EP)
    w.add(H3, 'Radarr', 'radarr', ['/data/Media/Movies/Cold Movie (2001)/x.mkv'])
    _, st, io = run_hold(w)
    del w.torrents[H1]                          # removed from Deluge while held
    w.torrents[H2]['label'] = 'superseded'      # someone else relabelled it
    w.fail_label.add(H3)
    r = gh.release_pass(io, st)
    assert (r['gone'], r['relabelled_elsewhere'], r['failed']) == (1, 1, 1)
    assert list(st) == [H3]                     # the failed one stays held and is retried
    w.fail_label.clear()
    assert gh.release_pass(io, st)['released'] == 1 and st == {}


def test_release_counts_unmatched_when_the_arr_never_tracks_it():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    _, st, io = run_hold(w)
    w.refresh_tracking('Sonarr')                # the arr has dropped it
    w.fail_cmd.add('Sonarr')                  # refresh fails and the arr never re-polls
    r = gh.release_pass(io, st, verify_tries=2, sleep=lambda s: None)
    assert r['released'] == 1 and r['refresh_failed'] == 1 and r['unmatched'] == 1
    assert w.torrents[H1]['label'] == 'sonarr-upgrade'


# ── the stopgap link ──────────────────────────────────────────────────────────

OLD_FOLDER = '/data/Media/TV Shows/Cold Show/Season 1'


def held_with_link():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    c, st, io = run_hold(w)
    return w, c, st, io


def test_link_is_made_next_to_the_old_file_on_the_downloads_disk_and_plex_is_told():
    w, c, st, io = held_with_link()
    link = st[H1]['link']
    assert (c['linked'], c['scanned']) == (1, 1)
    assert link == OLD_FOLDER + '/ep - glacier-tmp.mkv' and gh.is_link_name(link.rsplit('/', 1)[1])
    assert w.scans == [OLD_FOLDER]
    # proof (i): the link is never on the old file's disk: it shares the download's inode and disk
    assert w.fs[link] == w.fs[w.src_of(H1)] == 'disk2'
    assert w.fs[link] != w.fs[COLD_EP[0]] == 'disk1'


def test_download_that_is_itself_on_disk1_is_not_linked():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    p = gh.plan_link('/data/Media/TV Shows/Cold Show/Season 1/x.mkv', COLD_EP, PREFIXES)
    assert p == (None, 'link_skipped_src_on_disk1')
    assert gh.plan_link(w.src_of(H1), COLD_EP * 2, PREFIXES) == (None, 'link_skipped_multi')
    assert gh.plan_link(w.src_of(H1) + '.nfo', COLD_EP, PREFIXES) == (None, 'link_skipped_nosource')


def test_link_or_scan_failure_still_holds_and_never_leaves_a_stale_link_entry():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    w.fail_link = True
    c, st, io = run_hold(w)
    assert c['held'] == 1 and c['link_failed'] == 1 and st[H1]['link'] is None and w.saved[-1][H1]['link'] is None
    w2 = World()
    w2.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    w2.fail_scan = True
    c, st, io = run_hold(w2)
    assert c['linked'] == 1 and c['scan_failed'] == 1 and st[H1]['link']


def test_release_removes_the_link_first_then_relabels_then_refreshes():
    w, c, st, io = held_with_link()
    link = st[H1]['link']
    w.events.clear()
    r = gh.release_pass(io, st)
    assert r['released'] == 1
    kinds = [e[0] for e in w.events]
    assert kinds == ['unlink', 'label', 'refresh']                       # proof (ii)
    assert w.events[0] == ('unlink', link) and link not in w.fs
    assert w.torrents[H1]['label'] == 'sonarr-upgrade'


def test_after_release_the_arr_imports_it_as_an_upgrade_tied_to_its_grab():
    w, c, st, io = held_with_link()
    w.refresh_tracking('Sonarr')
    assert w.tracked == {}
    gh.release_pass(io, st)
    assert w.tracked == {('Sonarr', H1): 'upgrade'}                      # proof (iii)
    assert w.history[('Sonarr', H1)] == ['grabbed']
    assert not [p for p in w.fs if gh.LINK_MARK in p]                    # nothing temporary left for a rescan to find


def test_failed_unlink_keeps_the_hold_so_the_import_never_overtakes_the_link():
    w, c, st, io = held_with_link()
    w.fail_unlink = True
    r = gh.release_pass(io, st)
    assert r['unlink_failed'] == 1 and not r.get('released')
    assert w.torrents[H1]['label'] == gh.HOLD_LABEL and H1 in st
    w.fail_unlink = False
    assert gh.release_pass(io, st)['released'] == 1


def test_restart_between_hold_and_release_loses_nothing():
    w, c, st, io = held_with_link()
    link = st[H1]['link']
    after_restart = {h: dict(v) for h, v in w.saved[-1].items()}         # what the state file held
    assert after_restart[H1]['link'] == link
    rc = gh.reconcile(FakeIO(w), after_restart)                          # startup lists leftovers
    assert rc['links'] == 1 and rc['kept'] == 1 and not rc.get('orphan_links_removed')
    assert link in w.fs and w.torrents[H1]['label'] == gh.HOLD_LABEL
    w.events.clear()
    r = gh.release_pass(FakeIO(w), after_restart)                        # proof (iv)
    assert r['released'] == 1 and r['rematched'] == 1
    assert [e[0] for e in w.events] == ['unlink', 'label', 'refresh']
    assert w.tracked == {('Sonarr', H1): 'upgrade'}


def test_restart_clears_a_link_that_has_no_live_hold():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    link = OLD_FOLDER + '/ep - glacier-tmp.mkv'
    w.fs[link] = 'disk2'
    state = {H1: {'label': 'sonarr-upgrade', 'who': 'Sonarr', 'since': NOW, 'link': link, 'folder': OLD_FOLDER}}
    rc = gh.reconcile(FakeIO(w), state)                                  # torrent never got the hold label
    assert rc['orphan_links_removed'] == 1 and rc['orphan_entries_dropped'] == 1
    assert state == {} and link not in w.fs and w.saved[-1] == {}


def test_crash_after_journal_before_link_leaves_a_record_not_an_orphan():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    link = OLD_FOLDER + '/ep - glacier-tmp.mkv'
    state = {H1: {'label': 'sonarr-upgrade', 'who': 'Sonarr', 'since': NOW, 'link': link, 'folder': OLD_FOLDER}}
    w.torrents[H1]['label'] = gh.HOLD_LABEL                              # held, link never created
    r = gh.release_pass(FakeIO(w), state)
    assert r['released'] == 1 and w.torrents[H1]['label'] == 'sonarr-upgrade'


def test_journal_is_written_before_the_link_exists():
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    seen = []
    io = FakeIO(w)
    real = io.link
    io.link = lambda a, b: (seen.append(w.saved[-1][H1]['link']), real(a, b))
    gh.hold_pass(io, {}, PREFIXES, LABELS, set())
    assert seen == [OLD_FOLDER + '/ep - glacier-tmp.mkv']


def test_gone_torrent_releases_its_link_too():
    w, c, st, io = held_with_link()
    link = st[H1]['link']
    del w.torrents[H1]
    r = gh.release_pass(io, st)
    assert r['gone'] == 1 and link not in w.fs and st == {}


# ── arr-webhook wiring ────────────────────────────────────────────────────────

def wire(monkeypatch, tmp_path, hold=True, index=True):
    idx = tmp_path / 'idx'
    if index:
        idx.write_text('data/Media/TV Shows/Cold Show\n')
        os.utime(idx, (NOW, NOW))
    spin = tmp_path / 'spin'
    spin.mkdir(exist_ok=True)
    for k, v in dict(HOLD_DISK1_UPGRADES=hold, GLACIER_INDEX=str(idx),
                     GLACIER_SPIN_DIR=str(spin), GLACIER_STATE=str(tmp_path / 'state.json'),
                     GLACIER_WINDOW='22:00-06:00', GLACIER_TZ='').items():
        monkeypatch.setattr(aw, k, v)
    aw._glacier_checked.clear()
    return spin


NOON, NIGHT = datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 23, 0)


def test_tick_holds_by_day_releases_at_night_and_persists(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    out = aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON)
    assert out['hold']['held'] == 1 and w.torrents[H1]['label'] == gh.HOLD_LABEL
    assert aw._glacier_held_hashes() == {H1}                           # survives in the state file
    out = aw.glacier_tick(now=NOW + 60, io=FakeIO(w), now_dt=NOON)
    assert 'release' not in out
    out = aw.glacier_tick(now=NOW + 120, io=FakeIO(w), now_dt=NIGHT)
    assert out['release']['released'] == 1 and w.torrents[H1]['label'] == 'sonarr-upgrade'
    assert aw._glacier_held_hashes() == set()


def test_tick_releases_when_spin_log_says_up(monkeypatch, tmp_path):
    spin = wire(monkeypatch, tmp_path)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON)
    write_spin(spin, 'spin-1.tsv', [(1, 'disk1', 'up')])
    out = aw.glacier_tick(now=NOW + 60, io=FakeIO(w), now_dt=NOON)
    assert out['release']['released'] == 1


def test_tick_switch_off_releases_leftovers_and_holds_nothing(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON)
    monkeypatch.setattr(aw, 'HOLD_DISK1_UPGRADES', False)
    out = aw.glacier_tick(now=NOW + 60, io=FakeIO(w), now_dt=NOON)
    assert out['release']['released'] == 1
    w.add(H2, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    assert 'hold' not in aw.glacier_tick(now=NOW + 120, io=FakeIO(w), now_dt=NOON)
    assert w.torrents[H2]['label'] == 'sonarr-upgrade'


def test_tick_without_a_fresh_index_imports_go_through(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, index=False)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    assert aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON) == {}
    assert w.torrents[H1]['label'] == 'sonarr-upgrade'
    wire(monkeypatch, tmp_path)
    os.utime(tmp_path / 'idx', (NOW - 4 * 86400, NOW - 4 * 86400))      # stale
    assert aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON) == {}


def test_startup_reconcile_via_state_file(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON)
    link = aw._glacier_load()['held'][H1]['link']
    assert link in w.fs
    out = aw.glacier_startup(io=FakeIO(w))                               # restart: hold is live, link kept
    assert out['links'] == 1 and out['kept'] == 1 and link in w.fs
    w.torrents[H1]['label'] = 'sonarr-upgrade'                           # hold lost behind our back
    out = aw.glacier_startup(io=FakeIO(w))
    assert out['orphan_links_removed'] == 1 and link not in w.fs and aw._glacier_held_hashes() == set()


def test_tick_switches_off_does_nothing(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, hold=False)
    w = World()
    assert aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NIGHT) == {}
    assert w.commands == []


def test_defaults_are_off():
    assert gh.HOLD_LABEL == 'glacier-hold'
    src = open('docker-compose.yml').read()
    assert 'HOLD_DISK1_UPGRADES: "${HOLD_DISK1_UPGRADES:-0}"' in src
    assert 'DEFER_RECYCLE_CLEANUP' not in src


# ── other passes must leave a held download alone ─────────────────────────────

def test_label_verify_pass_skips_held_downloads(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    w = World()
    w.add(H1, 'Sonarr', 'sonarr-upgrade', COLD_EP)
    aw.glacier_tick(now=NOW, io=FakeIO(w), now_dt=NOON)
    relabels = []

    class Resp:
        def __init__(self, p):
            self.p = p

        def raise_for_status(self):
            pass

        def json(self):
            return self.p

    def fake_get(url, **kw):
        if url.endswith('/queue'):
            return Resp({'records': [{'downloadId': H1, 'episodeId': 7}]})
        return Resp({'hasFile': False})                 # would "correct" the label to plain sonarr

    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda force=False: {H1: {'name': 'x', 'label': gh.HOLD_LABEL}})
    monkeypatch.setattr(aw, 'set_torrent_label', lambda h, label: relabels.append((h, label)))
    monkeypatch.setattr(aw.requests, 'get', fake_get)
    monkeypatch.setattr(aw, 'RADARR_URL', 'http://radarr.invalid')
    aw.verify_and_fix_labels(services=('sonarr',))
    assert relabels == []


def test_dockerfile_copies_the_module():
    assert 'COPY glacier_hold.py .' in open('Dockerfile').read()
