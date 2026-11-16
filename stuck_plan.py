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


def plan(torrents, state, now, cfg):
    """Returns (actions, new_state, rows).

    actions: list of dicts, each with 'action' and 'hash', in the order to carry them out.
    new_state: {hash: {'step', 'step_at', 'stalled_since', 'notified'}}; the input is never changed.
    rows: one dict per torrent in scope (and per upgrade torrent that needs a cap), for the preview.
    """
    actions, rows, new_state = [], [], {}
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
               'step': prev['step'] if prev else 0, 'next': 'none'}
        rows.append(row)

        if not stalled:
            # Downloading and not stalled: it moves, or it moved recently. Its ladder is
            # reset (the entry is dropped). Any other state (queued, paused, checking) is
            # not ours to judge: keep the entry as it is and do nothing.
            if prev and info.get('state') != 'Downloading':
                new_state[h] = dict(prev)
            else:
                row['step'] = 0
            continue

        if prev is None:
            entry = {'step': 1, 'step_at': now, 'stalled_since': now, 'notified': False}
            actions.append({'action': REANNOUNCE, 'hash': h, 'name': name, 'step': 1})
            actions.append({'action': QUEUE_TOP, 'hash': h, 'name': name, 'step': 1})
            row['step'] = 1
            row['next'] = f'step 1 now: reannounce and queue to top; pause and resume after {cfg.stuck_after_min} min'
            new_state[h] = entry
            continue

        entry = dict(prev)
        new_state[h] = entry
        stalled_for = now - entry['stalled_since']
        if stalled_for >= cfg.give_up_s:
            if progress >= GIVE_UP_PROGRESS:
                if not entry.get('notified'):
                    actions.append({'action': NOTIFY, 'hash': h, 'name': name, 'progress': progress,
                                    'stalled_hours': round(stalled_for / 3600, 1)})
                    entry['notified'] = True
                    row['next'] = 'notify now (95% or more: never given up)'
                else:
                    row['next'] = 'none: already notified (95% or more: never given up)'
            else:
                actions.append({'action': GIVE_UP, 'hash': h, 'name': name, 'label': label,
                                'progress': progress, 'step': 3})
                entry['step'] = 3
                row['step'] = 3
                row['next'] = 'step 3 now: remove through the arr queue with blocklist, search again'
            continue
        if entry['step'] < 2 and now - entry['step_at'] >= cfg.after_s:
            actions.append({'action': PAUSE, 'hash': h, 'name': name, 'step': 2})
            actions.append({'action': RESUME, 'hash': h, 'name': name, 'step': 2})
            entry['step'] = 2
            entry['step_at'] = now
            row['step'] = 2
            row['next'] = f'step 2 now: pause and resume; give up in {_hours_left(cfg, stalled_for)}'
        elif entry['step'] < 2:
            wait = max(0, cfg.after_s - (now - entry['step_at']))
            row['next'] = f'step 2 in {int(wait // 60) + (1 if wait % 60 else 0)} min: pause and resume'
        else:
            row['next'] = f'give up in {_hours_left(cfg, stalled_for)}' if progress < GIVE_UP_PROGRESS \
                else f'notify in {_hours_left(cfg, stalled_for)} (95% or more: never given up)'
    return actions, new_state, rows


def _hours_left(cfg, stalled_for):
    return f'{max(0.0, (cfg.give_up_s - stalled_for) / 3600):.1f} h'
