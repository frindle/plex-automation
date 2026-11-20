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
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    actions, state2, rows = sp.plan({H: tor()}, state, T0 + 20 * MIN - 1, cfg())
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


def test_step_3_gives_up_after_the_hours_when_under_95_percent():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
    actions, state2, rows = sp.plan({H: tor(progress=94.99)}, state, T0 + 6 * HOUR, cfg())
    assert kinds(actions) == [sp.GIVE_UP]
    give = actions[0]
    assert give['label'] == 'radarr' and give['name'] == 'Example Movie (2020)'
    assert state2[H]['step'] == 3 and rows[0]['step'] == 3


def test_not_given_up_one_second_early():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
    actions, _, rows = sp.plan({H: tor()}, state, T0 + 6 * HOUR - 1, cfg())
    assert actions == []
    assert rows[0]['next'].startswith('give up in')


def test_give_up_is_asked_again_while_it_is_still_stalled():
    state = {H: {'step': 3, 'step_at': T0 + 6 * HOUR, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
    actions, _, _ = sp.plan({H: tor()}, state, T0 + 7 * HOUR, cfg())
    assert kinds(actions) == [sp.GIVE_UP]      # an earlier try that failed is retried


def test_give_up_hours_is_configurable():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=1))[0][0]['action'] == sp.GIVE_UP
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=3))[0] == []


# ── the 95% rule ────────────────────────────────────────────────────────────
def test_at_95_percent_or_more_it_is_never_given_up_only_notified_once():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
    for progress in (95.0, 99.9):
        actions, state2, _ = sp.plan({H: tor(progress=progress)}, state, T0 + 6 * HOUR, cfg())
        assert kinds(actions) == [sp.NOTIFY], progress
        assert actions[0]['progress'] == progress
        assert state2[H]['notified'] is True
        again, _, rows = sp.plan({H: tor(progress=progress)}, state2, T0 + 12 * HOUR, cfg())
        assert again == [], 'one notification, then quiet, and never a give-up'
        assert 'never given up' in rows[0]['next']


def test_a_notified_torrent_that_moves_again_starts_over_and_can_notify_again():
    state = {H: {'step': 2, 'step_at': T0, 'stalled_since': T0, 'dead_since': T0, 'notified': True}}
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
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    for st in ('Queued', 'Paused', 'Checking'):
        actions, state2, _ = sp.plan({H: tor(state=st)}, state, T0 + 25 * MIN, cfg())
        assert actions == [] and state2[H]['step'] == state[H]['step'], st
        # the time spent queued or paused does not count: every clock moved forward by the same 25 min
        assert state2[H]['stalled_since'] == state[H]['stalled_since'] + 25 * MIN, st
        assert state2[H]['dead_since'] == state[H]['dead_since'] + 25 * MIN, st


def test_state_of_torrents_that_vanished_or_finished_is_dropped():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    assert sp.plan({}, state, T0 + MIN, cfg())[1] == {}
    done = tor(is_finished=True, progress=100.0, state='Seeding')
    assert sp.plan({H: done}, state, T0 + MIN, cfg())[1] == {}


def test_plan_never_changes_its_inputs():
    torrents = {H: tor()}
    state = {H: {'step': 1, 'step_at': T0, 'stalled_since': T0, 'dead_since': T0, 'notified': False}}
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


def run_passes(t, times, c=None, unreg=None):
    state, out = {}, []
    for dt in times:
        actions, state, rows = sp.plan({H: t}, state, T0 + dt, c or cfg(), unreg)
        out.append((actions, rows))
    return state, out


def test_diagnose_cases():
    assert sp.diagnose(tor(**DEAD_T)) == sp.DEAD
    assert sp.diagnose(tor(**LOCAL_T)) == sp.LOCAL
    assert sp.diagnose(tor(tracker_status='Announce OK', total_seeds=-1)) == sp.UNKNOWN_SWARM
    assert sp.diagnose(tor(tracker_status='Announce OK', total_seeds=None)) == sp.UNKNOWN_SWARM
    assert sp.diagnose(tor(**ERR_T)) == sp.TRACKER_ERROR
    assert sp.diagnose(tor(tracker_status='', total_seeds=9)) == sp.TRACKER_ERROR
    assert sp.diagnose(tor(tracker_status='Error: Unregistered torrent'), lambda i: True) == sp.UNREGISTERED


def test_unregistered_gets_no_ladder_and_no_state():
    state, out = run_passes(tor(tracker_status='Error: Unregistered torrent'), [0, 25 * MIN, 7 * HOUR],
                            unreg=lambda i: True)
    assert state == {} and all(a == [] for a, _ in out)
    assert out[0][1][0]['diagnosis'] == sp.UNREGISTERED


def test_tracker_error_only_reannounces_never_gives_up_or_pauses():
    state, out = run_passes(tor(**ERR_T), [0, 25 * MIN, 7 * HOUR, 30 * HOUR])
    seen = {a['action'] for acts, _ in out for a in acts if a['hash'] == H}
    assert seen == {sp.REANNOUNCE}, seen
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


def test_local_problem_uncaps_reannounces_notifies_and_never_gives_up():
    state, out = run_passes(tor(**LOCAL_T), [0, 25 * MIN, 26 * MIN, 7 * HOUR, 30 * HOUR])
    first = [a['action'] for a in out[0][0]]
    assert sp.REANNOUNCE in first and sp.UNCAP in first and sp.GIVE_UP not in first
    uncap = [a for a in out[0][0] if a['action'] == sp.UNCAP][0]
    assert uncap['max_connections'] == -1
    seen = [a['action'] for acts, _ in out for a in acts]
    assert seen.count(sp.NOTIFY_LOCAL) == 1, 'notified once when the first fixes did not help'
    assert sp.GIVE_UP not in seen and sp.NOTIFY not in seen


def test_unknown_swarm_never_gives_up():
    state, out = run_passes(tor(tracker_status='Announce OK', total_seeds=-1), [0, 25 * MIN, 7 * HOUR, 30 * HOUR])
    assert sp.GIVE_UP not in [a['action'] for acts, _ in out for a in acts]


def test_only_a_dead_torrent_reaches_give_up():
    state, out = run_passes(tor(**DEAD_T), [0, 25 * MIN, 6 * HOUR + MIN])
    assert sp.GIVE_UP in [a['action'] for a in out[-1][0]]
    assert out[-1][0][-1]['diagnosis'].endswith('dead torrent')


def test_dead_must_be_confirmed_across_the_whole_window():
    # seeds appear mid-window (then vanish again): the confirmation starts over, so no give-up yet
    c = cfg()
    state = {}
    for dt, seeds in ((0, 0), (25 * MIN, 0), (3 * HOUR, 4), (6 * HOUR + MIN, 0)):
        acts, state, _ = sp.plan({H: tor(tracker_status='Announce OK', total_seeds=seeds)}, state, T0 + dt, c)
    assert sp.GIVE_UP not in [a['action'] for a in acts]
    acts, state, _ = sp.plan({H: tor(**DEAD_T)}, state, T0 + 12 * HOUR + 2 * MIN, c)
    assert sp.GIVE_UP in [a['action'] for a in acts]


def test_dead_at_95_percent_notifies_with_the_diagnosis():
    state, out = run_passes(tor(progress=96.0, **DEAD_T), [0, 25 * MIN, 6 * HOUR + MIN])
    notes = [a for a in out[-1][0] if a['action'] == sp.NOTIFY]
    assert notes and 'dead torrent' in notes[0]['diagnosis']


def test_queued_time_does_not_count_toward_give_up():
    c = cfg()
    _, st, _ = sp.plan({H: tor(**DEAD_T)}, {}, T0, c)
    _, st, _ = sp.plan({H: tor(**DEAD_T)}, st, T0 + 25 * MIN, c)
    for dt in range(1, 10):                               # queued for 9 hours
        _, st, rows = sp.plan({H: tor(state='Queued', **DEAD_T)}, st, T0 + 25 * MIN + dt * HOUR, c)
    assert rows[0]['diagnosis'] == sp.QUEUED
    acts, st, _ = sp.plan({H: tor(**DEAD_T)}, st, T0 + 25 * MIN + 9 * HOUR + 10 * MIN, c)
    assert sp.GIVE_UP not in [a['action'] for a in acts], 'only a few minutes of real stall so far'


def test_every_stalled_row_carries_a_diagnosis_text():
    _, _, rows = sp.plan({H: tor(**LOCAL_T)}, {}, T0, cfg())
    assert rows[0]['diagnosis'] == sp.LOCAL and 'local problem' in rows[0]['diagnosis_text']
