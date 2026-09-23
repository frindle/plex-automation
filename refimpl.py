#!/usr/bin/env python3
"""Reference impl for: arr-webhook-yearly-upgrade-batches-s1-batch-constants

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

OLD = r"BULK_SEARCH_DELAY = int(os.environ.get('BULK_SEARCH_DELAY', '180'))  # secs between batches"
NEW = (r"BULK_SEARCH_DELAY = int(os.environ.get('BULK_SEARCH_DELAY', '180'))  # secs between batches" "\n"
       "# Yearly upgrade batches: how many movies/series to search per batched" "\n"
       "# pass, and the minimum whole days between passes for a given service." "\n"
       r"UPGRADE_BATCH_SIZE = int(os.environ.get('UPGRADE_BATCH_SIZE', '12'))" "\n"
       r"UPGRADE_BATCH_INTERVAL_DAYS = int(os.environ.get('UPGRADE_BATCH_INTERVAL_DAYS', '3'))")

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
