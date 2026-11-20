"""Stuck-download planner: pure logic, no I/O, no clock.

plan() takes Deluge status dicts and the per-hash state left by earlier passes
and returns the actions for this pass, the state for the next pass and a
preview row per torrent. The caller (arr-webhook.py) does the I/O and decides
whether the actions are carried out (STUCK_AUTOMATION=on) or only recorded.

Scope: unfinished torrents with a normal sonarr/radarr label. Unlabeled
torrents, the library-seed label, other labels, anything finished or seeding
are never listed and never get an action.

A torrent is stalled when its state is Downloading, it is moving under
STALL_RATE_BPS, and it has not downloaded for stuck_after_min minutes
(time_since_download; -1 means it never has, and then it counts only once it
has been active that long). The ladder, per stalled torrent:
  step 1  reannounce + queue to top, as soon as the stall is seen
  step 2  pause + resume, stuck_after_min minutes after step 1 if still stalled
  step 3  give_up_hours after the stall was first seen, still stalled:
          under GIVE_UP_PROGRESS percent -> give_up (the caller removes it
          through the arr queue API with blocklist and searches again);
          at or above it -> notify only, once, never give up
A torrent that moves again drops out of the state, so its ladder starts over.

Before any ladder step, a stalled torrent is diagnosed (diagnose()), and the action follows the diagnosis:
  unregistered   the unregistered sweep owns it; no ladder, no state
  tracker_error  reannounce again each wait; one report per failing tracker; never blocklisted or removed
  local          tracker OK, swarm has seeds: uncap, reannounce, restart, notify once; never blocklisted
  unknown_swarm  tracker OK, seeds not scraped (-1): reannounce only
  dead           tracker OK and 0 seeds confirmed for the whole give-up window: the only case that reaches give_up
Queued, paused or checking time does not count: the clocks are held still while it lasts.

Upgrade-lane torrents are not part of the ladder; they only get the
per-torrent connection cap, so the normal lane can find peer slots.
"""

STALL_RATE_BPS = 1024          # 1 KiB/s
GIVE_UP_PROGRESS = 95.0        # percent; at or above this a torrent is never given up on

REANNOUNCE = 'reannounce'
QUEUE_TOP = 'queue_top'
PAUSE = 'pause'
RESUME = 'resume'
GIVE_UP = 'give_up'
NOTIFY = 'notify'
CAP_CONNECTIONS = 'cap_connections'
UNCAP = 'uncap_connections'
NOTIFY_LOCAL = 'notify_local'
NOTIFY_TRACKER = 'notify_tracker'

# Diagnosis of a stalled torrent. Only DEAD may reach give-up; the action follows the diagnosis.
UNREGISTERED = 'unregistered'      # tracker says it does not know the torrent: the unregistered sweep owns it
TRACKER_ERROR = 'tracker_error'    # tracker failing or not announcing: reannounce, never blocklist or remove
LOCAL = 'local'                    # announce OK, swarm has seeds, still no data: a problem on our side
UNKNOWN_SWARM = 'unknown_swarm'    # announce OK, seeds not scraped yet (-1): wait, reannounce only
DEAD = 'dead'                      # announce OK and the swarm has had 0 seeds the whole window
QUEUED = 'queued_or_paused'        # not stalled; the time does not count
MOVING = 'moving'

DIAGNOSIS_TEXT = {
    UNREGISTERED: 'tracker says the torrent is unregistered (handled by the unregistered sweep)',
    TRACKER_ERROR: 'tracker error or not announcing',
    LOCAL: 'tracker OK and the swarm has seeds, but no data arrives: a local problem',
    UNKNOWN_SWARM: 'tracker OK, swarm seed count not scraped yet',
    DEAD: 'tracker OK and the swarm has had no seeds for the whole window: dead torrent',
    QUEUED: 'queued or paused: not counted as stalled',
    MOVING: 'downloading',
}


class Config:
    """Settings for one pass. Plain values; the caller reads them from the environment."""

    def __init__(self, stuck_after_min=20, give_up_hours=6, upgrade_max_connections=10,
                 normal_labels=('sonarr', 'radarr'), upgrade_labels=()):
        self.stuck_after_min = stuck_after_min
        self.give_up_hours = give_up_hours
        self.upgrade_max_connections = upgrade_max_connections
        self.normal_labels = frozenset(l.lower() for l in normal_labels)
        self.upgrade_labels = frozenset(l.lower() for l in upgrade_labels if l)

    @property
    def after_s(self):
        return self.stuck_after_min * 60

    @property
    def give_up_s(self):
        return self.give_up_hours * 3600


def _num(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def is_unfinished(info):
    return not info.get('is_finished') and _num(info.get('progress'), 100.0) < 100.0


def is_stalled(info, cfg):
    """State Downloading, under 1 KiB/s, and quiet for at least stuck_after_min."""
    if info.get('state') != 'Downloading':
        return False
    if _num(info.get('download_payload_rate'), 0) >= STALL_RATE_BPS:
        return False
    since = _num(info.get('time_since_download'), -1)
    if since >= 0:
        return since >= cfg.after_s
    return _num(info.get('active_time'), 0) >= cfg.after_s


def diagnose(info, is_unregistered=None):
    """Why is this stalled torrent stuck? One of the diagnosis constants above. Pure."""
    if is_unregistered is not None and is_unregistered(info):
        return UNREGISTERED
    status = (info.get('tracker_status') or '').lower()
    if 'announce ok' not in status:
        # an error, a timeout, DNS, 5xx, an empty or unknown status: the tracker is not confirmed to be talking to us
        return TRACKER_ERROR
    seeds = info.get('total_seeds')
    if not isinstance(seeds, (int, float)) or isinstance(seeds, bool) or seeds < 0:
        return UNKNOWN_SWARM
    return LOCAL if seeds > 0 else DEAD


def plan(torrents, state, now, cfg, is_unregistered=None):
    """Returns (actions, new_state, rows).

    actions: list of dicts, each with 'action' and 'hash', in the order to carry them out.
    new_state: {hash: {'step', 'step_at', 'stalled_since', 'notified', 'last_seen', 'dead_since', ...}}; the input
    is never changed.
    rows: one dict per torrent in scope (and per upgrade torrent that needs a cap), for the preview, each with its
    'diagnosis'.
    is_unregistered: optional callable(info) -> bool (the caller passes torrent_is_unregistered).
    """
    actions, rows, new_state = [], [], {}
    failing_hosts = {}
    state = state or {}
    for h in sorted(torrents):
        info = torrents[h] or {}
        label = (info.get('label') or '').lower()
        name = info.get('name') or h
        progress = _num(info.get('progress'), 0.0)

        if label in cfg.upgrade_labels:
            if is_unfinished(info) and _num(info.get('max_connections'), -1) != cfg.upgrade_max_connections:
                actions.append({'action': CAP_CONNECTIONS, 'hash': h, 'name': name,
                                'max_connections': cfg.upgrade_max_connections})
                rows.append({'hash': h, 'name': name, 'label': label, 'progress': progress, 'stalled': False,
                             'step': 0, 'next': f'cap connections at {cfg.upgrade_max_connections}'})
            continue
        if label not in cfg.normal_labels or not is_unfinished(info):
            continue

        prev = state.get(h)
        stalled = is_stalled(info, cfg)
        row = {'hash': h, 'name': name, 'label': label, 'progress': progress, 'stalled': stalled,
               'step': prev['step'] if prev else 0, 'next': 'none', 'diagnosis': MOVING,
               'diagnosis_text': DIAGNOSIS_TEXT[MOVING]}
        rows.append(row)

        if not stalled:
            # Downloading and not stalled: it moves, or it moved recently. Its ladder is reset (the entry is
            # dropped). Queued, paused or checking is not a stall: the entry is kept and its clocks are held
            # still, so that time does not count toward anything.
            if prev and info.get('state') != 'Downloading':
                e = dict(prev)
                gap = max(0.0, now - e.get('last_seen', now))
                for k in ('stalled_since', 'step_at', 'dead_since'):
                    if e.get(k) is not None:
                        e[k] = e[k] + gap
                e['last_seen'] = now
                new_state[h] = e
                row['diagnosis'] = QUEUED
                row['diagnosis_text'] = DIAGNOSIS_TEXT[QUEUED]
            else:
                row['step'] = 0
            continue

        diag = diagnose(info, is_unregistered)
        row['diagnosis'] = diag
        row['diagnosis_text'] = DIAGNOSIS_TEXT[diag]
        dtext = DIAGNOSIS_TEXT[diag]
        if diag == UNREGISTERED:
            row['step'] = 0
            row['next'] = 'none here: the unregistered sweep decides'
            continue          # no ladder, no state: nothing but the unregistered path acts on it

        seeds_zero = diag == DEAD
        if prev is None:
            entry = {'step': 1, 'step_at': now, 'stalled_since': now, 'notified': False, 'last_seen': now,
                     'dead_since': now if seeds_zero else None}
            actions.append({'action': REANNOUNCE, 'hash': h, 'name': name, 'step': 1, 'diagnosis': dtext})
            if diag != TRACKER_ERROR:
                actions.append({'action': QUEUE_TOP, 'hash': h, 'name': name, 'step': 1, 'diagnosis': dtext})
            if diag in (LOCAL, UNKNOWN_SWARM, DEAD) and _num(info.get('max_connections'), -1) != -1:
                actions.append({'action': UNCAP, 'hash': h, 'name': name, 'max_connections': -1,
                                'diagnosis': dtext})
            row['step'] = 1
            row['next'] = f'step 1 now: reannounce ({dtext})'
            new_state[h] = entry
            if diag == TRACKER_ERROR:
                failing_hosts.setdefault(info.get('tracker_host') or '?', []).append(entry)
            continue

        entry = dict(prev)
        new_state[h] = entry
        entry['last_seen'] = now
        # dead_since is the start of an unbroken run of confirmed 0-seed observations
        if seeds_zero:
            if entry.get('dead_since') is None:
                entry['dead_since'] = now
        else:
            entry['dead_since'] = None
        stalled_for = now - entry['stalled_since']

        if diag == TRACKER_ERROR:
            failing_hosts.setdefault(info.get('tracker_host') or '?', []).append(entry)
            if now - entry['step_at'] >= cfg.after_s:
                actions.append({'action': REANNOUNCE, 'hash': h, 'name': name, 'step': 1, 'diagnosis': dtext})
                entry['step_at'] = now
                row['next'] = 'reannounced again; tracker failure is reported once per tracker, never blocklisted'
            else:
                row['next'] = 'wait, then reannounce again; never blocklisted or removed'
            continue

        if diag in (LOCAL, UNKNOWN_SWARM):
            if now - entry['step_at'] >= cfg.after_s and entry['step'] < 2:
                actions.append({'action': PAUSE, 'hash': h, 'name': name, 'step': 2, 'diagnosis': dtext})
                actions.append({'action': RESUME, 'hash': h, 'name': name, 'step': 2, 'diagnosis': dtext})
                entry['step'] = 2
                entry['step_at'] = now
                row['step'] = 2
            if diag == LOCAL and entry['step'] >= 2 and not entry.get('local_notified'):
                actions.append({'action': NOTIFY_LOCAL, 'hash': h, 'name': name, 'progress': progress,
                                'stalled_hours': round(stalled_for / 3600, 1), 'diagnosis': dtext})
                entry['local_notified'] = True
                row['next'] = 'notify: reannounce, uncap and restart did not help; never blocklisted'
            else:
                row['next'] = 'local problem: uncap, reannounce, restart; never blocklisted'
            continue

        # DEAD: announce OK and no seeds, confirmed since entry['dead_since']
        dead_for = now - entry['dead_since']
        if dead_for >= cfg.give_up_s and stalled_for >= cfg.give_up_s:
            if progress >= GIVE_UP_PROGRESS:
                if not entry.get('notified'):
                    actions.append({'action': NOTIFY, 'hash': h, 'name': name, 'progress': progress,
                                    'stalled_hours': round(stalled_for / 3600, 1), 'diagnosis': dtext})
                    entry['notified'] = True
                    row['next'] = 'notify now (95% or more: never given up)'
                else:
                    row['next'] = 'none: already notified (95% or more: never given up)'
            else:
                actions.append({'action': GIVE_UP, 'hash': h, 'name': name, 'label': label,
                                'progress': progress, 'step': 3, 'diagnosis': dtext})
                entry['step'] = 3
                row['step'] = 3
                row['next'] = 'step 3 now: blocklist in the arr, remove through remove_torrent(), search again'
            continue
        if entry['step'] < 2 and now - entry['step_at'] >= cfg.after_s:
            actions.append({'action': PAUSE, 'hash': h, 'name': name, 'step': 2, 'diagnosis': dtext})
            actions.append({'action': RESUME, 'hash': h, 'name': name, 'step': 2, 'diagnosis': dtext})
            entry['step'] = 2
            entry['step_at'] = now
            row['step'] = 2
            row['next'] = f'step 2 now: pause and resume; give up in {_hours_left(cfg, dead_for)}'
        elif entry['step'] < 2:
            wait = max(0, cfg.after_s - (now - entry['step_at']))
            row['next'] = f'step 2 in {int(wait // 60) + (1 if wait % 60 else 0)} min: pause and resume'
        else:
            row['next'] = f'give up in {_hours_left(cfg, dead_for)}' if progress < GIVE_UP_PROGRESS \
                else f'notify in {_hours_left(cfg, dead_for)} (95% or more: never given up)'

    # One report per failing tracker once its failure has outlasted the wait, never one per torrent.
    for host, entries in sorted(failing_hosts.items()):
        persisting = [e for e in entries if now - e['stalled_since'] >= cfg.after_s]
        if persisting:
            actions.append({'action': NOTIFY_TRACKER, 'hash': host, 'host': host, 'count': len(persisting),
                            'name': host, 'diagnosis': DIAGNOSIS_TEXT[TRACKER_ERROR]})
    return actions, new_state, rows


def _hours_left(cfg, stalled_for):
    return f'{max(0.0, (cfg.give_up_s - stalled_for) / 3600):.1f} h'
