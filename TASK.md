# TASK: arr-webhook-recent-upgrade-priority-s2-priority-hashes-list-sen

## Confirmed defect (observed, not suspected)

In `prioritize_normal_torrents()` the list of hashes that gets sent to Deluge's
`core.queue_top` is a local variable named `top_hashes`, and it is computed
inline inside the function body. The queue-top selection logic is therefore
untestable in isolation: any caller or test that wants "the priority hashes for
this torrent map" has no function to call, only the full network-bound
scheduler pass. Verified by reading the current source: `top_hashes = [h for h,
i in torrents.items() if i.get('label', '') in priority_labels]` sits inside
`prioritize_normal_torrents()` and is referenced again in the log line; there is
no standalone selector anywhere in the module.

## Entry point

arr-webhook.py:3654 (`def prioritize_normal_torrents():`) — specifically the
inline `top_hashes` computation at ~line 3660 and its use in the
`core.queue_top` POST at ~line 3665.

## Required change

In arr-webhook.py, extract the queue-top selection into a small pure helper
named `_select_priority_hashes(torrents, priority_labels)` that returns the list
of hashes (in torrent-map order) whose `label` is in `priority_labels`, and make
`prioritize_normal_torrents()` build its `priority_hashes` list from that helper
and send exactly that `priority_hashes` list to `core.queue_top`.

Contract:
- `_select_priority_hashes(torrents, priority_labels)` returns a new list of
  hash strings in the same order as `torrents.items()`, containing only entries
  whose `i.get('label', '')` is a member of `priority_labels`. It must not raise
  on an empty map or on entries with no/empty label (those are simply excluded).
- `prioritize_normal_torrents()` keeps its exact existing behaviour: same log
  lines, same Deluge calls, same order. The only change is that the list sent to
  `core.queue_top` is now named `priority_hashes` and produced by the helper;
  it must still be wrapped as `'params': [priority_hashes]`, and the call must
  still be skipped when the list is empty (the existing `if priority_hashes:`
  guard). The `core.queue_bottom` half (`bottom_hashes`) is untouched.

Behaviour that must NOT change:
- Upgrade-labeled torrents still go to `core.queue_bottom` with their hashes in
  map order; no other Deluge method or param shape changes.
- When there are no sonarr/radarr torrents, NO `core.queue_top` request is made
  (and vice versa for the bottom half).
- The function remains exception-safe: any failure inside the try block is
  logged and swallowed, never raised to the scheduler thread.

## Must contain

- `_select_priority_hashes(torrents, priority_labels)`
- `priority_hashes = _select_priority_hashes(torrents, priority_labels)`
- `'params': [priority_hashes]`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

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
