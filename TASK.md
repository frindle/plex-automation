# TASK: arr-codec-floor-s2-supersede-floor

## Confirmed defect (observed, not suspected)

In `handle_upgrade_import`'s supersede sweep, an imported x264 release supersedes
(or hard-deletes) an existing x265/HEVC release of the same title: the loop at
lines 2659-2706 matches on title + search term only and never compares codec
rank, so a lower-codec import destroys the better file. Reproduced by driving
`handle_upgrade_import` with an existing `... x265.mkv` torrent and an imported
`... x264.mkv`: baseline calls `supersede_torrent` on the x265 torrent (and
`remove_torrent` when it is same-group REPACK, unregistered at the tracker, and
seed-met).

## Entry point

arr-webhook.py:2671 — inside `handle_upgrade_import`, the branch
`if torrent_matches_any_title(name, title_variants) and search_term.lower() in name.lower():`
in the `for torrent_hash, info in torrents.items()` loop (lines 2659-2706).

## Required change

In handle_upgrade_import's supersede sweep (the `for torrent_hash, info in torrents.items()` loop, lines 2659-2706), add a codec floor guard: before superseding OR hard-deleting a matched existing torrent, if codec_rank(existing name) > codec_rank(new import name) -- i.e. the existing release is x265/HEVC and the incoming import is a lower-rank codec such as x264 -- do NOT supersede/delete it; log the skip and `continue`. The guard is one-directional: it only blocks DOWNGRADES, it must not block a same-codec or genuine higher-codec upgrade. The new import's name is available in the enclosing scope (the same name matched against title_variants/search_term).

Use the existing `codec_rank(name)` helper (arr-webhook.py:226) on both sides;
compare the existing torrent's `name` against `new_filename`. Record the skip via
`record_activity('supersede-skipped', ...)` and a `log.info` line, then `continue`.

Behaviour that must NOT change:
- Same-codec imports (x264 -> x264) still supersede exactly as before.
- Genuine upgrades (existing x264, import x265) still supersede; the guard never blocks them.
- The hard-delete path (`should_hard_delete_on_upgrade` true: same-group REPACK, unregistered, seed-met) still deletes for non-downgrades — but a codec downgrade is blocked on that branch too.
- Torrents with no codec token in either name are unaffected (rank 0 vs rank 0 falls through).
- The new torrent itself (`torrent_hash == new_torrent_hash`) and already-superseded torrents are still skipped before the guard runs.

## Must contain

- `codec_rank(name) > codec_rank(new_filename)`
- `supersede-skipped`
- `record_activity('supersede-skipped'`

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
