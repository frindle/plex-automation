# TASK: arr-webhook-recent-upgrade-priority-s3-sonarr-recent-fasttrack

## Confirmed defect (observed, not suspected)

`relabel_sonarr_upgrades()` in `arr-webhook.py` already fetches the full episode
object from `GET /api/v3/episode/{id}` to read `hasFile`, but it throws away the
rest of that response: every confirmed upgrade -- including one whose episode
aired THIS year -- is labeled with the throttled `sonarr-upgrade` label and pushed
to the BOTTOM of the Deluge queue. The Radarr mirror (`relabel_radarr_upgrades`)
already fast-tracks recent upgrades via `RADARR_UPG_PRIORITY_LABEL` +
`core.queue_top`; Sonarr has no equivalent, so brand-new episode upgrades are
throttled behind old ones. Verified by reading the function: it only ever calls
`ensure_label_exists_named(SONARR_UPG_LABEL)` / `set_torrent_label(..., SONARR_UPG_LABEL)`
and posts a single `core.queue_bottom`; there is no priority branch and no
`airDateUtc` handling anywhere in the Sonarr path.

## Entry point

arr-webhook.py:3403 (`def relabel_sonarr_upgrades():`)

## Required change

In arr-webhook.py, the Sonarr mirror of the Radarr fast-track.
1. Add a label constant next to `SONARR_UPG_LABEL`:
   SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')
2. `relabel_sonarr_upgrades()` already fetches the full episode object from GET /api/v3/episode/{id} to read `hasFile`. That SAME response carries `airDateUtc` (an ISO date string, e.g. "2026-03-14"). Parse the year from it -- `int(str(air_date)[:4])` -- and branch on `_is_recent_year(air_year)`. Use the EPISODE's air year, never the series' start year: a show that began in 2015 can still air a brand-new episode this year.
   - RECENT -> `ensure_label_exists_named(SONARR_UPG_PRIORITY_LABEL)`, `set_torrent_label(torrent_hash, SONARR_UPG_PRIORITY_LABEL)`, collect into a separate priority list sent to `core.queue_top` (only when non-empty).
   - NOT recent -> existing path unchanged: `SONARR_UPG_LABEL` + `core.queue_bottom`.
3. Fall back to `airDate` when `airDateUtc` is absent. An absent/empty/unparseable date is NOT recent -> existing throttled path. Parsing must never raise out of the function.
4. The returned relabeled count must include BOTH branches (same bug as the Radarr slice).

ADVERSARIAL CASES THE FIXTURE MUST CARRY: an episode with a this-year `airDateUtc` -> priority label + core.queue_top, never bottomed, and the returned count is 1; an episode with `airDateUtc` ABSENT but a recent `airDate` -> falls back and still fast-tracks; an episode with NO air date at all -> throttled lane, not crashed; an episode with a decade-old airDateUtc -> SONARR_UPG_LABEL + core.queue_bottom.

Behaviour that must NOT change:
- The Radarr path (`relabel_radarr_upgrades`) is untouched and keeps its existing priority/throttled split.
- Episodes with `hasFile` false are still skipped entirely (no relabel, no queue move).
- Torrents not present in the Sonarr queue mapping are still skipped.
- A failed episode lookup still logs a warning and continues to the next torrent; the outer failure path still returns 0.
- The throttled branch keeps posting `core.queue_bottom` only when its list is non-empty, exactly as before.

## Must contain

- `SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')`
- `airDateUtc`
- `int(str(air_date)[:4])`
- `_is_recent_year(air_year)`
- `ensure_label_exists_named(SONARR_UPG_PRIORITY_LABEL)`
- `set_torrent_label(torrent_hash, SONARR_UPG_PRIORITY_LABEL)`
- `'method': 'core.queue_top', 'params': [priority_hashes]`

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
