#!/usr/bin/env python3
"""Reference impl for: arr-deluge-false-supersede-915

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Diagnosis: 525804e guards the MOVE after supersede; b368987 routes queue-dupe
losers through supersede. Neither touches the DECISION in dedup_via_radarr,
which relabels every non-keeper matched torrent as superseded without asking
the tracker whether that torrent is still registered. A torrent the tracker
has unregistered can no longer accrue seeding credit -- relabeling it
"superseded" (and moving it to the seed dir) is pure damage with nothing left
to seed, and it is exactly how a wrong candidate becomes "superseded" with no
user action. Fix: add should_supersede_candidate() (fail-safe toward
'supersede' on unknown status, mirroring torrent_is_unregistered) and gate the
relabel loop on it.

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = r'''def should_hard_delete_on_upgrade(info, same_group):'''
NEW = r'''def should_supersede_candidate(info):
    """Pure decision: may dedup_via_radarr relabel this candidate superseded?

    A torrent the tracker has ALREADY unregistered can no longer accrue
    seeding credit -- there is nothing left to seed and no hit-and-run risk,
    so relabeling it "superseded" (and moving it into the seed dir) is pure
    damage. Skip it: leave the label alone and let cleanup/hard-delete paths
    handle its fate. Fail-safe toward 'may supersede' on unknown/empty status,
    mirroring torrent_is_unregistered's direction of caution."""
    if not isinstance(info, dict):
        return True
    return not torrent_is_unregistered(info)


def should_hard_delete_on_upgrade(info, same_group):'''

assert OLD in t, "refimpl anchor 1 not found -- did the target change?"
t = t.replace(OLD, NEW, 1)

OLD2 = r'''                name = radarr_torrents[h].get('name', '')
                action = 'WOULD relabel' if dry_run else 'relabeling'
                log.info(f'  {action} superseded: "{name}" (movie {movie["id"]}: {movie.get("title")})')'''
NEW2 = r'''                name = radarr_torrents[h].get('name', '')
                if not should_supersede_candidate(radarr_torrents[h]):
                    log.info(f'  skip superseding "{name}" (tracker already unregistered it -- nothing left to seed)')
                    continue
                action = 'WOULD relabel' if dry_run else 'relabeling'
                log.info(f'  {action} superseded: "{name}" (movie {movie["id"]}: {movie.get("title")})')'''

assert OLD2 in t, "refimpl anchor 2 not found -- did the target change?"
t = t.replace(OLD2, NEW2, 1)

p.write_text(t)
print("refimpl applied")
