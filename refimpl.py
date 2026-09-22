#!/usr/bin/env python3
"""Reference impl for: arr-codec-floor-s4-pack-vs-pack

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Adds the same-season pack-vs-pack pass to dedup_via_sonarr plus its pure
decision helper; preserves everything already in arr-webhook.py.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

HELPER_ANCHOR = "def dedup_via_sonarr(dry_run=False):"
assert HELPER_ANCHOR in t, "refimpl anchor not found -- did the target change?"

HELPER = '''def select_same_season_pack_losers(pack_hashes, torrent_names, keeper_ids=None):
    """Given the season-PACK hashes of ONE (series_id, season) group from
    same_season_packs, {hash: release_name}, and the current-keeper downloadId
    set (lowercased), return the loser pack(s) to supersede.

    The highest codec_rank wins; a rank tie keeps every pack unless exactly one
    member is itself a current keeper (in `keeper_ids`), which then beats the
    rest -- the same coverage/score/history tie-break philosophy as the other
    passes: never guess when no signal separates them. A single-pack group
    returns []. Case-insensitive on hashes; never raises on None inputs."""
    names = torrent_names or {}
    keepers = {(d or '').lower() for d in (keeper_ids or set())}
    if len(pack_hashes) <= 1:
        return []
    keeper_in_group = [h for h in pack_hashes if h.lower() in keepers]
    if len(keeper_in_group) == 1:
        return [h for h in pack_hashes if h != keeper_in_group[0]]
    best_rank = max(codec_rank(names.get(h) or '') for h in pack_hashes)
    top = [h for h in pack_hashes if codec_rank(names.get(h) or '') == best_rank]
    if len(top) > 1:
        return []  # rank tie with no keeper signal -> keep all, never guess
    return [h for h in pack_hashes if h != top[0]]


'''

t = t.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)

PASS_ANCHOR = """            # Skip these packs in the per-episode grouping below (they carry no
            # SxxExx token anyway, but keep the skip set explicit)."""
assert PASS_ANCHOR in t, "refimpl pass anchor not found -- did the target change?"

PASS = '''            # Same-season pack-vs-pack: two season packs for the SAME series+season
            # both seeding (e.g. an H.265 re-rip next to the AVC original) leaves
            # the lower-codec one seeding forever -- neither pass above compares a
            # pack against another pack. Group by (series_id, season); different
            # seasons are different keys and never compare against each other.
            same_season_packs = {}
            for h in matched_hashes:
                name = sonarr_torrents[h].get('name', '') or ''
                sm = SEASON_RE.search(name)
                if not sm or EPISODE_RE.search(name):
                    continue  # only season packs (no SxxExx token) join a group
                same_season_packs.setdefault((series_id, int(sm.group(1))), []).append(h)
            for (sid, season), pack_hashes in same_season_packs.items():
                if len(pack_hashes) <= 1:
                    continue  # nothing to compare within this series+season
                losers = select_same_season_pack_losers(pack_hashes, matched_names, keeper_ids)
                for h in losers:
                    name = sonarr_torrents[h].get('name', '') or ''
                    action = 'WOULD relabel' if dry_run else 'relabeling'
                    log.info(f'  {action} superseded (lower-codec pack, same season): "{name}" (series {sid}: {series.get("title")}, S{season:02d})')
                    report.append({
                        'series_id': sid,
                        'series': series.get('title'),
                        'season': season,
                        'pack_hash': h,
                        'pack_name': name,
                    })
                    if not dry_run:
                        supersede_torrent(h)
                    relabeled += 1
            # Skip these packs in the per-episode grouping below (they carry no
            # SxxExx token anyway, but keep the skip set explicit).'''

t = t.replace(PASS_ANCHOR, PASS, 1)
p.write_text(t)
print("refimpl applied")
