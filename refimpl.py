#!/usr/bin/env python3
"""Reference impl for: arr-codec-floor-s2-supersede-floor

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Inserts the one-directional codec floor guard into handle_upgrade_import's
supersede sweep: before superseding OR hard-deleting a matched existing
torrent, skip it when its codec outranks the imported release.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = """        if torrent_matches_any_title(name, title_variants) and search_term.lower() in name.lower():
            # A repack/proper is only a true immediate replacement (safe to"""
NEW = """        if torrent_matches_any_title(name, title_variants) and search_term.lower() in name.lower():
            # Codec floor: never supersede or hard-delete an existing release
            # that outranks the imported one (e.g. keep x265/HEVC when the new
            # import is x264). One-directional -- same-codec and genuine
            # upgrades fall through untouched.
            if codec_rank(name) > codec_rank(new_filename):
                log.info(f'{source}: codec floor -- keeping {torrent_hash} - {name} '
                         f'(existing rank {codec_rank(name)} outranks import "{new_filename}" rank {codec_rank(new_filename)})')
                record_activity('supersede-skipped', f'{source}: kept "{name}" (codec floor: existing release outranks imported "{new_filename}")')
                continue
            # A repack/proper is only a true immediate replacement (safe to"""

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
