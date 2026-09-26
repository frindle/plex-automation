# TASK: arr-webhook-recent-upgrade-priority-s4-hourly-sweep-keeps-recent-on-top

## Confirmed defect (observed, not suspected)

`prioritize_normal_torrents()` in `arr-webhook.py` runs hourly and re-enforces
Deluge queue placement purely from LABELS: torrents whose label is in
`priority_labels` are sent to `core.queue_top`, those in `upgrade_labels` to
`core.queue_bottom`. The two recent-upgrade labels (`sonarr-upgrade-recent` /
`radarr-upgrade-recent`, i.e. `SONARR_UPG_PRIORITY_LABEL` /
`RADARR_UPG_PRIORITY_LABEL`) are in NEITHER set today, so a fast-tracked
recent upgrade is silently ignored by the sweep and drops back down the queue
within an hour -- undoing the whole feature. Verified by reading the label sets
at the top of `prioritize_normal_torrents()`: `priority_labels = {'sonarr', 'radarr'}`
and `upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}`, with no reference
to either `*_UPG_PRIORITY_LABEL` constant.

## Entry point

arr-webhook.py:3696 (the `priority_labels = {'sonarr', 'radarr'}` line inside
`prioritize_normal_torrents()`, defined at arr-webhook.py:3688)

## Required change

In arr-webhook.py, close the hole that would silently undo the whole feature within an hour. `prioritize_normal_torrents()` runs hourly and re-enforces queue placement purely from LABELS: `priority_labels` -> core.queue_top, `upgrade_labels` -> core.queue_bottom. The two new `*-upgrade-recent` labels are in NEITHER set today, so a fast-tracked torrent is simply ignored by the sweep -- and if anyone adds them to `upgrade_labels` it is actively re-bottomed. The parent intent says a recent upgrade must be 'top of the Deluge queue, NEVER bottomed'.
Required change: make the priority set
  priority_labels = {'sonarr', 'radarr', SONARR_UPG_PRIORITY_LABEL, RADARR_UPG_PRIORITY_LABEL}
so the hourly pass sweeps recent-upgrade torrents to core.queue_top exactly like plain 'sonarr'/'radarr' ones. Do NOT add either new label to `upgrade_labels` -- that set stays {SONARR_UPG_LABEL, RADARR_UPG_LABEL}.

Behaviour that must NOT change:
- plain 'sonarr'/'radarr' labeled torrents still go to core.queue_top;
- SONARR_UPG_LABEL / RADARR_UPG_LABEL ('sonarr-upgrade'/'radarr-upgrade') torrents still go to core.queue_bottom and are NOT in the top list;
- a torrent labeled `radarr-upgrade-recent` appears in the core.queue_top hash list and in NO core.queue_bottom call; likewise `sonarr-upgrade-recent`;
- no Deluge call is made for an empty list (no matching labels -> zero posts);
- the function stays exception-safe: any failure inside the try block is logged, never raised out.

## Must contain

- `priority_labels = {'sonarr', 'radarr', SONARR_UPG_PRIORITY_LABEL, RADARR_UPG_PRIORITY_LABEL}`
- `upgrade_labels = {SONARR_UPG_LABEL, RADARR_UPG_LABEL}`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

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
