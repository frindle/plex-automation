# Task: make seed-obligation authoritative on every data-delete path (HnR backstop)

## The defect, CONFIRMED
Read directly in `arr-webhook.py` on branch main. The Diplomat hardening added a
correct three-way AND gate to `should_hard_delete_on_upgrade` (same_group AND
tracker-unregistered AND `seeding_time >= SEED_DAYS*86400`), but the two OTHER
paths that can delete a torrent's DATA still use the pre-Diplomat OR-logic:

1. `remove_torrent` (the single choke point — the only `core.remove_torrent`
   call in the codebase) keeps files only when the torrent is BOTH still
   registered AND under its seed obligation:
   `if not torrent_is_unregistered(info) and seeding_time < SEED_DAYS * 86400:`
   So an **unregistered** torrent that has **not** met its seed obligation
   (the exact Diplomat case: a private tracker unregisters a superseded torrent
   the instant the repack posts, while still enforcing minimum seed time) falls
   through the guard and its data is deleted → hit-and-run.

2. `queued_superseded_targets` selects a Queued superseded torrent when
   `(progress == 0) or torrent_is_unregistered(info)` — same false assumption
   that "unregistered" means "nothing owed". A torrent at progress>0 that the
   tracker just dropped but that still owes seed time gets purged (data deleted).

`test_maintenance.py` asserts the buggy behaviour as correct (case `'e'`: an
unregistered, progress-100, no-seeding_time Queued torrent is expected in the
purge list `== ['a', 'e']`), so the regression is locked in by the test.

## The property to satisfy
A torrent's DATA may be removed ONLY when it owes the tracker no seed time —
i.e. `(info.get('seeding_time') or 0) >= SEED_DAYS * 86400` — OR it never took
any data (`progress == 0`, nothing to owe). Tracker registration status is NOT
a licence to delete: unregistered + seed-unmet MUST keep the files. This mirrors
the seed gate already in `should_hard_delete_on_upgrade`.

## Entry points (edit ONLY `arr-webhook.py` and `test_maintenance.py`)
Only edit `arr-webhook.py` and `test_maintenance.py`.
- `arr-webhook.py:438` — the HnR guard `if` line inside `remove_torrent`.
- `arr-webhook.py:628` — the final `and (...)` condition in `queued_superseded_targets`.
- `test_maintenance.py:202` — the assertion that reads `== ['a', 'e']`.

Do NOT touch `should_hard_delete_on_upgrade` (already correct), any other
function, or `verify.sh` / `fixture_hnr.py`.

## Exactly what to change
1. In `remove_torrent`, the guard condition becomes seed-obligation only, dropping
   the registration term — keep files whenever the obligation is unmet:
   `if seeding_time < SEED_DAYS * 86400:`
   (You may also update the adjacent `log.warning` text to say the reason is the
   unmet seed obligation rather than "tracker still knows it" — optional.)
2. In `queued_superseded_targets`, replace the `torrent_is_unregistered(info)`
   branch with a seed-met test so only zero-progress OR seed-met torrents purge:
   `and ((info.get('progress') or 0) == 0 or (info.get('seeding_time') or 0) >= SEED_DAYS * 86400)`
3. In `test_maintenance.py`, change the expectation from `== ['a', 'e']` to
   `== ['a']` (the unregistered-but-seed-unmet case `'e'` must no longer purge).

## Must contain (literal tokens that must appear in arr-webhook.py after the change)
- `if seeding_time < SEED_DAYS * 86400:`
- `(info.get('seeding_time') or 0) >= SEED_DAYS * 86400`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints
`VERIFY_OK`. Only edit the three files named above; do not edit `verify.sh`.
