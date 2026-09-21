#!/usr/bin/env python3
"""Reference impl for: arr-codec-floor-s3-sort-keys

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

# Radarr: codec_rank goes directly ABOVE score in the sort key. Both candidate
# shapes (queue and throttled-lane) already carry the release name in 'title'.
OLD_RADARR = "    return (-c['score'], -c['progress'], c['order'])"
NEW_RADARR = "    return (-codec_rank(c['title']), -c['score'], -c['progress'], c['order'])"

# Sonarr: codec goes ABOVE score but BELOW coverage (episode count), preserving
# the existing coverage-first contract.
OLD_SONARR = "    return (-len(c['episodes']), -c['score'], -c['progress'], c['order'])"
NEW_SONARR = ("    return (-len(c['episodes']), -codec_rank(c['title']), "
              "-c['score'], -c['progress'], c['order'])")

assert OLD_RADARR in t, "refimpl anchor not found (Radarr) -- did the target change?"
t = t.replace(OLD_RADARR, NEW_RADARR, 1)
assert OLD_SONARR in t, "refimpl anchor not found (Sonarr) -- did the target change?"
t = t.replace(OLD_SONARR, NEW_SONARR, 1)

p.write_text(t)
print("refimpl applied")
