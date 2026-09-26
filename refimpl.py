#!/usr/bin/env python3
"""Reference impl for: arr-webhook-recent-upgrade-priority-s4-hourly-sweep-keeps-recent-on-top

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

OLD = r"        priority_labels = {'sonarr', 'radarr'}"
NEW = r"        priority_labels = {'sonarr', 'radarr', SONARR_UPG_PRIORITY_LABEL, RADARR_UPG_PRIORITY_LABEL}"

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
