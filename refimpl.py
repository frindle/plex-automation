#!/usr/bin/env python3
"""Reference impl for: arr-new-grab-queue-top-r2

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

OLD = """    if not upgrade:
        log.info(f"{source}: grab {download_id} is a new release, not throttling")
        return"""

NEW = """    if not upgrade:
        # A new (non-upgrade) grab must start immediately: move it to the TOP
        # of the Deluge queue so it sits ahead of any throttled upgrades.
        log.info(f"{source}: grab {download_id} is a new release, moving to top of queue")
        try:
            deluge_login()
            session.post(
                f'{DELUGE_URL}/json',
                json={'method': 'core.queue_top', 'params': [download_id], 'id': 92},
                timeout=10,
            )
            log.info(f"{source}: moved {download_id} to top of queue")
        except Exception as e:
            log.error(f"{source}: failed to move new grab to top of queue: {e}")
        return"""

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
