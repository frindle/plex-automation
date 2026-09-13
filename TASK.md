# TASK: plex-hnr-guard

## Confirmed defect (observed, not suspected)

confirmed: 9 completed radarr-upgrade torrents in /data/Downloads/Complete/radarr had their DATA deleted off disk while Deluge was still seeding them AND the tracker still showed them registered (Announce OK) and under their seed-time requirement -> 9 live tracker hit-and-runs. Data-deleting removals (remove_data=True) run without a central still-registered/under-seed guard; purge_non_radarr and purge_stalled_upgrade_torrents call core.remove_torrent/core.remove_torrents by direct RPC, bypassing the wrapper entirely.

## Entry point

arr-webhook.py:423

## Required change

Add a single hit-and-run choke point to the `remove_torrent` wrapper
(arr-webhook.py:423) so that NO code path can delete a torrent's DATA while the
tracker still knows it AND its seed obligation is unmet.

Concretely:
1. Change the signature to `def remove_torrent(torrent_hash, remove_data=True, info=None):`.
2. When `remove_data` is True: if `info` is None, fetch the torrent's current
   status via a `core.get_torrent_status` RPC requesting the fields
   `['tracker_status', 'seeding_time']` (same DELUGE_URL/session pattern as the
   existing call, id 8, timeout 10). If that fetch raises, catch it, log a
   warning, and set `info = {}` (fail SAFE -- an unknown status is treated as
   still-registered, so files are kept).
3. Compute `seeding_time = info.get('seeding_time') or 0`. If the torrent is
   still registered (`not torrent_is_unregistered(info)`) AND
   `seeding_time < SEED_DAYS * 86400`, DOWNGRADE the deletion: log a warning
   containing the marker `HnR guard`, call `record_activity('hnr-guard', ...)`,
   and set `remove_data = False` (the torrent entry is still removed, but the
   files stay on disk).
4. Then perform the existing `core.remove_torrent` RPC using the (possibly
   downgraded) `remove_data` value, unchanged otherwise.
5. Route the two wrapper-bypassing direct-RPC removals through the wrapper so
   they inherit the guard:
   - `purge_non_radarr` (~arr-webhook.py:3725): replace the direct
     `core.remove_torrent [h, True]` POST with `remove_torrent(h)`.
   - `purge_stalled_upgrade_torrents` (~arr-webhook.py:3269): replace the direct
     `core.remove_torrents [to_remove, False]` POST with a loop calling
     `remove_torrent(_h, remove_data=False)` for each hash.

Behaviour that must NOT change:
- `remove_torrent(hash, remove_data=False)` must still remove only the entry and
  keep files -- the guard only engages on data deletions.
- An UNREGISTERED torrent, or one whose `seeding_time >= SEED_DAYS * 86400`, must
  still have its data deleted (`remove_data` stays True) -- the guard must not
  block legitimate cleanup (cleanup_superseded, same-group upgrade delete).
- The existing single `core.remove_torrent` RPC (id 7) still fires exactly once
  per call, with the resolved `remove_data`, followed by `resp.raise_for_status()`
  and the existing `log.info` line.

## Env vars the verify/fixture rely on
- `SEED_DAYS` (default 21) -- the seed-time threshold; the guard compares against
  `SEED_DAYS * 86400` seconds. The fixture reads `target.SEED_DAYS`; do not
  hardcode 21.

## Must contain

- `HnR guard`
- `hnr-guard`
- `remove_data = False`
- `info=None`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

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
