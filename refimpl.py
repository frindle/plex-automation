#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s10-shared-weekly-quota

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

# 1) WEEKLY_UPGRADE_QUOTA constant, immediately after UPGRADE_BATCH_SIZE.
OLD_CONST = r'''UPGRADE_BATCH_SIZE = int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))
'''
NEW_CONST = r'''UPGRADE_BATCH_SIZE = int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))
# Shared cap across BOTH radarr and sonarr combined: max upgrades confirmed
# queued within a rolling 7-day window (replaces the per-service days-since-
# last-run gate as the throttle).
WEEKLY_UPGRADE_QUOTA = int(os.environ.get('WEEKLY_UPGRADE_QUOTA', '10'))
'''

assert OLD_CONST in t, "refimpl anchor not found -- did the target change?"
t = t.replace(OLD_CONST, NEW_CONST, 1)

# 2) Helpers after _save_upgrade_state.
OLD_HELPERS = r'''def _save_seed_state(state):
    try:
        with open(SEED_STATE_PATH, 'w') as f:
            _json.dump(state, f)
    except Exception as e:
        log.warning(f'[seed-tracking] failed to persist state: {e}')
'''
NEW_HELPERS = r'''def weekly_quota_state(state, now=None):
    """Current shared weekly-upgrade quota entry for the given state dict.

    Reads the top-level 'quota' key (shape {'count': int, 'week_start':
    ISO-8601 string}). When absent, or when `now` is >= 7 days past the
    parsed week_start (or week_start is unparseable), the window has expired:
    return a FRESH {'count': 0, 'week_start': <now>} dict without mutating
    the passed-in state. Otherwise return the existing entry unchanged.
    `now` defaults to a naive-UTC clock, matching upgrade_batch_due.
    """
    import datetime
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    fresh = {'count': 0, 'week_start': now.isoformat()}
    entry = (state or {}).get('quota')
    if not isinstance(entry, dict):
        return fresh
    try:
        elapsed_days = (now - datetime.datetime.fromisoformat(entry['week_start'])).total_seconds() / 86400.0
    except (KeyError, TypeError, ValueError):
        return fresh  # unparseable week_start -> window expired
    if elapsed_days >= 7:
        return fresh
    return entry

def record_upgrades_found(state, n):
    """Add n confirmed-queued upgrades to the current quota entry and persist.

    Takes the already-reset-if-needed entry (see weekly_quota_state), adds n
    to its 'count', writes it back into state['quota'], then persists via
    _save_upgrade_state. Does NOT enforce the cap itself -- callers check
    remaining capacity via WEEKLY_UPGRADE_QUOTA - quota_entry['count'] before
    deciding whether to run a pass.
    """
    entry = weekly_quota_state(state)
    entry['count'] += n
    state['quota'] = entry
    _save_upgrade_state(state)

def _save_seed_state(state):
    try:
        with open(SEED_STATE_PATH, 'w') as f:
            _json.dump(state, f)
    except Exception as e:
        log.warning(f'[seed-tracking] failed to persist state: {e}')
'''

assert OLD_HELPERS in t, "refimpl anchor not found -- did the target change?"
t = t.replace(OLD_HELPERS, NEW_HELPERS, 1)

p.write_text(t)
print("refimpl applied")
