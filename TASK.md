# TASK: arr-new-grab-queue-top-r2

## Confirmed defect (observed, not suspected)

New (non-upgrade) grabs are never reordered in Deluge. `handle_grab` hits the
`if not upgrade:` branch and only logs "is a new release, not throttling" then
returns without making any Deluge call at all -- verified by reading
arr-webhook.py:2867-2881: that branch contains no `session.post`, unlike the
upgrade branch which drives `{DELUGE_URL}/json`. Meanwhile real upgrades over
10GB are pushed to the BOTTOM of the queue (`core.queue_bottom`) and stay there
while they download, so a brand-new request sits behind every throttled upgrade
torrent instead of starting immediately.

## Entry point

arr-webhook.py:2879 -- the `if not upgrade:` block inside `handle_grab` (def at line 2867)

## Required change

In handle_grab a new non-upgrade grab must be moved to the TOP of the Deluge queue immediately via core.queue_top instead of returning without reordering, so new requests sit ahead of throttled upgrade torrents.

Mirror the existing upgrade branch pattern: call `deluge_login()` then
`session.post(f'{DELUGE_URL}/json', json={'method': 'core.queue_top', 'params': [download_id], ...}, timeout=...)`. Wrap it in try/except so a Deluge failure is logged and swallowed (the webhook worker thread must not crash). Only the non-upgrade branch changes; keep its final `return`.

Behaviour that must NOT change:
- An upgrade grab over 10GB is still throttled to `core.queue_bottom` with the -upgrade label, exactly as before -- it must NOT be topped.
- An upgrade grab under 10GB still returns without any Deluge call (no queue_top, no queue_bottom).
- A Grab payload with a missing/empty downloadId still returns early and makes NO Deluge calls at all.
- The webhook routes (/webhook/radarr, /webhook/sonarr) still answer 200 {'status': 'ok'} for Grab events even when Deluge is unreachable.

## Must contain

- `core.queue_top`
- `deluge_login()`

## Scope

Only edit `arr-webhook.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
