#!/usr/bin/env python3
"""Reference impl for: arr-codec-floor-s1-codec-rank

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'arr-webhook.py'
t = p.read_text()

OLD = "SEASON_RE = re.compile(r'[Ss](\\d{1,2})(?:[Ee]\\d{1,3})?')"
NEW = OLD + '''

# Codec floor (slice 1): rank a release name by its video codec so later slices
# can refuse to replace a better-encoded file with a worse one. HEVC/x265/h265/
# h.265 -> 2, AVC/x264/h264/h.264 -> 1, anything else (unknown) -> 0.
def codec_rank(name):
    """Map a release name to an integer codec rank; pure, no I/O."""
    n = str(name).lower() if name is not None else ''
    if 'hevc' in n or 'x265' in n or 'h265' in n or 'h.265' in n:
        return 2
    if 'avc' in n or 'x264' in n or 'h264' in n or 'h.264' in n:
        return 1
    return 0'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
