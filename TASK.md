# TASK: arr-webhook-recent-upgrade-priority

## Confirmed defect (observed, not suspected)

The yearly-upgrade background system in `arr-webhook.py` already detects when a
radarr/sonarr-labeled Deluge torrent is actually an UPGRADE (the movie/episode
already hasFile) and deliberately deprioritizes it: `relabel_radarr_upgrades` /
`relabel_sonarr_upgrades` relabel it to `RADARR_UPG_LABEL` / `SONARR_UPG_LABEL`
and move it to `core.queue_bottom`, and the hourly `prioritize_normal_torrents`
pass re-enforces that every hour (labels in `priority_labels` -> queue_top,
labels in `upgrade_labels` -> queue_bottom).

Observed problem: an upgrade for a RECENT release -- e.g. a 2026 movie or a
brand-new 2026 episode of a show that started in 2015 -- is throttled into the
bottom-of-queue upgrade lane exactly like a decade-old re-grab, even though it
is effectively a fresh download the user wants now. The relabel functions have
the recency data on hand (Radarr's movie object already carries `year`; Sonarr's
episode response from GET /api/v3/episode/{id} already carries `airDateUtc`) but
ignore it when choosing queue placement.

## Entry point

`arr-webhook.py`: `relabel_radarr_upgrades()` and `relabel_sonarr_upgrades()`
(the relabel + queue_bottom block), plus `prioritize_normal_torrents()` (the
hourly re-enforcement pass). Label constants live next to
`SONARR_UPG_LABEL` / `RADARR_UPG_LABEL`.

## Required change

Add a fast-track: when the underlying media is RECENT -- its release year is the
current year or the immediately preceding year -- an upgrade for it should behave
like a normal priority download instead: top of the Deluge queue, never bottomed.
Everything else about the existing throttled-upgrade behavior (older-than-last-year
upgrades, the weekly quota, the batch cursor, `purge_stalled_upgrade_torrents`) is
unchanged.

1. Add a helper:

   ```python
   def _is_recent_year(year, now=None) -> bool
       # now defaults to datetime.now(timezone.utc); returns True iff year is a
       # truthy int-or-int-like value and int(year) >= now.year - 1.
       # None/0/missing/non-numeric -> False.
   ```

2. Add two new label env-var constants next to `SONARR_UPG_LABEL` /
   `RADARR_UPG_LABEL`:

   ```python
   RADARR_UPG_PRIORITY_LABEL = os.environ.get('RADARR_UPGRADE_PRIORITY_LABEL', 'radarr-upgrade-recent')
   SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')
   ```

3. In `relabel_radarr_upgrades`: for each torrent whose movie hasFile, branch on
   `_is_recent_year(movie.get('year'))`. Recent -> `ensure_label_exists_named(RADARR_UPG_PRIORITY_LABEL)`,
   `set_torrent_label(torrent_hash, RADARR_UPG_PRIORITY_LABEL)`, collect into a separate
   `priority_hashes` list sent to `core.queue_top`. Not-recent -> unchanged existing path
   (`RADARR_UPG_LABEL`, queue_bottom). Both branches still count toward the function's
   returned relabeled count.

4. In `relabel_sonarr_upgrades`: same split, using `_is_recent_year` on the year parsed
   from the episode response's `airDateUtc` (fall back to `airDate` if `airDateUtc` is
   absent; unparseable/missing -> not recent, existing throttled path). Recent ->
   `SONARR_UPG_PRIORITY_LABEL` + queue_top; not-recent -> unchanged (`SONARR_UPG_LABEL`,
   queue_bottom).

5. In `prioritize_normal_torrents`: add `RADARR_UPG_PRIORITY_LABEL` and
   `SONARR_UPG_PRIORITY_LABEL` to `priority_labels` (swept to queue_top every hour, same as
   the plain 'sonarr'/'radarr' labels). Do NOT add them to `upgrade_labels` -- they must
   never be swept to queue_bottom.

Behaviour that must NOT change:
- Older-than-last-year upgrades still get `RADARR_UPG_LABEL` / `SONARR_UPG_LABEL` and are
  moved to `core.queue_bottom`.
- The relabel functions' returned count includes BOTH recent (priority) and older
  (throttled) relabeled torrents.
- `purge_stalled_upgrade_torrents`, the weekly quota, `monthly_upgrade_cycle`, and the
  batch cursor / search selection logic are untouched.
- Torrents whose movie/episode does NOT haveFile are never relabeled at all.

## Must contain

- `_is_recent_year`
- `def _is_recent_year(year, now=None)`
- `RADARR_UPG_PRIORITY_LABEL = os.environ.get('RADARR_UPGRADE_PRIORITY_LABEL', 'radarr-upgrade-recent')`
- `SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')`
- `_is_recent_year(movie.get('year'))`
- `ensure_label_exists_named(RADARR_UPG_PRIORITY_LABEL)`
- `set_torrent_label(torrent_hash, RADARR_UPG_PRIORITY_LABEL)`
- `priority_hashes.append(torrent_hash)`
- `'method': 'core.queue_top', 'params': [priority_hashes]`
- `_is_recent_year(air_year)`
- `ensure_label_exists_named(SONARR_UPG_PRIORITY_LABEL)`
- `set_torrent_label(torrent_hash, SONARR_UPG_PRIORITY_LABEL)`
- `airDateUtc`
- `priority_labels = {'sonarr', 'radarr', SONARR_UPG_PRIORITY_LABEL, RADARR_UPG_PRIORITY_LABEL}`

## Scope

Only edit `arr-webhook.py` (via `refimpl.py`) and `test_fixture.py`; do not edit
`verify.sh` or `check_literals.py`. test_fixture.py is the adversarial fixture --
it must stay strict: every changed line of the reference impl is either asserted
by a case in it, or carries an explicit `# relevance: unobservable` marker.

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
