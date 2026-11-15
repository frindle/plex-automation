"""Upgrade-lane cap and seed retirement: the pure planners (deluge_trim.py)
and their wiring in arr-webhook.py. Synthetic torrents only."""
import importlib.util

import deluge_trim as dt

spec = importlib.util.spec_from_file_location("aw_trim", 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

LANE = ('sonarr-upgrade', 'radarr-upgrade', 'sonarr-upgrade-recent', 'radarr-upgrade-recent')
RECENT = ('sonarr-upgrade-recent', 'radarr-upgrade-recent')
DAY = 86400


def fresh(label='radarr-upgrade', queue=0, added=0, state='Queued'):
    return {'label': label, 'state': state, 'progress': 0.0, 'total_done': 0, 'queue': queue,
            'time_added': added, 'seeding_time': 0, 'total_uploaded': 0, 'is_finished': False,
            'save_path': '/data/Downloads/Incomplete', 'name': 'n'}


def started(label='radarr-upgrade', done=1, state='Queued'):
    return dict(fresh(label, state=state), progress=0.5, total_done=done)


# ── upgrade_lane_plan ────────────────────────────────────────────────────────

def test_started_torrents_are_never_dropped_even_over_cap():
    ts = {f's{i}': started() for i in range(5)}
    ts.update({f'f{i}': fresh(queue=10 + i) for i in range(3)})
    keep, drop = dt.upgrade_lane_plan(ts, 3, LANE, RECENT)
    assert set(keep) == {f's{i}' for i in range(5)}
    assert sorted(drop) == ['f0', 'f1', 'f2']


def test_untouched_kept_in_queue_order_until_cap():
    ts = {'s': started(), 'a': fresh(queue=7), 'b': fresh(queue=3), 'c': fresh(queue=5)}
    keep, drop = dt.upgrade_lane_plan(ts, 3, LANE, RECENT)
    assert keep == ['s', 'b', 'c'] and drop == ['a']


def test_downloading_then_recent_label_win_over_queue_position():
    ts = {'old': fresh(queue=0), 'rec': fresh('radarr-upgrade-recent', queue=9),
          'run': fresh(queue=20, state='Downloading')}
    keep, drop = dt.upgrade_lane_plan(ts, 2, LANE, RECENT)
    assert keep == ['run', 'rec'] and drop == ['old']


def test_unknown_progress_or_bytes_counts_as_started():
    ts = {'p': dict(fresh(), progress=None), 'd': dict(fresh(), total_done=None), 'f': fresh()}
    keep, drop = dt.upgrade_lane_plan(ts, 1, LANE, RECENT)
    assert set(keep) == {'p', 'd'} and drop == ['f']


def test_checking_or_error_states_are_never_dropped():
    ts = {'c': fresh(state='Checking'), 'e': fresh(state='Error'), 'f': fresh()}
    keep, drop = dt.upgrade_lane_plan(ts, 0 + 1, LANE, RECENT)
    assert drop == ['f'] and set(keep) == {'c', 'e'}


def test_other_labels_and_finished_torrents_are_outside_the_lane():
    ts = {'n': fresh('radarr'), 'adult': fresh('adult'), 'done': dict(fresh(), progress=100.0), 'f': fresh()}
    keep, drop = dt.upgrade_lane_plan(ts, 1, LANE, RECENT)
    assert keep == ['f'] and drop == []


def test_cap_zero_turns_the_cap_off():
    ts = {f'f{i}': fresh(queue=i) for i in range(50)}
    keep, drop = dt.upgrade_lane_plan(ts, 0, LANE, RECENT)
    assert len(keep) == 50 and drop == []


def test_census_shape_keeps_22_started_plus_8():
    ts = {f's{i}': started() for i in range(22)}
    ts.update({f'f{i:03d}': fresh(queue=100 + i) for i in range(234)})
    keep, drop = dt.upgrade_lane_plan(ts, 30, LANE, RECENT)
    assert len(keep) == 30 and len(drop) == 226
    assert [h for h in keep if h.startswith('f')] == [f'f{i:03d}' for i in range(8)]


# ── seed_retire_plan ─────────────────────────────────────────────────────────

def seed(label='radarr', days=200, seeds=9, state='Seeding', finished=True):
    return {'label': label, 'state': state, 'is_finished': finished, 'seeding_time': days * DAY,
            'total_seeds': seeds, 'name': 'n', 'save_path': '/data/Downloads/Complete', 'progress': 100.0,
            'total_uploaded': 5}


LABELS = ('sonarr', 'radarr', 'lidarr', 'library-seed') + LANE


def test_retire_picks_old_seeds_in_healthy_swarms_longest_first():
    ts = {'a': seed(days=200), 'b': seed(days=400), 'young': seed(days=179), 'lonely': seed(seeds=5),
          'unk': seed(seeds=-1), 'none': dict(seed(), total_seeds=None)}
    assert dt.seed_retire_plan(ts, LABELS, 180, 5) == ['b', 'a']


def test_retire_leaves_unmanaged_labels_and_unfinished_alone():
    ts = {'adult': seed('adult'), 'blank': seed(''), 'sup': seed('superseded'),
          'paused': seed(state='Paused'), 'unf': seed(finished=False), 'ok': seed('library-seed')}
    assert dt.seed_retire_plan(ts, LABELS, 180, 5) == ['ok']


def test_retire_limit_and_off_switch():
    ts = {f'h{i}': seed(days=200 + i) for i in range(10)}
    assert dt.seed_retire_plan(ts, LABELS, 180, 5, limit=3) == ['h9', 'h8', 'h7']
    assert dt.seed_retire_plan(ts, LABELS, 0, 5) == []


# ── idle rule ────────────────────────────────────────────────────────────────

def quiet(label='radarr', days=30, idle=15, uploaded=5, seeds=9):
    """A finished seed `days` old that last uploaded `idle` days ago (-1 = none on record)."""
    return dict(seed(label, days=days, seeds=seeds), total_uploaded=uploaded,
                time_since_upload=-1 if idle == -1 else idle * DAY)


def idle_plan(ts, min_days=180, idle_days=14, idle_min_days=21):
    return dt.seed_retire_picks(ts, LABELS, min_days, 5, idle_days, idle_min_days)


def test_idle_seconds():
    assert dt.idle_seconds(quiet(idle=3)) == 3 * DAY
    assert dt.idle_seconds(quiet(idle=-1, uploaded=0)) == float('inf')
    assert dt.idle_seconds(quiet(idle=-1, uploaded=5)) is None, 'uploads with no time on record: unknown'
    assert dt.idle_seconds(quiet(idle=-1, uploaded=None)) is None
    assert dt.idle_seconds(seed()) is None, 'field missing: unknown'
    assert dt.idle_seconds(dict(seed(), time_since_upload=True)) is None


def test_idle_retires_quiet_seeds_past_the_floor_only():
    ts = {'quiet': quiet(idle=15), 'never': quiet(idle=-1, uploaded=0), 'edge': quiet(idle=14),
          'busy': quiet(idle=13), 'young': quiet(days=20, idle=19), 'unk': quiet(idle=-1, uploaded=5),
          'nofield': seed(days=30), 'lonely': quiet(seeds=5), 'adult': quiet('adult'),
          'sup': quiet('superseded'), 'blank': quiet('')}
    assert idle_plan(ts) == [('never', 'idle'), ('quiet', 'idle'), ('edge', 'idle')]


def test_active_seeds_keep_going_until_the_age_ceiling():
    ts = {'busy_old': quiet(days=200, idle=0), 'busy': quiet(days=100, idle=0), 'quiet': quiet(days=40, idle=60)}
    assert idle_plan(ts) == [('busy_old', 'age'), ('quiet', 'idle')]


def test_aged_out_come_before_idle_and_limit_applies():
    ts = {'old': quiet(days=300, idle=1), 'older': quiet(days=400, idle=1),
          'i30': quiet(idle=30), 'i90': quiet(idle=90), 'never': quiet(idle=-1, uploaded=0)}
    assert [h for h, _ in idle_plan(ts)] == ['older', 'old', 'never', 'i90', 'i30']
    assert dt.seed_retire_plan(ts, LABELS, 180, 5, limit=3, idle_days=14, idle_min_days=21) == ['older', 'old', 'never']


def test_each_rule_has_its_own_off_switch():
    ts = {'old': quiet(days=300, idle=1), 'quiet': quiet(idle=30)}
    assert idle_plan(ts, idle_days=0) == [('old', 'age')]
    assert idle_plan(ts, min_days=0) == [('quiet', 'idle')]
    assert idle_plan(ts, min_days=0, idle_days=0) == []
    assert dt.seed_retire_plan(ts, LABELS, 180, 5) == ['old'], 'no idle args: age rule alone, as before'


def test_parse_labels():
    assert dt.parse_labels(' a, b ,,', ('x',)) == ('a', 'b')
    assert dt.parse_labels('', ('x',)) == ('x',)
    assert dt.parse_labels(None, ('x', 'y')) == ('x', 'y')


# ── wiring ───────────────────────────────────────────────────────────────────

def _wire(monkeypatch, torrents):
    removed, activity = [], []
    monkeypatch.setattr(aw, 'deluge_login', lambda: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda fields=None, force=False: {h: dict(t) for h, t in torrents.items()})
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: removed.append((h, remove_data)) or True)
    monkeypatch.setattr(aw, 'record_activity', lambda c, m: activity.append((c, m)))
    return removed, activity


def test_enforce_drops_overflow_keeping_files(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 2)
    ts = {'s': started(), 'a': fresh(queue=1), 'b': fresh(queue=2), 'c': fresh(queue=3)}
    removed, activity = _wire(monkeypatch, ts)
    res = aw.enforce_upgrade_lane_cap()
    assert removed == [('b', False), ('c', False)]
    assert res['lane'] == 4 and res['kept'] == 2 and res['removed'] == 2
    assert activity and activity[0][0] == 'cleanup'


def test_enforce_dry_run_removes_nothing(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 1)
    removed, activity = _wire(monkeypatch, {'a': fresh(queue=1), 'b': fresh(queue=2)})
    res = aw.enforce_upgrade_lane_cap(dry_run=True)
    assert removed == [] and activity == [] and [d['hash'] for d in res['drop']] == ['b']


def test_enforce_counts_seed_floor_deferrals(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 1)
    _wire(monkeypatch, {'a': fresh(queue=1), 'b': fresh(queue=2)})
    monkeypatch.setattr(aw, 'remove_torrent', lambda h, remove_data=True, info=None: aw.DEFERRED)
    res = aw.enforce_upgrade_lane_cap()
    assert res['removed'] == 0 and res['deferred'] == 1


def test_grab_time_check_only_touches_the_new_grab(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 1)
    removed, _ = _wire(monkeypatch, {'a': fresh(queue=1), 'b': fresh(queue=2), 'new': fresh(queue=3)})
    aw._cap_new_upgrade_grab('Radarr', 'new')
    assert removed == [('new', False)]
    removed.clear()
    aw._cap_new_upgrade_grab('Radarr', 'a')  # a is kept: nothing to do
    assert removed == []


def test_enforce_off_and_deluge_failure_never_raise(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 0)
    assert aw.enforce_upgrade_lane_cap()['removed'] == 0
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 30)
    monkeypatch.setattr(aw, 'deluge_login', lambda: (_ for _ in ()).throw(RuntimeError('down')))
    assert 'error' in aw.enforce_upgrade_lane_cap()


def test_retire_deletes_copy_only_when_library_has_it(monkeypatch):
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 180)
    monkeypatch.setattr(aw, 'SEED_RETIRE_PER_RUN', 0)
    ts = {'imp': seed('radarr', days=300), 'not': seed('sonarr', days=250), 'lib': seed('library-seed', days=400)}
    removed, activity = _wire(monkeypatch, ts)
    monkeypatch.setattr(aw, '_deluge_rpc', lambda m, p: {'files': [{'path': p[0] + '.mkv'}]})
    seen = []

    def remove_data_for(t, has):
        seen.append(t['hash'])
        return t['hash'] == 'imp'
    monkeypatch.setattr(aw.episode_retention, 'remove_data_for', remove_data_for)
    res = aw.retire_old_seeds()
    assert removed == [('lib', False), ('imp', True), ('not', False)]
    assert 'lib' not in seen, 'library-seed files are the library: never even considered for deletion'
    assert res['removed'] == 3 and res['files_deleted'] == 1
    assert activity and activity[0][0] == 'cleanup'


def test_retire_respects_per_run_and_dry_run(monkeypatch):
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 180)
    monkeypatch.setattr(aw, 'SEED_RETIRE_PER_RUN', 2)
    ts = {f'h{i}': seed(days=200 + i) for i in range(5)}
    removed, _ = _wire(monkeypatch, ts)
    res = aw.retire_old_seeds(dry_run=True)
    assert removed == [] and res['eligible'] == 5 and [c['hash'] for c in res['candidates']] == ['h4', 'h3']


def test_retire_off_by_zero_days(monkeypatch):
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 0)
    monkeypatch.setattr(aw, 'SEED_IDLE_DAYS', 0)
    removed, _ = _wire(monkeypatch, {'h': seed(days=999), 'q': quiet(idle=99)})
    assert aw.retire_old_seeds()['eligible'] == 0 and removed == []


def test_retire_idle_rule_wiring(monkeypatch):
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 180)
    monkeypatch.setattr(aw, 'SEED_IDLE_DAYS', 14)
    monkeypatch.setattr(aw, 'SEED_IDLE_MIN_DAYS', 21)
    monkeypatch.setattr(aw, 'SEED_RETIRE_PER_RUN', 0)
    ts = {'old': seed('radarr', days=300), 'q': quiet('library-seed', idle=40),
          'never': quiet('sonarr', idle=-1, uploaded=0), 'busy': quiet('sonarr', idle=2)}
    removed, activity = _wire(monkeypatch, ts)
    monkeypatch.setattr(aw, '_deluge_rpc', lambda m, p: {'files': []})
    monkeypatch.setattr(aw.episode_retention, 'remove_data_for', lambda t, has: False)
    res = aw.retire_old_seeds(dry_run=True)
    assert removed == [] and res['by_rule'] == {'age': 1, 'idle': 2}
    assert [(c['hash'], c['rule'], c['idle_days']) for c in res['candidates']] == [
        ('old', 'age', None), ('never', 'idle', 'never uploaded'), ('q', 'idle', 40.0)]
    res = aw.retire_old_seeds()
    assert removed == [('old', False), ('never', False), ('q', False)]
    assert res['removed_by_rule'] == {'age': 1, 'idle': 2}
    assert '1 seeded >= 180d, 2 idle' in activity[0][1]


def test_retire_runs_on_idle_rule_alone(monkeypatch):
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 0)
    monkeypatch.setattr(aw, 'SEED_IDLE_DAYS', 14)
    monkeypatch.setattr(aw, 'SEED_IDLE_MIN_DAYS', 21)
    removed, _ = _wire(monkeypatch, {'old': seed(days=999), 'q': quiet('library-seed', idle=30)})
    assert aw.retire_old_seeds()['removed'] == 1 and removed == [('q', False)]


def test_idle_floor_never_below_seed_floor():
    assert aw.SEED_IDLE_MIN_DAYS >= aw.SEED_DAYS
    assert 'time_since_upload' in aw.SEED_RETIRE_FIELDS


def test_endpoints_preview_by_default(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 1)
    monkeypatch.setattr(aw, 'SEED_RETIRE_DAYS', 180)
    removed, _ = _wire(monkeypatch, {'a': fresh(queue=1), 'b': fresh(queue=2), 'old': seed(days=300)})
    monkeypatch.setattr(aw, '_deluge_rpc', lambda m, p: {'files': []})
    c = aw.app.test_client()
    body = c.get('/upgrade-lane-cap').get_json()
    assert body['dry_run'] is True and len(body['drop']) == 1
    body = c.get('/retire-seeds').get_json()
    assert body['dry_run'] is True and body['eligible'] == 1
    assert removed == []
    assert c.post('/upgrade-lane-cap?apply=1').get_json()['removed'] == 1


def test_monthly_scheduler_skips_when_lane_full(monkeypatch):
    monkeypatch.setattr(aw, 'UPGRADE_LANE_MAX', 30)
    monkeypatch.setattr(aw, 'upgrade_lane_count', lambda: 30)
    monkeypatch.setattr(aw, '_load_upgrade_state', lambda: {})
    monkeypatch.setattr(aw, '_save_upgrade_state', lambda st: None)
    ran = []
    monkeypatch.setattr(aw, 'monthly_upgrade_cycle', lambda svc: ran.append(svc))

    class Stop(Exception):
        pass

    def stop(_):
        raise Stop
    monkeypatch.setattr(aw.time, 'sleep', stop)
    try:
        aw.monthly_search_scheduler()
    except Stop:
        pass
    assert ran == []
    monkeypatch.setattr(aw, 'upgrade_lane_count', lambda: 29)
    try:
        aw.monthly_search_scheduler()
    except Stop:
        pass
    assert ran == ['radarr']
