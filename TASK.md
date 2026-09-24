# TASK: arr-sonarr-new-request-priority

## Confirmed defect (observed, not suspected)

In `handle_grab` (arr-webhook.py), the non-upgrade branch is a bare early
return: `if not upgrade:` logs "is a new release, not throttling" and does
nothing to Deluge. Reproduced by reading the code path end-to-end: upgrades
are explicitly moved to the BOTTOM of the Deluge queue (`core.queue_bottom`,
arr-webhook.py:3041) so they throttle behind real work, but a NEW Sonarr
search/download request (episode with no file yet) is left wherever Deluge
appended it -- i.e. at the back of a plain FIFO queue, behind every in-flight
download including throttled upgrades. The hourly `prioritize_normal_torrents`
pass only reorders torrents that already carry a sonarr/radarr label and runs
at most once an hour, so a fresh request can sit queued for up to 60 minutes
behind slower items.

## Entry point

arr-webhook.py:2937 (the `if not upgrade:` branch of `handle_grab`)

## Required change

new Sonarr search/download requests that are NOT upgrades must be queued/prioritized ahead of items already in the queue, instead of going to the back of a plain FIFO queue

Behaviour that must NOT change:
- Upgrades (episode already has a file) are still throttled exactly as before:
  over-10GB ones get the `-upgrade` label and `core.queue_bottom`; under-10GB
  upgrades are left alone. The new prioritization must NOT fire for upgrades.
- A Grab webhook with no `downloadId` is still skipped without any Deluge call.
- Radarr grabs keep their existing behaviour (the change is Sonarr-scoped); a
  non-Sonarr source must not raise in the new code path.
- The prioritization helper never raises out of `handle_grab`: a Deluge error
  is logged and swallowed, exactly like the rest of the grab-time helpers.

## Must contain

- `core.queue_top` -- the Deluge RPC method that moves a torrent ahead of everything already queued; the correct change necessarily calls it for new (non-upgrade) Sonarr grabs.

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
