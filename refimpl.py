#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s7-scheduler-gate

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Change: monthly_search_scheduler's hourly poll no longer fires on the 1st of
the month; it checks each service (radarr, sonarr) against its persisted
'last_run' timestamp from _load_upgrade_state and runs that service's cycle
only when the entry is absent / has no 'last_run' key / or
UPGRADE_BATCH_INTERVAL_DAYS days have elapsed. Everything else in arr-webhook.py
is preserved untouched.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''    import datetime
    last_run_month = None
    while True:
        now = datetime.datetime.now()
        if now.day == 1 and now.month != last_run_month:
            last_run_month = now.month
            log.info('Monthly upgrade cycle starting')
            monthly_upgrade_cycle('radarr')
            # Sonarr runs after Radarr rather than in parallel so the two
            # bulk searches don't stack announces on the same tracker.
            monthly_upgrade_cycle('sonarr')
            log.info('Monthly upgrade cycle complete')
        time.sleep(3600)  # check every hour'''

NEW = r'''    import datetime
    while True:
        # last_run stamps are persisted in UTC (see radarr_bulk_search /
        # sonarr_bulk_search), so compare against a naive-UTC clock.
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        state = _load_upgrade_state()
        for service in ('radarr', 'sonarr'):
            entry = state.get(service) or {}
            last_run = entry.get('last_run')
            if not last_run:
                due = True  # first run: no persisted timestamp yet
            else:
                try:
                    elapsed_days = (now - datetime.datetime.fromisoformat(last_run)).total_seconds() / 86400.0
                except ValueError:
                    elapsed_days = float('inf')  # unparseable stamp -> treat as due
                due = elapsed_days >= UPGRADE_BATCH_INTERVAL_DAYS
            if due:
                log.info(f'{service}: upgrade batch interval reached, starting monthly cycle')
                # Services run sequentially rather than in parallel so the two
                # bulk searches don't stack announces on the same tracker.
                monthly_upgrade_cycle(service)
        time.sleep(3600)  # check every hour'''

if NEW in t:
    # Idempotent re-apply: a previous round already landed this edit (it was
    # sealed into the baseline). The gate still needs the apply step to SUCCEED
    # so its required-literals check can run against the target.
    print("refimpl already applied -- no-op")
elif OLD in t:
    p.write_text(t.replace(OLD, NEW, 1))
    print("refimpl applied")
else:
    raise SystemExit("refimpl anchor not found and new code absent -- did the target change?")
