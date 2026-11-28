# Offload tier end-to-end scenarios

These are wired up once tierfs, state/ops, intake/qualify and the runner are
merged. `test_mount_propagation.sh` already covers the Plex and mount plumbing
with a bindfs stand-in.

## Common setup

- Library from `make_media.sh` (2 movies, 1 show, 2 seasons of 4 episodes), real
  files, deterministic sha256s in `SHA256SUMS`.
- `fake_webdav.py` serves a copy of the library bytes as the remote, with Basic
  auth. A fake NNTP answers STAT for every message id.
- tierfs mounted on the host at `${TIERFS_MOUNT}` after Plex starts (`:rslave`).
- Plex is scanned and every part id, rating key and size is recorded.
- Runner env: `TIERING_APPLY=1 TIERING_NEW_DAYS=0 TIERING_DEMOTE_WINDOW=00:00-24:00
  TIERING_PASS_SECONDS=2`. Scenario 9 leaves `TIERING_APPLY` unset.
- "Not re-analysed" means: part id, media id and rating key unchanged, and no new
  analysis lines for the file in the Plex log after the swap.

## 1. Offload

1. Qualify the target file against the fake WebDAV (exact bytes) and fake NNTP.
2. Start the runner. It demotes inside the always-open window.
3. Assert:
   - `L` is a symlink into `${TIERFS_MOUNT}/<id><ext>`;
   - `du -k` of the stub is about 20% of the file size (head plus 32 MiB tail);
   - `stat -L` on `L` still gives the original size and mtime;
   - the Plex part id is unchanged after a rescan;
   - Plex does not re-analyse the file;
   - the row is `state=offloaded`.

## 2. Streaming

1. Offload a movie.
2. Through Plex, Range read the head, the middle and the tail.
3. Assert each range sha256 equals the same range of the original, and tierfs
   logged `remote_read` for the middle range only.

## 3. Mobile sync

1. Offload all of a season.
2. Full Plex download of one episode. Assert its sha256 equals the original.
3. Assert the `remote_read` event makes the runner promote the whole season, in
   episode order (check the order of the state changes in the DB).
4. Assert every `L` in the season is a regular file with the original sha256
   and mtime, the rows are `state=whole`, and no stub or manifest remains.

## 4. Dead data

1. Offload a movie. Set a fake WebDAV zero range, for example one 4 MiB chunk
   in the middle, answered with HTTP 206.
2. Full Plex download. Assert it fails and no read of that range ever returns
   zero bytes (compare every byte the client did receive).
3. Assert a `bad_chunk` event with the chunk offset, the row is `state=failed`,
   an alert is raised, and `L` is still the symlink.
4. Assert no promote swap happened (the stub is unchanged, `L` is not replaced).

## 5. Season rule

1. Offload all of S01 and S02.
2. `plexctl play` S01E02. Assert all of S01 becomes whole, S02 stays offloaded.
3. `plexctl play` S01E03 (one of the last 3 of the season). Assert S02 becomes
   whole as well.

## 6. Clock

1. Start with a whole season that was played.
2. Set `TIERING_IDLE_DAYS=0.0005` (about 43 s). Assert the season demotes again
   after that time with no play.
3. Play an episode just before the timer ends. Assert the season stays whole
   and the timer restarts from the new play (it demotes about 43 s after the
   last play, not the first).

## 7. Upgrade race

1. Start an offload of a file. While it runs (after the hardlink to the stub,
   before the swap), replace `L` with a new file, as an arr upgrade would.
2. Assert the new file is intact (sha256 unchanged), it is not a symlink, the
   row is `state=failed` with `restore_note="L replaced"`, and the stub and
   manifest are left in place.

## 8. Crash

1. Kill the runner (SIGKILL) mid-offload, and again mid-promote, at each step
   boundary that the test can hit.
2. After each kill assert `L` resolves (readable, right size) and its sha256
   through the path equals the original.
3. Restart the runner. Assert the run resumes from the recorded `step` and ends
   in `offloaded` or `whole`, with no leftover `L.tierfs-tmp`.

## 9. Dry run

1. Run the runner without `TIERING_APPLY`.
2. Assert it logs planned actions and the library tree (paths, types, sizes,
   mtimes, inodes), the stub dir, the manifests and the DB rows are identical
   before and after.

## 10. Concurrency

1. Offload 4 files.
2. Start 4 parallel Plex downloads (or Range streams), one per file.
3. Assert each sha256 equals its original, the number of open remote streams
   never exceeds `TIERFS_MAX_STREAMS`, and tierfs `/healthz` stays OK.

## 11. Plex's own reads (test_plex_tierfs.py)

1. A real Plex scans two whole movies; the runner offloads them.
2. A forced rescan, then preview thumbnails (`GenerateBIFBehavior=asap` and an
   analyze), read through the remote: same parts, no `want`, still offloaded.
3. Plex's marker (`-f flac` to a file) and loudness (`-f null -`) command lines,
   run by a binary named `Plex Transcoder`: same.
4. A download through Plex, and a `Plex Transcoder` writing any other muxer, wait
   for the promote and get every byte; the other file stays offloaded and Plex
   keeps the same part.

## 12. Restub

1. Offload a movie, then delete its stub, and in a second run also `L`, to
   stand in for a lost disk. Keep the DB and manifest.
2. `python -m offload.runner restub` (dry run): assert it lists the file and the
   tree is unchanged.
3. Run with `--apply`. Assert the fake WebDAV saw ranged reads of only the head
   and the tail, the stub is sparse with the original mtime, `L` is the tierfs
   symlink again, the sha256 through `L` equals the original and the row is
   still `offloaded`.
4. Delete the manifest too: assert the file is reported and nothing is written.
5. Make the tail unreadable on the fake remote: assert `state=failed`, a
   `restub_unreadable` alert, no stub and no `L` left half-written.

## Shared assertions after every scenario

- No `L` is missing or dangling.
- No file under the library has changed content (sha256 against `SHA256SUMS`,
  read through the path).
- No zero-filled range was ever served.
