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
                download_payload_rate=0, time_since_download=30 * MIN, active_time=2 * HOUR, max_connections=-1)
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
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'notified': False}}
    actions, state2, rows = sp.plan({H: tor(progress=94.99)}, state, T0 + 6 * HOUR, cfg())
    assert kinds(actions) == [sp.GIVE_UP]
    give = actions[0]
    assert give['label'] == 'radarr' and give['name'] == 'Example Movie (2020)'
    assert state2[H]['step'] == 3 and rows[0]['step'] == 3


def test_not_given_up_one_second_early():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'notified': False}}
    actions, _, rows = sp.plan({H: tor()}, state, T0 + 6 * HOUR - 1, cfg())
    assert actions == []
    assert rows[0]['next'].startswith('give up in')


def test_give_up_is_asked_again_while_it_is_still_stalled():
    state = {H: {'step': 3, 'step_at': T0 + 6 * HOUR, 'stalled_since': T0, 'notified': False}}
    actions, _, _ = sp.plan({H: tor()}, state, T0 + 7 * HOUR, cfg())
    assert kinds(actions) == [sp.GIVE_UP]      # an earlier try that failed is retried


def test_give_up_hours_is_configurable():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'notified': False}}
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=1))[0][0]['action'] == sp.GIVE_UP
    assert sp.plan({H: tor()}, state, T0 + 2 * HOUR, cfg(give_up_hours=3))[0] == []


# ── the 95% rule ────────────────────────────────────────────────────────────
def test_at_95_percent_or_more_it_is_never_given_up_only_notified_once():
    state = {H: {'step': 2, 'step_at': T0 + 20 * MIN, 'stalled_since': T0, 'notified': False}}
    for progress in (95.0, 99.9):
        actions, state2, _ = sp.plan({H: tor(progress=progress)}, state, T0 + 6 * HOUR, cfg())
        assert kinds(actions) == [sp.NOTIFY], progress
        assert actions[0]['progress'] == progress
        assert state2[H]['notified'] is True
        again, _, rows = sp.plan({H: tor(progress=progress)}, state2, T0 + 12 * HOUR, cfg())
        assert again == [], 'one notification, then quiet, and never a give-up'
        assert 'never given up' in rows[0]['next']


def test_a_notified_torrent_that_moves_again_starts_over_and_can_notify_again():
    state = {H: {'step': 2, 'step_at': T0, 'stalled_since': T0, 'notified': True}}
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
        assert actions == [] and state2[H] == state[H], st


def test_state_of_torrents_that_vanished_or_finished_is_dropped():
    _, state, _ = sp.plan({H: tor()}, {}, T0, cfg())
    assert sp.plan({}, state, T0 + MIN, cfg())[1] == {}
    done = tor(is_finished=True, progress=100.0, state='Seeding')
    assert sp.plan({H: done}, state, T0 + MIN, cfg())[1] == {}


def test_plan_never_changes_its_inputs():
    torrents = {H: tor()}
    state = {H: {'step': 1, 'step_at': T0, 'stalled_since': T0, 'notified': False}}
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
