# Offload tier: shared design contract

This is the contract the offload parts are built against. Change it here first,
in its own commit, before code relies on a change.

Nothing in this repo may name a host, IP, provider, indexer or key. All of those
come from environment variables or a config file that is not committed.

## Goal

The library keeps every file at its path, size and mtime. A file that is
offloaded keeps its first 20% and its last 32 MB on local disk. The middle is
read from Usenet through AltMount's WebDAV. Plex is not told anything changed.

## Rules for what stays whole

The hot-set code implements these rules.

- **Never offloaded:**
  - the Adult section (`Cfg.never_sections`; Asian TV is offloadable);
  - files Radarr and Sonarr don't manage;
  - files still seeding (the glacier gate is not "free");
  - files with more than one hardlink;
  - files with no restore path;
  - any file whose bitrate is above `max_stream_kbps`, which is set to a quarter of the measured line speed (0 turns this off);
  - 4K, when `TIERING_KEEP_4K=1`. This is a blunt override that can be used before the line speed has been measured.
- **New import:** whole for `new_days` (30).
- **Played, synced, on deck or in progress:** whole until `idle_days` (60) after the last play.
- **TV:** a play or sync of any episode makes its whole season whole. If the episode is one of the last `near_end_eps` (3) of the season, the next season is made whole too. Each season stays whole until `idle_days` after the last play in it.
- **Synced:** any tierfs `remote_read` event, which is a read past the local head. A Plex download reads the whole file, so this covers sync.

## Layout on disk

These names apply to each library file system.

- `L` is the library path Plex sees. When the file is offloaded, `L` is a symlink to `${TIERFS_MOUNT}/<id><ext>`.
- The stub is `${stub_root}/<id><ext>`. It must be on the same file system as `L`, so that rename and link are atomic and never copy.
  - The stub keeps the original bytes in `[0, head_len)` and `[size - tail_len, size)`.
  - Everything else in the stub is a hole, made with `fallocate(PUNCH_HOLE | KEEP_SIZE)`.
  - The stub's mtime is the original mtime.
- `<id>` is 16 lowercase hex characters. It is random, assigned once, and never reused.
- `head_len` is `ceil(size * head_frac)`, where `head_frac` defaults to 0.20.
  - It is rounded up to a 1 MiB boundary.
  - `tail_len` is 32 MiB plus `size mod 1 MiB`, so the middle starts and ends on 1 MiB boundaries. Page-rounded tail reads then never reach into the middle.
  - When `head_len + tail_len >= size`, the file is never offloaded.

### Manifest

There is one manifest per offloaded file, at `${TIERFS_MANIFESTS}/<id>.json`. It is written atomically: write a temp file in the same directory, fsync it, then rename it into place.

```json
{"v": 1, "id": "0123456789abcdef", "ext": ".mkv", "size": 8370000000,
 "mtime_ns": 1711393377000000000, "head_len": 1674575872, "tail_len": 33820800,
 "stub_path": "/abs/path/to/stub/0123456789abcdef.mkv",
 "remote_path": "/path/inside/altmount/webdav/file.mkv",
 "sha256": "<whole original file>", "head_b3": "<blake3 of head>", "tail_b3": "<blake3 of tail>",
 "chunk_size": 4194304, "chunks_b3": "<blake3 of the chunk list file>"}
```

### Chunk list

Each offloaded file also has a chunk list at `${TIERFS_MANIFESTS}/<id>.chunks`.
- It covers the middle, `[head_len, size - tail_len)`, cut into `chunk_size` pieces (4 MiB). The last piece may be short.
- The file is the 32-byte BLAKE3 digests of the pieces, in order, with nothing else.
- It is written atomically, before the manifest. The manifest's `chunks_b3` is the BLAKE3 of this file, so a cut-short or damaged list is caught.
- The hashes come from the same read of the whole file that gives `sha256`.

The remote can answer a missing segment with zeros and a normal 206. The chunk list is how tierfs and promote tell good bytes from dead ones.

tierfs reads the remote at `${TIERFS_REMOTE_BASE}` + `remote_path`. The credentials come from `TIERFS_REMOTE_USER` and `TIERFS_REMOTE_PASS`.

## tierfs (Go, `tierfs/`)

- **Mount:** read-only FUSE with a flat directory holding one entry, `<id><ext>`, per valid manifest.
  - Mode is 0444 and nlink is 1.
  - Handles are opened with direct I/O, so reads arrive with their exact offset and size and tierfs does its own read-ahead.
  - Size and mtime come from the manifest.
- **Reads:** any read in the head or tail ranges comes from the stub with `pread`. Any read in the middle comes from the remote with HTTP Range requests. A read that crosses a boundary is split.
- **Verified middle:** tierfs never returns a remote byte it has not checked.
  - It fetches whole chunks and checks each one's BLAKE3 against the chunk list before any byte of it is returned.
  - On a mismatch it fetches the chunk again once with a new request. If that also fails, the read returns EIO and a `bad_chunk` event is written.
  - A manifest whose chunk list is missing or does not match `chunks_b3` is skipped, so its entry is not shown.
  - This is what stops a Plex stream or a mobile download from ever getting zero-filled data. A download that hits a bad chunk fails and Plex retries it later.
- **Remote stream:** each open handle has one sequential stream with a read-ahead buffer, `TIERFS_READAHEAD`, default 32 MiB.
  - Pre-warm: once a read reaches `0.75 * head_len`, start the stream at `head_len`.
  - A read outside the current stream window cancels the old request at once and starts a new one at that offset.
  - The number of open remote streams is capped by `TIERFS_MAX_STREAMS`, default 8.
- **Errors:** retry with backoff for up to 10 s, then return EIO. Never return zeros for bytes it could not fetch.
- **Manifest reload:** poll every 5 s and on SIGHUP.
  - When a manifest is removed, its entry disappears.
  - Handles that are already open keep working until they are closed.
- **Events:** JSON lines appended to `${TIERFS_EVENTS}`. The file rotates at 64 MiB and 3 old files are kept.
  - Format: `{"ts": <unix s float>, "id": "...", "ev": "open"|"remote_read"|"close"|"bad_chunk", "bytes_local": n, "bytes_remote": n}`. `bad_chunk` also carries `"offset": n`, the chunk's start in the file.
  - `remote_read` is written once per handle, on the first remote byte. `close` carries the totals.
- **Status:** `/healthz` and `/metrics` on `TIERFS_HTTP`, which listens on localhost only.

## Python (`tiering/offload/`)

| Module | Owner | Contract |
|---|---|---|
| `state.py` | file ops | SQLite at `${TIERING_DB}`. API below. |
| `ops.py` | file ops | `offload`, `promote`, `demote` on one file. Uses a `RemoteReader`. |
| `hotset.py` | rules | Pure functions, no I/O. |
| `plex_watch.py` | rules | Read-only Plex client and the tierfs event reader. |
| `altmount.py`, `nntp_stat.py`, `newznab.py` | intake | Clients. `altmount.WebDavReader` implements `RemoteReader`. |
| `qualify.py`, `weekly.py` | intake | The checks before an offload, the weekly check and the report. |
| `runner.py` | runner | The long-running loop that applies the hot set. See "Runner". |

Tests go in `tiering/offload/tests/test_<module>*.py` and run under pytest in CI (`.github/workflows/tiering.yml`).

### state.py API

```python
STATES = ("whole", "offloading", "offloaded", "promoting", "failed", "never")
open_db(path) -> sqlite3.Connection          # creates tables, WAL mode
get(db, id) -> dict | None
by_path(db, lib_path) -> dict | None
by_rating_key(db, rating_key) -> list[dict]
upsert(db, row: dict) -> None                # keyed on id
set_state(db, id, state, **fields) -> None   # also sets updated_ts
list_files(db, state=None) -> list[dict]
log_event(db, id, kind, detail: dict) -> None
```

The `files` table has these columns:
- identity and placement: `id`, `lib_path`, `stub_path`, `remote_path`;
- file facts: `size`, `mtime_ns`, `sha256`, `head_len`, `tail_len`, `head_b3`, `tail_b3`;
- sources: `nzb_path`, `nzb_path2`;
- Plex facts: `rating_key`, `kind` (movie or episode), `is4k`;
- state: `state`, `step` (the last finished step of an offload or promote, so a crash can resume), `restore_score`, `restore_note`;
- timestamps: `last_check_ts`, `last_ok_ts`, `created_ts`, `updated_ts`.

### RemoteReader

```python
class RemoteReader(Protocol):
    def read_range(self, remote_path: str, offset: int, length: int) -> bytes: ...
```

### hotset.py API

```python
@dataclass(frozen=True)
class Cfg: new_days=30; idle_days=60; near_end_eps=3; keep_4k=False; max_stream_kbps=0; never_sections=("Adult",)
@dataclass(frozen=True)
class Action: kind: str  # "promote" | "demote"
              rating_key: str; reason: str; priority: int  # 0 = now (live session)
desired_whole(items, plays, sessions, on_deck, synced_rks, now, cfg) -> dict[str, tuple[str, float]]
plan_actions(desired, offloaded_rks, whole_rks, now) -> list[Action]
```

The inputs are plain dicts shaped like Plex's JSON: `ratingKey`, `grandparentRatingKey`, `parentIndex`, `index`, `addedAt` and `viewedAt`.

### Offload order (crash-safe, `L` is never missing)

1. Check the preconditions:
   - `qualify` passed, the gate says "free", nlink is 1, and the hot set does not want the file;
   - `TIERING_APPLY=1`, otherwise this is a dry run.
2. Hardlink `L` to the stub path.
3. Write the chunk list, then the manifest. The stub is still whole.
4. Create the symlink at `L.tierfs-tmp`, then `rename` it over `L`. This is atomic.
5. Wait until no process holds the old inode open. Check `/proc/*/fd`, with a timeout.
6. Punch holes in the stub, restore its mtime, and check head and tail against `head_b3` and `tail_b3`.
7. Set `state=offloaded`.

### Promote order

1. Read the middle through `RemoteReader` one chunk at a time. Check each chunk against the chunk list before it is written into the stub's holes; a mismatch stops the promote at once with `state=failed`, as in step 2. fsync.
2. Hash the whole stub with SHA-256. If it does not match the manifest, stop: set `state=failed`, keep the symlink, and alert. Nothing is swapped.
3. Restore the stub's mtime.
4. Link the stub to `L.tierfs-tmp`, then `rename` it over `L`.
5. Unlink the stub path, remove the manifest and the chunk list, and set `state=whole`.

## Runner (`runner.py`)

Swaps are automatic. One long-running process applies the hot set.

- **Each pass (every 60 s):**
  - read Plex sessions, play history back to `now - idle_days`, on deck, in progress and the tierfs events;
  - compute `desired_whole`, then `plan_actions`.
- **Promotes run at once:**
  - a live session first, then the rest of its season in episode order, then everything else;
  - at most `TIERING_PROMOTE_CONCURRENCY` (default 2) at a time.
- **Demotes run only at night:**
  - only inside `TIERING_DEMOTE_WINDOW` (default `02:00-06:00`, local time);
  - only files that pass `qualify` that night, with the glacier gate "free" and nlink 1;
  - capped by `TIERING_DEMOTE_GB_PER_NIGHT` (default 200);
  - no demotes on a night when any promote is queued or any hash check has failed.
- **A `bad_chunk` event:** promoting cannot help, because the remote copy is bad. Set `state=failed`, alert, and list it in the weekly report with its restore path. The file stays a working symlink for the parts that are good.
- **Reconcile each pass:** if `L` is no longer the symlink a row expects (an arr upgrade or delete replaced it), stop managing that row. Set `state=failed` with `restore_note="L replaced"`. Leave the stub and the manifest for the weekly report; nothing is deleted.
- **Config from env:**
  - `TIERING_MAX_STREAM_KBPS` gives `Cfg.max_stream_kbps`; `TIERING_KEEP_4K` gives `Cfg.keep_4k`;
  - `TIERING_NEVER_SECTIONS` is a comma list (default `Adult`).
- **Safety:**
  - dry run unless `TIERING_APPLY=1`, in which case it logs each planned action and changes nothing;
  - one instance at a time (a lock file next to `TIERING_DB`);
  - a heartbeat plus `/healthz` on localhost.

## Safety

- Dry run is the default everywhere. Nothing changes a file unless `TIERING_APPLY=1` is set.
- No code path deletes media. The only unlinks are the stub's extra link, made in step 2 of an offload, and a temp symlink.
- The tests run in GitHub Actions or the sandbox, never against the live library.
- No unverified remote byte is ever served or written: tierfs and promote both check every chunk against the chunk list.
