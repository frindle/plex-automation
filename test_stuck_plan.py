"""stuck_plan: the ladder, the 95% rule, the scope exclusions and the connection cap.
Pure logic, no services. Titles are made up."""
import stuck_plan as sp

H = 'a' * 40
MIN = 60
HOUR = 3600
T0 = 1_000_000.0


def cfg(**kw):
    base = dict(stuck_after_min=20, give_up_hours=6, upgrade_max_connections=10,
                upgrade_labels=('sonarr-upgrade', 'radarr-upgrade', 'sonarr-upgrade-recent', 'radarr-upgrade-recent'))
    base.update(kw)
    return sp.Config(**base)


def tor(**kw):
    base = dict(name='Example Movie (2020)', label='radarr', state='Downloading', progress=40.0, is_finished=False,
                download_payload_rate=0, time_since_download=30 * MIN, active_time=2 * HOUR, max_connections=-1,
                tracker_status='Announce OK', tracker_host='tracker.example.com', total_seeds=0)
    base.update(kw)
    return base


ZP = {H: 3}      # three pieces no connected peer has


def kinds(actions, h=H):
    return [a['action'] for a in actions if a['hash'] == h]


# ── what counts as stalled ──────────────────────────────────────────────────
def test_stalled_needs_state_rate_and_quiet_time():
    c = cfg()
    assert sp.is_stalled(tor(), c)
    assert not sp.is_stalled(tor(state='Queued'), c)
    assert not sp.is_stalled(tor(state='Paused'), c)
    assert not sp.is_stalled(tor(download_payload_rate=1024), c)          # at 1 KiB/s it moves
    assert sp.is_stalled(tor(download_payload_rate=1023), c)
    assert not sp.is_stalled(tor(time_since_download=20 * MIN - 1), c)
    assert sp.is_stalled(tor(time_since_download=20 * MIN), c)


def test_never_downloaded_counts_only_after_active_long_enough():
    c = cfg()
    assert not sp.is_stalled(tor(time_since_download=-1, active_time=20 * MIN - 1), c)
    assert sp.is_stalled(tor(time_since_download=-1, active_time=20 * MIN), c)


def test_stuck_after_min_is_configurable():
    assert not sp.is_stalled(tor(time_since_download=40 * MIN), cfg(stuck_after_min=60))
    assert sp.is_stalled(tor(time_since_download=61 * MIN), cfg(stuck_after_min=60))


# ── the ladder ──────────────────────────────────────────────────────────────
def test_step_1_reannounce_and_queue_top_at_once():
    actions, state, rows = sp.plan({H: tor()}, {}, T0, cfg())
    assert kinds(actions) == [sp.REANNOUNCE, sp.QUEUE_TOP]
    assert state[H]['step'] == 1 and state[H]['stalled_since'] == T0
    assert rows[0]['step'] == 1 and rows[0]['stalled'] is True


def test_nothing_more_before_the_wait_is_over():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg(), None, ZP)
    actions, state2, rows = sp.plan({H: tor()}, state, T0 + 20 * MIN - 1, cfg(), None, ZP)
    assert actions == []
    assert state2[H]['step'] == 1
    assert 'step 2 in' in rows[0]['next']


def test_step_2_pause_then_resume_after_the_wait():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    actions, state2, _ = sp.plan({H: tor()}, state, T0 + 20 * MIN, cfg())
    assert kinds(actions) == [sp.PAUSE, sp.RESUME]
    assert state2[H]['step'] == 2 and state2[H]['step_at'] == T0 + 20 * MIN
    # and it is not repeated on the next pass
    actions, state3, _ = sp.plan({H: tor()}, state2, T0 + 30 * MIN, cfg())
    assert actions == [] and state3[H]['step'] == 2


def test_step_3_gives_up_after_the_hours():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    actions, state2, rows = sp.plan({H: tor(progress=94.99)}, state, T0 + 6 * HOUR, cfg(), None, ZP)
    assert kinds(actions) == [sp.GIVE_UP]
    give = actions[0]
    assert give['label'] == 'radarr' and give['name'] == 'Example Movie (2020)'
    assert state2[H]['step'] == 3 and rows[0]['step'] == 3


def test_not_given_up_one_second_early():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    actions, _, rows = sp.plan({H: tor()}, state, T0 + 6 * HOUR - 1, cfg(), None, ZP)
    assert actions == []
    assert rows[0]['next'].startswith('give up in')


def test_give_up_is_asked_again_while_it_is_still_stalled():
    state = {H: {'step': 3, 'step_at': T0 + 6 * HOUR, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    actions, _, _ = sp.plan({H: tor()}, state, T0 + 7 * HOUR, cfg(), None, ZP)
    assert kinds(actions) == [sp.GIVE_UP]      # an earlier try that failed is retried


def test_give_up_hours_is_configurable():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=1), None, ZP)[0][0]['action'] == sp.GIVE_UP
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=3), None, ZP)[0] == []


# ── no progress exception ───────────────────────────────────────────────────
def test_a_97_percent_never_complete_torrent_is_given_up_like_any_other():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    for progress in (95.0, 97.0, 99.9):
        actions, _, _ = sp.plan({H: tor(progress=progress)}, state, T0 + 6 * HOUR, cfg(), None, ZP)
        assert kinds(actions) == [sp.GIVE_UP], progress
        assert actions[0]['progress'] == progress and actions[0]['zero_pieces'] == 3
        assert actions[0]['never_hours'] == 6.0


def test_a_given_up_candidate_that_moves_again_starts_over():
    state = {H: {'step': 2, 'step_at': T0, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    _, state2, _ = sp.plan({H: tor(progress=97, download_payload_rate=50_000)}, state, T0 + 7 * HOUR, cfg())
    assert H not in state2


# ── reset ───────────────────────────────────────────────────────────────────
def test_ladder_resets_as_soon_as_it_moves():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    actions, state2, rows = sp.plan({H: tor(download_payload_rate=200_000, time_since_download=0)}, state,
                                    T0 + 5 * MIN, cfg())
    assert actions == [] and H not in state2
    assert rows[0]['stalled'] is False and rows[0]['step'] == 0
    # stalling again later starts at step 1 again
    actions, state3, _ = sp.plan({H: tor()}, state2, T0 + 2 * HOUR, cfg())
    assert kinds(actions) == [sp.REANNOUNCE, sp.QUEUE_TOP] and state3[H]['step'] == 1


def test_slow_but_downloaded_recently_is_not_stalled_and_resets():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    _, state2, _ = sp.plan({H: tor(download_payload_rate=10, time_since_download=2 * MIN)}, state, T0 + MIN, cfg())
    assert H not in state2


def test_queued_or_paused_keeps_the_ladder_and_gets_no_action():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg(), None, ZP)
    for st in ('Queued', 'Paused', 'Checking'):
        actions, state2, _ = sp.plan({H: tor(state=st)}, state, T0 + 25 * MIN, cfg())
        assert actions == [] and state2[H]['step'] == state[H]['step'], st
        # the time spent queued or paused does not count: every clock moved forward by the same 25 min
        assert state2[H]['stalled_since'] == state[H]['stalled_since'] + 25 * MIN, st
        assert state2[H]['never_since'] == state[H]['never_since'] + 25 * MIN, st


def test_state_of_torrents_that_vanished_or_finished_is_dropped():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    assert sp.plan({}, state, T0 + MIN, cfg())[1] == {}
    done = tor(is_finished=True, progress=100.0, state='Seeding')
    assert sp.plan({H: done}, state, T0 + MIN, cfg())[1] == {}


def test_plan_never_changes_its_inputs():
    torrents = {H: tor()}
    state = {H: {'step': 1, 'step_at': T0, 'stalled_since': T0, 'never_since': T0, 'zero_pieces': 3}}
    before = (dict(torrents[H]), {k: dict(v) for k, v in state.items()})
    sp.plan(torrents, state, T0 + 25 * MIN, cfg())
    assert (torrents[H], state) == (before[0], before[1])


# ── scope ───────────────────────────────────────────────────────────────────
def test_only_normal_sonarr_and_radarr_labels_are_in_scope():
    torrents = {
        'u': tor(label=''),                    # unlabeled
        'n': tor(label=None),
        'l': tor(label='library-seed'),
        'p': tor(label='adult'),
        'r': tor(label='radarr'),
        's': tor(label='Sonarr'),              # label compare ignores case
    }
    actions, state, rows = sp.plan(torrents, {}, T0, cfg())
    assert sorted(state) == ['r', 's']
    assert {a['hash'] for a in actions} == {'r', 's'}
    assert {r['hash'] for r in rows} == {'r', 's'}


def test_finished_or_seeding_torrents_are_never_touched():
    torrents = {
        'a': tor(is_finished=True),
        'b': tor(progress=100.0, state='Seeding'),
        'c': tor(progress=100.0),
        'd': tor(state='Seeding', is_finished=True, progress=100.0, label='sonarr'),
    }
    actions, state, rows = sp.plan(torrents, {}, T0, cfg())
    assert actions == [] and state == {} and rows == []


def test_upgrade_lane_gets_no_ladder_only_the_connection_cap():
    torrents = {k: tor(label=k, download_payload_rate=0) for k in
                ('radarr-upgrade', 'sonarr-upgrade', 'radarr-upgrade-recent', 'sonarr-upgrade-recent')}
    actions, state, _ = sp.plan(torrents, {}, T0, cfg())
    assert state == {}
    assert sorted(a['action'] for a in actions) == [sp.CAP_CONNECTIONS] * 4
    assert all(a['max_connections'] == 10 for a in actions)


# ── the connection cap ──────────────────────────────────────────────────────
def test_cap_only_where_it_is_not_already_set_and_only_while_unfinished():
    torrents = {
        'a': tor(label='radarr-upgrade', max_connections=-1),
        'b': tor(label='radarr-upgrade', max_connections=10),
        'c': tor(label='radarr-upgrade', max_connections=10_000),
        'd': tor(label='radarr-upgrade', is_finished=True, progress=100.0, state='Seeding', max_connections=-1),
    }
    actions, _, rows = sp.plan(torrents, {}, T0, cfg())
    assert sorted(a['hash'] for a in actions) == ['a', 'c']
    assert [r['next'] for r in rows] == ['cap connections at 10'] * 2


def test_cap_value_follows_the_setting():
    actions, _, _ = sp.plan({'a': tor(label='sonarr-upgrade')}, {}, T0, cfg(upgrade_max_connections=4))
    assert actions[0]['max_connections'] == 4


def test_normal_lane_is_never_capped():
    actions, _, _ = sp.plan({H: tor(download_payload_rate=500_000, time_since_download=0)}, {}, T0, cfg())
    assert not any(a['action'] == sp.CAP_CONNECTIONS for a in actions)


def test_garbage_status_values_do_not_raise():
    bad = {H: {'name': None, 'label': 'radarr', 'state': 'Downloading', 'progress': None,
               'download_payload_rate': None, 'time_since_download': 'x', 'active_time': None}}
    sp.plan(bad, {}, T0, cfg())


# ── diagnosis: the action follows why it is stuck ───────────────────────────
DEAD_T = dict(tracker_status='Announce OK', total_seeds=0)
LOCAL_T = dict(tracker_status='Announce OK', total_seeds=12, max_connections=10)
ERR_T = dict(tracker_status='Error: Connection timed out', total_seeds=0)


def run_passes(t, times, c=None, unreg=None, zp=ZP):
    state, out = {}, []
    for dt in times:
        actions, state, rows = sp.plan({H: t}, state, T0 + dt, c or cfg(), unreg, zp)
        out.append((actions, rows))
    return state, out


def test_diagnose_cases():
    assert sp.diagnose(tor(**DEAD_T), None, 3) == sp.NEVER_COMPLETE
    assert sp.diagnose(tor(**LOCAL_T), None, 3) == sp.LOCAL
    assert sp.diagnose(tor(tracker_status='Announce OK', total_seeds=-1), None, 3) == sp.UNKNOWN_SWARM
    assert sp.diagnose(tor(tracker_status='Announce OK', total_seeds=None), None, 3) == sp.UNKNOWN_SWARM
    assert sp.diagnose(tor(**ERR_T)) == sp.TRACKER_ERROR
    assert sp.diagnose(tor(tracker_status='Error: Unregistered torrent'), lambda i: True) == sp.UNREGISTERED
    assert sp.diagnose(tor(tracker_status='Error: Connection timed out'), lambda i: False) == sp.TRACKER_ERROR


def test_every_deluge_tracker_status_form():
    # Announce OK: working
    assert sp.tracker_state(tor(tracker_status='Announce OK')) == 'ok'
    assert sp.diagnose(tor(tracker_status='Announce OK', total_seeds=0), None, 2) == sp.NEVER_COMPLETE
    # Warning: <msg> is the tracker answering, so it is working too
    w = tor(tracker_status='Warning: Tracker is busy, try later', total_seeds=0)
    assert sp.tracker_state(w) == 'ok' and sp.diagnose(w, None, 2) == sp.NEVER_COMPLETE
    # '' and Announce Sent: no conclusion, whatever the seed count says
    for st in ('', 'Announce Sent', None):
        t = tor(tracker_status=st, total_seeds=0)
        assert sp.tracker_state(t) == 'pending'
        assert sp.diagnose(t, None, 2) == sp.PENDING
    # Error: unregistered check first, else tracker error
    assert sp.diagnose(tor(tracker_status='Error: Unregistered'), lambda i: True) == sp.UNREGISTERED
    assert sp.diagnose(tor(tracker_status='Error: Unregistered'), lambda i: False) == sp.TRACKER_ERROR
    assert sp.diagnose(tor(tracker_status='Error: timed out')) == sp.TRACKER_ERROR
    # anything else: pending, flagged unknown in the row
    t = tor(tracker_status='Something New', total_seeds=0)
    assert sp.diagnose(t, None, 2) == sp.PENDING and sp.tracker_state(t) == 'unknown'
    _, _, rows = sp.plan({H: t}, {}, T0, cfg(), None, ZP)
    assert rows[0]['status_unknown'] is True and rows[0]['tracker_status'] == 'Something New'


def test_pending_status_gets_no_tracker_push_and_no_never_complete_clock():
    for st in ('', 'Announce Sent', 'Something New'):
        state, out = run_passes(tor(tracker_status=st), [0, 25 * MIN, 50 * MIN, 7 * HOUR, 30 * HOUR])
        seen = {a['action'] for acts, _ in out for a in acts}
        assert seen == {sp.REANNOUNCE}, (st, seen)
        assert state[H]['never_since'] is None


def test_warning_text_stays_on_the_row():
    t = tor(tracker_status='Warning: ' + 'x' * 200, total_seeds=0)
    _, _, rows = sp.plan({H: t}, {}, T0, cfg(), None, ZP)
    assert rows[0]['tracker_status'].startswith('Warning: ') and len(rows[0]['tracker_status']) == 80
    assert rows[0]['seeds'] == 0 and rows[0]['zero_pieces'] == 3


def test_unregistered_gets_no_ladder_and_no_state():
    state, out = run_passes(tor(tracker_status='Error: Unregistered torrent'), [0, 25 * MIN, 7 * HOUR],
                            unreg=lambda i: True)
    assert state == {} and all(a == [] for a, _ in out)
    assert out[0][1][0]['diagnosis'] == sp.UNREGISTERED


def test_tracker_error_only_reannounces_never_gives_up_or_pauses():
    state, out = run_passes(tor(**ERR_T), [0, 25 * MIN, 7 * HOUR, 30 * HOUR])
    seen = {a['action'] for acts, _ in out for a in acts if a['hash'] == H}
    assert seen <= {sp.REANNOUNCE, sp.NOTIFY_TRACKER} and sp.REANNOUNCE in seen, seen
    assert out[0][1][0]['diagnosis'] == sp.TRACKER_ERROR


def test_tracker_failure_is_reported_once_per_tracker_not_per_torrent():
    ts = {c * 40: tor(name=f'Example {c}', **ERR_T) for c in 'abc'}
    ts['d' * 40] = tor(name='Other tracker', tracker_host='other.example.com', **ERR_T)
    _, st, _ = sp.plan(ts, {}, T0, cfg())
    acts, st, _ = sp.plan(ts, st, T0 + 5 * MIN, cfg())
    assert [a for a in acts if a['action'] == sp.NOTIFY_TRACKER] == [], 'not before the failure persists'
    acts, st, _ = sp.plan(ts, st, T0 + 25 * MIN, cfg())
    notes = [a for a in acts if a['action'] == sp.NOTIFY_TRACKER]
    assert sorted((n['host'], n['count']) for n in notes) == [('other.example.com', 1), ('tracker.example.com', 3)]


def test_tracker_push_needs_the_same_host_in_error_on_two_passes_in_a_row():
    c = cfg(stuck_after_min=1)
    ok = tor(tracker_status='Announce OK', total_seeds=9)
    _, st, _ = sp.plan({H: tor(**ERR_T)}, {}, T0, c)
    # one error pass, a good pass, then an error pass again: the streak starts over
    _, st, _ = sp.plan({H: ok}, st, T0 + 10 * MIN, c)
    acts, st, _ = sp.plan({H: tor(**ERR_T)}, st, T0 + 20 * MIN, c)
    assert sp.NOTIFY_TRACKER not in [a['action'] for a in acts]
    acts, st, _ = sp.plan({H: tor(**ERR_T)}, st, T0 + 30 * MIN, c)
    assert sp.NOTIFY_TRACKER in [a['action'] for a in acts]


def test_local_problem_uncaps_reannounces_notifies_and_never_gives_up():
    state, out = run_passes(tor(**LOCAL_T), [0, 25 * MIN, 26 * MIN, 7 * HOUR, 30 * HOUR])
    first = [a['action'] for a in out[0][0]]
    assert sp.REANNOUNCE in first and sp.UNCAP in first and sp.GIVE_UP not in first
    uncap = [a for a in out[0][0] if a['action'] == sp.UNCAP][0]
    assert uncap['max_connections'] == -1
    seen = [a['action'] for acts, _ in out for a in acts]
    assert seen.count(sp.NOTIFY_LOCAL) == 1, 'notified once when the first fixes did not help'
    assert sp.GIVE_UP not in seen


def test_unknown_swarm_never_gives_up():
    state, out = run_passes(tor(tracker_status='Announce OK', total_seeds=-1), [0, 25 * MIN, 7 * HOUR, 30 * HOUR])
    assert sp.GIVE_UP not in [a['action'] for acts, _ in out for a in acts]


def test_zero_seeds_with_every_missing_piece_available_is_local_never_removed():
    state, out = run_passes(tor(**DEAD_T), [0, 25 * MIN, 7 * HOUR, 30 * HOUR], zp={H: 0})
    seen = [a['action'] for acts, _ in out for a in acts]
    assert sp.GIVE_UP not in seen and out[0][1][0]['diagnosis'] == sp.LOCAL


def test_unreadable_pieces_never_give_up():
    state, out = run_passes(tor(**DEAD_T), [0, 25 * MIN, 7 * HOUR, 30 * HOUR], zp={})
    assert sp.GIVE_UP not in [a['action'] for acts, _ in out for a in acts]
    assert out[0][1][0]['diagnosis'] == sp.UNKNOWN_SWARM


def test_only_a_never_complete_torrent_reaches_give_up():
    state, out = run_passes(tor(**DEAD_T), [0, 25 * MIN, 6 * HOUR + MIN])
    assert sp.GIVE_UP in [a['action'] for a in out[-1][0]]
    assert out[-1][0][-1]['diagnosis'].endswith('could never complete')
    assert out[-1][0][-1]['zero_pieces'] == 3


def test_never_complete_needs_all_three_for_the_whole_window():
    # each of the three conditions failing once in the middle starts the window over
    c = cfg()
    for break_with, zp in ((dict(tracker_status='Announce OK', total_seeds=4), 3),     # seeds appear
                           (dict(tracker_status='Announce OK', total_seeds=0), 0),     # every piece available
                           (dict(tracker_status='Error: timed out', total_seeds=0), 3),  # tracker not confirmed
                           (dict(tracker_status='Announce Sent', total_seeds=0), 3)):
        state = {}
        for dt, t, z in ((0, DEAD_T, 3), (25 * MIN, DEAD_T, 3), (3 * HOUR, break_with, zp),
                         (6 * HOUR + MIN, DEAD_T, 3)):
            acts, state, _ = sp.plan({H: tor(**t)}, state, T0 + dt, c, None, {H: z})
        assert sp.GIVE_UP not in [a['action'] for a in acts], break_with
        acts, state, _ = sp.plan({H: tor(**DEAD_T)}, state, T0 + 12 * HOUR + 2 * MIN, c, None, ZP)
        assert sp.GIVE_UP in [a['action'] for a in acts], break_with


def test_a_piece_becoming_available_resets_the_window():
    c = cfg()
    state = {}
    for dt, z in ((0, 3), (25 * MIN, 3), (3 * HOUR, 0), (3 * HOUR + 10 * MIN, 3), (6 * HOUR + MIN, 3)):
        acts, state, _ = sp.plan({H: tor(**DEAD_T)}, state, T0 + dt, c, None, {H: z})
    assert sp.GIVE_UP not in [a['action'] for a in acts]
    assert state[H]['never_since'] == T0 + 3 * HOUR + 10 * MIN


def test_queued_time_does_not_count_toward_give_up():
    c = cfg()
    _, st, _ = sp.plan({H: tor(**DEAD_T)}, {}, T0, c, None, ZP)
    _, st, _ = sp.plan({H: tor(**DEAD_T)}, st, T0 + 25 * MIN, c, None, ZP)
    for dt in range(1, 10):                               # queued for 9 hours
        _, st, rows = sp.plan({H: tor(state='Queued', **DEAD_T)}, st, T0 + 25 * MIN + dt * HOUR, c, None, ZP)
    assert rows[0]['diagnosis'] == sp.QUEUED
    acts, st, _ = sp.plan({H: tor(**DEAD_T)}, st, T0 + 25 * MIN + 9 * HOUR + 10 * MIN, c, None, ZP)
    assert sp.GIVE_UP not in [a['action'] for a in acts], 'only a few minutes of real stall so far'


def test_piece_check_candidates_are_only_stalled_working_zero_seed_torrents():
    ts = {'a': tor(**DEAD_T), 'b': tor(tracker_status='Announce OK', total_seeds=5),
          'c': tor(tracker_status='Error: x', total_seeds=0), 'd': tor(download_payload_rate=999999, **DEAD_T),
          'e': tor(label='', **DEAD_T), 'f': tor(tracker_status='Announce OK', total_seeds=-1),
          'g': tor(tracker_status='Warning: busy', total_seeds=0)}
    assert sp.piece_check_candidates(ts, cfg()) == ['a', 'g']


def test_every_stalled_row_carries_a_diagnosis_text():
    _, _, rows = sp.plan({H: tor(**LOCAL_T)}, {}, T0, cfg())
    assert rows[0]['diagnosis'] == sp.LOCAL and 'local problem' in rows[0]['diagnosis_text']
