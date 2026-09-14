# Task: /orphan-scan mode=all must not delete an untracked file Deluge is still seeding

## The defect, CONFIRMED
Read directly in `arr-webhook.py` `orphan_scan` (route at 5874). The scan already builds
`seeding_basenames` from Deluge and reroutes a `dupe` file to the protected `dupe_seeding`
bucket when its basename is seeding (≈line 5985). But the `untracked` bucket gets NO such
protection: under `?mode=all` (`to_delete_buckets = ['sample', 'dupe', 'untracked']`) every
untracked file is `os.remove`d (≈line 6012) with no Deluge cross-check. A file a torrent is
actively seeding, whose path the scan classifies `untracked`, is deleted out from under the
seed → hit-and-run. `/incomplete-orphans` cross-references Deluge for exactly this reason.

## The property to satisfy
Before an `untracked` file is deleted, cross-reference Deluge's seeding basenames the same way
`dupe` already does, and SKIP (protect) any untracked file that is actively seeding — including
the fail-safe case where the Deluge cross-check was unavailable (`seeding_basenames is None`).

## Entry points (edit ONLY `arr-webhook.py`)
Only edit `arr-webhook.py`.
- The `buckets = {...}` initialiser (≈line 5965): add an `'untracked_seeding': []` bucket.
- The reroute block `if cat == 'dupe':` (≈line 5985): also reroute a seeding `untracked` file
  to `'untracked_seeding'`.
Do not change the delete loop, `_classify`, the mode handling, `verify.sh`, or `fixture_orphan.py`.
(`untracked_seeding` is not in any `to_delete_buckets` list, so protected files are never removed.)

## Exactly what to change
1. `buckets = {'sample': [], 'dupe': [], 'dupe_seeding': [], 'untracked': [], 'untracked_seeding': []}`
2. Replace
       if cat == 'dupe':
           if seeding_basenames is None or f in seeding_basenames:
               cat = 'dupe_seeding'
   with
       if cat in ('dupe', 'untracked'):
           if seeding_basenames is None or f in seeding_basenames:
               cat = 'dupe_seeding' if cat == 'dupe' else 'untracked_seeding'

## Must contain
- `'untracked_seeding'`
- `cat in ('dupe', 'untracked')`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints `VERIFY_OK`.
Only edit `arr-webhook.py`; do not edit `verify.sh`.
