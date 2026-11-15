"""Pure planners for trimming Deluge: the upgrade-lane backlog cap and the
retirement of long-seeded torrents. No I/O here; arr-webhook.py fetches the
torrents, calls these, and does the removals through remove_torrent (which
keeps its own seed floor and library guards).

Upgrade-lane cap (added 2026-11-15 at Penn's request: "cap the backlog at 30
torrents"). The lane is every unfinished torrent carrying an upgrade label.
A torrent that has downloaded anything is never dropped: it may already be
announced with data, so dropping it could leave a partial on a tracker. Only
untouched torrents (progress 0, total_done 0, both known) are droppable, and
of those the ones next in line to run are kept until the lane holds `cap`.
This is a deliberate trim, unlike the 2026-09-26 queue-wipe incident, where
purge_stalled_upgrade_torrents removed the whole queued backlog by mistake.

Seed retirement (same request: "we also need to reduce seeding torrents").
A finished torrent that has seeded at least `min_days` on a label we manage,
in a swarm that already has more than `min_swarm` other seeds, has paid its
dues (TorrentLeech's hit-and-run rule is 10 days or 1:1) and can stop seeding.

Idle rule (Penn, 11-15: "can we do variable based on seeding activity?"). A
torrent nobody has downloaded from in `idle_days` retires as soon as it has
seeded `idle_min_days`, instead of waiting out `min_days`; a torrent that is
still uploading keeps seeding until `min_days`. Activity is Deluge's
time_since_upload (libtorrent keeps it across restarts). -1 means no upload
on record: idle only when total_uploaded is also known to be 0. Anything
unknown is not idle. The swarm rule applies to both.
"""

DROPPABLE_STATES = ('Queued', 'Paused', 'Downloading')


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _untouched(t):
    """Known to have nothing downloaded. Unknown values are not untouched."""
    return (_num(t.get('progress')) == 0 and _num(t.get('total_done')) == 0
            and t.get('state') in DROPPABLE_STATES)


def lane_torrents(torrents, lane_labels):
    """Unfinished torrents in the upgrade lane (hash -> info)."""
    out = {}
    for h, t in (torrents or {}).items():
        progress = _num(t.get('progress'))
        if (t.get('label') or '') in lane_labels and (progress is None or progress < 100):
            out[h] = t
    return out


def upgrade_lane_plan(torrents, cap, lane_labels, recent_labels=()):
    """(keep, drop) hash lists for the upgrade lane. cap <= 0 means no cap.

    Kept first: everything with progress (or unknown progress). Then the
    untouched ones in run order (a Downloading one already holds a slot, then
    recent-upgrade labels, then Deluge queue position, then oldest added) until
    the lane holds `cap`. The rest of the untouched ones are dropped."""
    lane = lane_torrents(torrents, lane_labels)
    started = sorted(h for h, t in lane.items() if not _untouched(t))
    fresh = [h for h, t in lane.items() if _untouched(t)]

    def order(h):
        t = lane[h]
        q = _num(t.get('queue'))
        added = _num(t.get('time_added'))
        return (t.get('state') != 'Downloading',
                (t.get('label') or '') not in recent_labels,
                q if q is not None and q >= 0 else float('inf'),
                added if added is not None else float('inf'),
                h)

    fresh.sort(key=order)
    if not cap or cap <= 0:
        return started + fresh, []
    room = max(0, cap - len(started))
    return started + fresh[:room], fresh[room:]


def idle_seconds(t):
    """Seconds since this torrent last uploaded; inf when it never has (both
    time_since_upload == -1 and total_uploaded == 0); None when unknown."""
    tsu = _num(t.get('time_since_upload'))
    if tsu is None:
        return None
    if tsu >= 0:
        return tsu
    return float('inf') if _num(t.get('total_uploaded')) == 0 else None


def seed_retire_rule(t, labels, min_days, min_swarm, idle_days=0, idle_min_days=0):
    """'age' (seeded min_days), 'idle' (seeded idle_min_days and no upload in
    idle_days) or None. min_days <= 0 turns the age rule off, idle_days <= 0
    the idle rule. Unknown seed time, swarm size or activity never qualifies.

    With the idle rule on, a torrent still uploading is never retired, however
    long it has seeded (Penn, 11-15: "if its seeding actively i want to leave
    it, just torrents that aren't doing anything"): the age rule then only
    puts idle torrents that are past min_days first in line."""
    if (t.get('label') or '') not in labels:
        return None
    if t.get('state') != 'Seeding' or t.get('is_finished') is not True:
        return None
    st = _num(t.get('seeding_time'))
    seeds = _num(t.get('total_seeds'))
    if st is None or seeds is None or seeds < 0 or seeds <= min_swarm:
        return None
    idle_on = bool(idle_days and idle_days > 0)
    if idle_on:
        idle = idle_seconds(t)
        if idle is None or idle < idle_days * 86400:
            return None
    if min_days and min_days > 0 and st >= min_days * 86400:
        return 'age'
    if idle_on and st >= max(idle_min_days or 0, 0) * 86400:
        return 'idle'
    return None


def seed_retire_picks(torrents, labels, min_days, min_swarm, idle_days=0, idle_min_days=0, limit=0):
    """[(hash, rule)] to stop seeding: aged-out torrents first (longest seeded
    first), then idle ones (longest idle first, never-uploaded before all).
    With the idle rule on, both are idle torrents only. limit > 0 caps one run."""
    out = []
    for h, t in (torrents or {}).items():
        rule = seed_retire_rule(t, labels, min_days, min_swarm, idle_days, idle_min_days)
        if rule == 'age':
            out.append((0, -t['seeding_time'], h, rule))
        elif rule == 'idle':
            out.append((1, -idle_seconds(t), h, rule))
    out.sort()
    picks = [(h, rule) for _, _, h, rule in out]
    return picks[:limit] if limit and limit > 0 else picks


def seed_retire_plan(torrents, labels, min_days, min_swarm, limit=0, idle_days=0, idle_min_days=0):
    """Hashes of finished torrents to stop seeding (seed_retire_picks order).
    With idle_days 0 this is the age rule alone: longest-seeded first."""
    return [h for h, _ in seed_retire_picks(torrents, labels, min_days, min_swarm,
                                            idle_days, idle_min_days, limit)]


def parse_labels(raw, default):
    """Comma-separated label list from an env value; blank means default."""
    items = [s.strip() for s in (raw or '').split(',') if s.strip()]
    return tuple(items) if items else tuple(default)
