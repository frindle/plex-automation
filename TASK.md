# TASK: arr-codec-floor-s3-sort-keys

## Confirmed defect (observed, not suspected)

The keeper-dedup sort keys in `arr-webhook.py` rank candidates by custom-format
score first and never look at the video codec. Observed in the code directly:
Radarr's `_dupe_candidate_sort_key` returns `(-c['score'], -c['progress'], c['order'])`
and Sonarr's `_sonarr_dupe_candidate_sort_key` returns
`(-len(c['episodes']), -c['score'], -c['progress'], c['order'])`. So a lower-scoring
HEVC/x265 release loses to a higher-scoring AVC/x264 one, and the codec floor is
never enforced at keeper selection. The `codec_rank(name)` helper already exists in
this file (returns 2 for HEVC/x265, 1 for AVC/x264, 0 otherwise) but neither sort
key uses it.

## Entry point

arr-webhook.py:1802 (`_dupe_candidate_sort_key`) and arr-webhook.py:2012
(`_sonarr_dupe_candidate_sort_key`).

## Required change

Insert codec_rank as a sort key directly ABOVE score in BOTH keeper-dedup sort keys. Radarr `_dupe_candidate_sort_key` (line 1800): change `(-c['score'], -c['progress'], c['order'])` to `(-codec_rank(<candidate name>), -c['score'], -c['progress'], c['order'])`. Sonarr `_sonarr_dupe_candidate_sort_key` (lines 2004-2018): change `(-len(c['episodes']), -c['score'], -c['progress'], c['order'])` to `(-len(c['episodes']), -codec_rank(<candidate name>), -c['score'], -c['progress'], c['order'])` -- codec goes ABOVE score but BELOW coverage (episode count), preserving the existing coverage-first contract. Use whichever field on the candidate dict `c` holds the release name (add/pass it if the sort receives only score/progress -- keep the change minimal and reuse the existing candidate shape).

Both candidate shapes already carry the release name in `c['title']` (queue
candidates from Radarr's queue records / Sonarr's episode titles, and throttled-lane
candidates from Deluge torrent names) -- use that field; do not add a new key.

Behaviour that must NOT change:
- Radarr: within one codec tier the old contract holds exactly -- higher score first, then more progress (bytes pulled), then lower queue order; torrents with no queue record (`order == inf`) still sort last.
- Sonarr: coverage (episode count) remains the FIRST key -- a larger pack always beats a smaller grab regardless of codec or score.
- `codec_rank` itself is untouched and keeps tolerating `None`/empty names (rank 0).

## Must contain

- `-codec_rank(c['title'])` in `_dupe_candidate_sort_key`: `-codec_rank(c['title']), -c['score'], -c['progress'], c['order']`
- `-codec_rank(c['title'])` in `_sonarr_dupe_candidate_sort_key`, below coverage: `(-len(c['episodes']), -codec_rank(c['title']), -c['score'], -c['progress'], c['order'])`

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
