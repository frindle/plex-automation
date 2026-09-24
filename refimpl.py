#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s11-scheduler-shared-quota

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Two surgical replacements in arr-webhook.py:
  1. monthly_search_scheduler's per-service upgrade_batch_due gate becomes the
     shared weekly-quota gate with 'next_service' alternation.
  2. monthly_upgrade_cycle captures relabel()'s integer return and routes it
     through record_upgrades_found so the quota only advances on CONFIRMED
     queued upgrades (the manual /run-monthly-upgrade endpoint goes through
     monthly_upgrade_cycle, so it inherits the same recording).

Everything else in the file is preserved untouched.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD_SCHEDULER = r'''def monthly_search_scheduler():
    """
    On the 1st of each month, Radarr first and then the identical Sonarr
    cycle (Sonarr was silently missing a lot of episodes because nothing
    ever bulk-searched it):
    1. Purge stalled <service>-upgrade torrents
    2. Wait 30 minutes
    3. Trigger bulk search
    4. Wait 5 minutes
    5. Relabel new upgrade torrents to the throttled lane, queue them last
    """
    import datetime
    while True:
        # last_run stamps are persisted in UTC (see radarr_bulk_search /
        # sonarr_bulk_search), so compare against a naive-UTC clock.
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        state = _load_upgrade_state()
        for service in ('radarr', 'sonarr'):
            entry = state.get(service) or {}
            due = upgrade_batch_due(entry, now)
            if due:
                log.info(f'{service}: upgrade batch interval reached, starting monthly cycle')
                # Services run sequentially rather than in parallel so the two
                # bulk searches don't stack announces on the same tracker.
                monthly_upgrade_cycle(service)
        time.sleep(3600)  # check every hour'''

NEW_SCHEDULER = r'''def monthly_search_scheduler():
    """Hourly shared-quota gate for the radarr/sonarr upgrade cycles.

    Each poll checks the rolling 7-day shared quota via weekly_quota_state;
    if WEEKLY_UPGRADE_QUOTA is exhausted, no service runs this poll at all.
    Otherwise exactly one service gets the turn -- alternating via the
    persisted top-level 'next_service' key ('radarr' or 'sonarr', defaulting
    to 'radarr') -- and after its monthly_upgrade_cycle completes the key is
    flipped to the other service and state saved before the next hourly sleep.

    The per-service interval gate (upgrade_batch_due / UPGRADE_BATCH_INTERVAL_DAYS)
    is no longer consulted here; both stay defined for backward compatibility."""
    import datetime
    while True:
        # last_run stamps are persisted in UTC, so compare against a naive-UTC clock.  # relevance: unobservable
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        state = _load_upgrade_state()
        quota_entry = weekly_quota_state(state, now)
        if WEEKLY_UPGRADE_QUOTA - quota_entry['count'] <= 0:
            log.info('weekly upgrade quota exhausted; skipping this poll')
            time.sleep(3600)  # check every hour
            continue
        service = state.get('next_service') or 'radarr'
        if service not in ('radarr', 'sonarr'):
            service = 'radarr'
        log.info(f'{service}: shared upgrade quota available, starting monthly cycle')
        monthly_upgrade_cycle(service)
        state['next_service'] = 'sonarr' if service == 'radarr' else 'radarr'
        _save_upgrade_state(state)
        time.sleep(3600)  # check every hour'''

OLD_CYCLE_TAIL = r'''    log.info(f'{service}: waiting {wait_before_relabel}s before relabeling upgrades...')
    time.sleep(wait_before_relabel)
    relabel()
    log.info(f'{service}: monthly cycle complete')'''

NEW_CYCLE_TAIL = r'''    log.info(f'{service}: waiting {wait_before_relabel}s before relabeling upgrades...')
    time.sleep(wait_before_relabel)
    count = relabel() or 0
    state = _load_upgrade_state()
    record_upgrades_found(state, count)
    log.info(f'{service}: monthly cycle complete ({count} upgrade(s) confirmed queued)')'''

assert OLD_SCHEDULER in t, "refimpl anchor not found (scheduler) -- did the target change?"
t = t.replace(OLD_SCHEDULER, NEW_SCHEDULER, 1)
assert OLD_CYCLE_TAIL in t, "refimpl anchor not found (cycle tail) -- did the target change?"
t = t.replace(OLD_CYCLE_TAIL, NEW_CYCLE_TAIL, 1)

p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(t)
print("refimpl applied")
