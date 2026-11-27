# Offload tier: shared design contract

This is the contract the offload parts are built against. Change it here first,
in its own commit, before code relies on a change.

Nothing in this repo may name a host, IP, provider, indexer or key. All of those
come from environment variables or a config file that is not committed.

## Goal

The library keeps every file at its path, size and mtime. A file that is
offloaded keeps its first 20 minutes of play (at least 20% of the file) and its last 32 MB on local disk; a file under
30 minutes of play is never offloaded. The middle is
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
- **Synced:** any tierfs `want` event, which is a player read past the local head (see tierfs, **Waiting reads**). A Plex download reads the whole file, so this covers sync. A background reader's `remote_read` does not count.
- **Read while whole:** a download or sync of a file that is still whole never touches tierfs, so it leaves no event. The runner notes each whole file's access time (`kv['local_atimes']`, checked every 10 minutes). A later access time is a read and restarts the idle clock like a play or sync. The first sight of a file only notes its time, and the runner's own reads (qualify, promote) are noted again when they end, so they never count. This needs a file system that updates access times; with `relatime` that is at most once a day per file, which is enough for a 60 day clock. Any other reader of a whole file (a Plex analysis task, say) counts too, which only keeps the file whole longer.

## Layout on disk

These names apply to each library file system.

- `L` is the library path Plex sees. When the file is offloaded, `L` is a symlink to `${TIERFS_MOUNT}/<id><ext>`.
- The stub is `${stub_root}/<id><ext>`. It must be on the same file system as `L`, so that rename and link are atomic and never copy.
  - The stub keeps the original bytes in `[0, head_len)` and `[size - tail_len, size)`.
  - Everything else in the stub is a hole, made with `fallocate(PUNCH_HOLE | KEEP_SIZE)`.
  - The stub's mtime is the original mtime.
- **Real file system only:** every file operation (link, `renameat2(RENAME_EXCHANGE)`, hole punch, nlink and inode checks) runs on the disk's own mount, never through a union or user-share FUSE layer. Such a layer can report nlink 1 for a hardlinked file and may not support `RENAME_EXCHANGE`. `TIERING_PATH_MAP` maps the share path Plex and the arrs report to the disk path that holds the file. A file that can't be mapped to exactly one disk path is never offloaded.
  - **Path namespaces:**
    - `lib_path` in the database is always the share path, the one Plex reports. It is never a disk path, so it stays valid when a whole file moves between disks.
    - Every file operation resolves `lib_path` to its disk path at that moment.
      - `TIERING_PATH_MAP` holds one `share_prefix=disk_prefix` entry per disk.
      - Only candidates that exist count. Zero or several means skip and log.
    - `ops` resolves the path itself, on both demote and promote, and refuses unless there is exactly one disk path. It never writes a disk path into the database. The manifest's `lib_path` may hold the disk path.
    - `register` stores the arr's path, mapped through `TIERING_ARR_PATH_MAP` (`arr_prefix=share_prefix`, optional, identity when unset) when the arrs see different paths from Plex. Its stat (size and mtime) goes to exactly one existing disk candidate from `paths.disk_paths(share, TIERING_PATH_MAP)`, or to the share path itself when `TIERING_PATH_MAP` is unset. Zero candidates count as `on_pool` when the file is on a pool (cache) instead, a new file the mover has not put on the array yet (`TIERING_POOLS` names the pools; unset, every `/mnt/<x>` that is not a disk, a user share mount, `disks`, `remotes`, `addons` or `rootshare`), and as `missing_local` otherwise. Several count as `ambiguous`. The file is skipped, and the result's `listed`, `missing_local`, `on_pool` and `ambiguous` counts let the caller see trouble. An `on_pool` file is registered the night after the mover moves it. An arr path that is under a `TIERING_PATH_MAP` disk prefix is rewritten to its share path with the reverse of that map (`/mnt/disk1/movies/m.mkv` becomes `/mnt/user/movies/m.mkv`) and counted as `disk_path_mapped`. A disk path no entry covers (an unraid disk or cache mount, or a share equal to its disk) is skipped and counted as `disk_path`. `lib_path` never holds a disk path: only array paths are stored. `/mnt/user0/...` is rewritten to `/mnt/user/...`; any other `/mnt/<x>/...` that is not `/mnt/user` and is not covered by a map entry (a named pool such as `/mnt/nvme`, `/mnt/disks`, `/mnt/remotes`) is skipped and counted as `disk_path`, also when `TIERING_PATH_MAP` is unset. Repeated slashes are collapsed to one before the path is stored (`..` is not resolved), so `/mnt/user/media//m.mkv` is the same row as `/mnt/user/media/m.mkv`.
    - The runner:
      - matches Plex files to rows by the share path;
      - in reconcile, reads the link at the resolved disk path;
      - passes the disk path to the glacier gate, which maps it into Deluge's namespace with `TIERING_GATE_PATH_MAP`.
  - The stub root is per disk (`${stub_root}` on the same disk as `L`). `TIERFS_STUB_ROOTS` lists them (comma list). tierfs skips a manifest unless `stub_path` is exactly `<one of those roots>/<id><ext>`, and opens it with `O_NOFOLLOW`, then requires a regular file of the manifest's size.
  - Anything else that moves library files between disks (for example a glacier mover) must skip a symlink that points into `${TIERFS_MOUNT}`.
- `<id>` is 16 lowercase hex characters. It is random, assigned once, and never reused.
- `head_len` is `ceil(size * head_frac)`. The runner sets `head_frac` per file so the head holds at least `TIERING_HEAD_MIN_S` (default 1200, 20 minutes) of play: `head_frac = max(0.20, TIERING_HEAD_MIN_S * 1000 / duration_ms)`, with the play time from Plex (the part's `duration`, else the media's, else the item's), else `play_ms`, the play time Radarr or Sonarr measured from the file (`mediaInfo.runTime`). A show's listed slot length is never used: it runs longer than the file and would shrink the head.
  - A file neither gives a play time for is not offloaded ("no play time from Plex or the arr").
  - A file under `TIERING_MIN_PLAY_S` (default 1800, 30 minutes) of play is not offloaded ("under 30 minutes of play"): a half-hour show stays whole.
  - A file whose `head_frac` would pass 0.80 is not offloaded ("too short for a 20-minute head"): it would save too little.
  - Examples: a 2-hour film keeps 20%, a 45-minute episode about 44%; a 22- or 29-minute episode stays whole.
  - The layout is chosen only on a fresh demote. A resumed demote keeps the `head_len` in its manifest.
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
- **Reads:** any read in the head or tail ranges comes from the stub with `pread`. A read in the middle goes one of two ways, depending on the reader (see **Waiting reads**). A read that crosses a boundary is split.
- **Verified middle:** tierfs never returns a remote byte it has not checked.
  - It fetches whole chunks and checks each one's BLAKE3 against the chunk list before any byte of it is returned.
  - On a mismatch it fetches the chunk again once with a new request. If that also fails, the read returns EIO and a `bad_chunk` event is written.
  - A manifest whose chunk list is missing or does not match `chunks_b3` is skipped, so its entry is not shown.
  - This is what stops a reader from ever getting zero-filled data.
- **Waiting reads (playback and sync never fail on a slow remote):** a play or a sync makes the file whole anyway, so a Plex player never streams the middle from the remote. Its reads wait for the promote, which fetches each chunk only once.
  - **Reader class:** each FUSE request carries the caller's pid.
    - tierfs reads `/proc/<pid>/comm` and the parent's `comm`, cached per pid. `comm` is cut to 15 characters.
    - The reader is *background* when either name is in `TIERFS_BACKGROUND_COMMS` (comma list, default `Plex Media Scan,watch-align`). That covers the scan, match, analysis, deep analysis, chapter thumbnails, credits and voice-activity scanner jobs, and the webhook's audio matching before an upgrade (`watch-align`, ffmpeg under another name).
    - A `Plex Transcoder` started by the server is also *background* when its command line is one of Plex's analysis jobs: loudness (`-f null -`), intro and credits marker audio (written under `Transcode/Detection/`) or voice activity (`track-NNNN.wav`). One that names any other muxer is a client stream or a sync, so a *player*.
    - A reader whose name is in `TIERFS_STREAM_COMMS` (comma list, default `Plex Media Server,Plex Transcoder,Plex DLNA Server`) and is not background is a *stream*: Plex serving a client.
    - Every other reader, including a pid or command line it cannot read, is a *player*.
    - tierfs runs with `pid: host`, so it can see the pids of Plex processes in other containers.
  - **Background reads** use the remote stream below, unchanged, and write `remote_read` as before.
  - **Player reads** of the middle come only from the stub, and only for chunks listed in the fill file `${TIERFS_MANIFESTS}/<id>.fill` (see **Promote order**). A read whose chunks are not listed yet waits:
    - On the first wait of a handle, tierfs writes a `want` event with `"offset"` set to the chunk start. It writes another whenever the wanted offset falls outside the 64 MiB after the last `want` of that handle.
    - Pre-warm: a *stream* writes a `want` at `head_len` on the first read of a handle, so the promote starts when play starts and has the whole head (20 minutes or more) to get ahead of the player. Any other player writes it once a read reaches `0.75 * head_len`.
    - The read is answered as soon as the fill file lists its chunks. tierfs watches the file, polling at least every 200 ms.
    - A `failed <offset>` line in the fill file returns EIO at once.
    - Otherwise, EIO only after `TIERFS_WAIT_S` (default 900) with no new line in the fill file. There is no limit while the fill file keeps growing.
    - When the caller goes away and the request is interrupted, the wait stops.
  - Each handle keeps its stub fd open. When the manifest is removed after a finished promote, open handles treat every chunk as local, because the stub inode is now the whole file.
  - Player reads never return remote bytes, so the 10 s remote error limit does not apply to them.
- **Remote stream (background readers only):** each open handle has one sequential stream with a read-ahead buffer, `TIERFS_READAHEAD`, default 32 MiB.
  - Pre-warm: once a read reaches `0.75 * head_len`, start the stream at `head_len`.
  - A read outside the current stream window cancels the old request at once and starts a new one at that offset.
  - The number of open remote streams is capped by `TIERFS_MAX_STREAMS`, default 16.
- **Errors (background readers):** retry with backoff for up to 10 s, then return EIO. Never return zeros for bytes it could not fetch.
- **Manifest reload:** poll every 5 s and on SIGHUP.
  - When a manifest is removed, its entry disappears.
  - Handles that are already open keep working until they are closed.
- **Events:** JSON lines appended to `${TIERFS_EVENTS}`. The file rotates at 64 MiB and 3 old files are kept.
  - Format: `{"ts": <unix s float>, "id": "...", "ev": "open"|"remote_read"|"want"|"close"|"bad_chunk", "bytes_local": n, "bytes_remote": n}`. `bad_chunk` and `want` also carry `"offset": n`, the chunk's start in the file.
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
| `register.py` | intake | Creates `whole` rows from Radarr and Sonarr. See "Register". |
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
update_if(db, id, expect_state, **fields) -> bool  # one UPDATE ... WHERE id=? AND state=?; fields may include
                                             # state; sets updated_ts; False (no change) if the row is gone or
                                             # its state moved on. Long jobs (weekly) write through this only.
list_files(db, state=None) -> list[dict]
log_event(db, id, kind, detail: dict) -> None
```

The `files` table has these columns:
- identity and placement: `id`, `lib_path`, `stub_path`, `remote_path`;
- file facts: `size`, `mtime_ns`, `sha256`, `head_len`, `tail_len`, `head_b3`, `tail_b3`;
- sources:
  - `nzb_path`: the archived NZB of the byte-identical release that AltMount streams. An offload needs it.
  - `nzb_path2`: the archived NZB of the restore-path release, which is a different release at or above the quality floor. It may be empty when the restore path is only known from a search.
  - `twin_nzb_path`, `twin_remote_path`: the twin, a second upload of the very same bytes (see Qualify), and where AltMount serves it. A promote reads it when the first copy fails. Both are empty when there is none.
- media identity, filled from the arr when the row is created and used by qualify and weekly to search: `title`, `year`, `imdb_id`, `tvdb_id`, `season`, `episode`, `release_name`;
- `play_ms`: the play time the arr measured from the file (`mediaInfo.runTime`), used for the head when Plex has none. Empty until the arr has scanned the file;
- Plex facts: `rating_key`, `kind` (movie or episode), `is4k`;
- state: `state`, `step` (the last finished step of an offload or promote, so a crash can resume), `restore_score`, `restore_note`;
- timestamps:
  - `last_check_ts`: the last weekly STAT check, whatever its result;
  - `last_stat_ok_ts`: the last weekly STAT check that found every segment;
  - `last_ok_ts`: the last full read whose hash matched;
  - `created_ts`, `updated_ts`.

### Qualify

`qualify` decides whether one whole file may be offloaded tonight.
- **Same bytes:** it hashes `L` itself with a fresh full read and hashes the remote copy, and both must equal. It never trusts a stored `sha256`. It writes the hash it saw into the row's `sha256`.
- **Restore path:** another release at or above the quality floor that is a different upload. Any candidate whose NZB shares message-ids with the `nzb_path` NZB is the same upload, whatever its name. Size alone never decides.
  - Message-ids are compared without `<>` or white space, and case-sensitive.
  - The candidate's NZB is STATed, every segment on every configured provider, like `nzb_path`'s, before it counts. A candidate with missing articles is not a restore path.
  - The first candidate that is disjoint and live is archived as `nzb_path2`. `QualifyResult.fields` always carries `nzb_path2`, `None` when there is no such candidate, so a stale one is cleared.
  - qualify searches once and reuses that search for the exact NZB and for the restore path.
- **Twin (best effort, never a fail):** last, after the restore path passed (so it only spends the grabs the required steps left), qualify looks for a second upload of the same file among the listings with the exact release name: no message-id in common with `nzb_path`, every article on every provider, and once imported into AltMount (its own job, so its own folder) the local file's size and the local file's bytes at 5 places of 1 MiB, both ends among them. At most 3 NZB grabs (`TWIN_GRABS`) go to it, and the listing our own NZB was grabbed from this time is not grabbed again. The first that passes is archived as `twin_nzb_path` with its AltMount path in `twin_remote_path`; `QualifyResult.fields` always carries both, `None` when there is none. A file without a twin still qualifies. A twin may also be the restore path.
- **Quality floor:** taken from the source's resolution. When the source resolution is unknown, the floor is 1080p.
- **Exact NZB: unsure is not a miss.** `no_exact_nzb` means a complete search (every configured indexer answered) found no release with the exact name. Anything less returns `nzb_unsure`, meaning "try later": a partial search (an indexer dropped, out of budget or failing), a spent grab budget, or `MAX_FAILED_GRABS` (3) failed grabs. A listing known to have no NZB is skipped without a grab and never counts as a failed grab, so the loop goes on to the next listing. Neither passes; only `nzb_unsure` is worth a retry tomorrow. A search that finds every indexer dropped, out of budget or rate limited is `nzb_unsure` (never `nzb_error`), also in the restore step.
- **"Try later" is not a permanent state.** When every candidate of a row failed at item level (the indexer says the item is gone, or the listing is known to have no NZB), the row's own fault, that is counted per release name in the client's state file. One body that is no NZB, not yet confirmed on a second night, is not the row's fault: it keeps the streak where it is, so an indexer serving a maintenance page never charges the rows it saw first. On the third night in a row `qualify` returns `no_exact_nzb` (step info `why=grab_failed`). Unsure caused by a spent budget, a dropped indexer or a transport error never counts, and keeps the streak where it is. `oldest_first(rows, indexers)` orders rows by the time of their last attempt (never attempted first), so a spent budget cannot starve the same rows night after night. `qualify` records the attempt on every path (a reused NZB, a failure after the NZB step, any error), not only when it searches.
- **Restore step: a confirmed "no NZB" is a "no".** An alternative the indexer says is gone (code 300, a 404 or 410), or that is known to have no NZB (its strike confirmed on a second night), counts as dead (`no_restore_path` when nothing else is live). One body that is no NZB is only a strike (it may be a maintenance page), so it makes the check unsure, never dead. A real inability to check (indexers unavailable, grab budget spent, a transport error, `MAX_FAILED_GRABS` reached) is also `restore_check_unsure`.
- **Unsure is a fail:** an indexer or provider that did not answer, or a partial search, never passes.
- `QualifyResult.fields` holds only `files` columns.

Offload step 3 hashes `L` again in the read that builds the chunk list. If that hash differs from the row's `sha256`, the offload stops and nothing is swapped.

### Register

- `register(db, apply)` runs once a night before the demote window, and the runner calls it.
- It reads every movie file and episode file from Radarr and Sonarr through their APIs (`RADARR_URL`, `SONARR_URL`, keys from env; more than one Sonarr is a comma list).
- For each file it upserts a row by `lib_path`. A new row gets a fresh `<id>`, `state=whole`, `size`, `mtime_ns` and the media identity: `title`, `year`, `imdb_id`, `tvdb_id`, `season`, `episode`, and `release_name` (the arr's scene name, else the file name), and `play_ms` from the arr's `mediaInfo.runTime`. A later arr answer with a run time refreshes `play_ms`; a changed file takes the new file's or none. `sha256` stays empty until qualify fills it.
- An existing row only gets its identity fields refreshed while `state=whole`. When the arr's size or mtime differs from a `whole` row's, it updates them and clears `sha256`, `nzb_path`, `nzb_path2`, `restore_score`, `restore_note`, `remote_path` and the head/tail facts.
- A path that is a symlink into the tierfs mount (`<id><ext>`, with the path's own extension) of a row that lists another path is that row's file after an arr rename. The row's `lib_path` is changed to the new path, so the file can still be promoted, and no new row is made (result count `renamed`). It is refused (`renamed_skipped`, nothing changes) unless the row is `offloaded` or `failed` and its old path no longer holds a link to the same entry (two links mean a copy, not a rename).
- It never deletes a row. A path the arrs no longer list is logged.
- The runner fills `rating_key`, `kind` and `is4k` from Plex.
- The result is a dict with these int counts: `listed`, `missing_local`, `on_pool`, `ambiguous`, `errors`, `new`, `updated`, `unchanged`, `skipped_state`, `unlisted`, and `applied` (bool). `listed` is the number of files the arrs listed.
- The runner fails closed on the result. It blocks that night's demotes when the result is not a dict, lacks `listed`, `missing_local`, `ambiguous` or `errors`, holds a non-int there, has `errors > 0` or `listed == 0`, or when `missing_local + ambiguous` is over 5% of `listed` (`on_pool` files are not missing and are left out). It alerts `register_failed`.
- `lib_path` is the share path (see "Path namespaces"). `TIERING_ARR_PATH_MAP` maps arr paths to share paths when they differ. Size and mtime come from the resolved disk path.

### Weekly

- STAT every segment of each `offloaded` row's `nzb_path`. All found: set `last_check_ts` and `last_stat_ok_ts`. Any missing: flag a promote at once. Unknown (provider or network trouble): set `last_check_ts` only; alert when `last_stat_ok_ts` is older than 14 days.
- An indexer that fails or answers partly makes the restore-path check "unchecked", never "no restore path". Unchecked never triggers a promote.
- Twin: a stored `twin_nzb_path` is STATed on both providers like the restore NZB. Once an article is gone (or the archived file is), weekly clears `twin_nzb_path` and `twin_remote_path` and logs a `twin_lost` event; a lost twin alone flags nothing. The report counts the files with a twin and the twins lost.
- Restore path: a stored `nzb_path2` is STATed on both providers, and nothing is asked of an indexer while it is live and disjoint from `nzb_path` (weekly checks that locally; `nzb_path2` equal to or overlapping `nzb_path` is never OK). A STAT that fails for a reason other than a provider being unreachable (a disk fault reading the file, say) is "unchecked": it never clears `nzb_path2`. Only an NZB that is not valid or has lost articles is dead. When it is missing, not disjoint, or has lost articles, weekly searches, verifies a candidate by message-id and STAT, and stores it as `nzb_path2`. A candidate that overlaps the dead `nzb_path2` or `nzb_path` never counts. If nothing new and live turns up, weekly flags the loss (alert, event and promote flag) and clears `nzb_path2`; it never reports "OK". When the stored one is known dead but the search for a replacement was incomplete, weekly keeps the path, raises an alert and logs a `restore_nzb_lost` event with `unsure`, and reports the row as unchecked, never OK.

#### Indexer etiquette (paid indexers ban for abuse)

All of it lives in the client, which the runner keeps for weeks, so the caller never resets anything. Budgets are rolling 24-hour windows per indexer.

| Env | Default | Meaning |
|---|---|---|
| `NEWZNAB_SEARCH_BUDGET` | 30 | searches per indexer per 24 hours |
| `NEWZNAB_GRAB_BUDGET` | 10 | NZB grabs per indexer per 24 hours |
| `NEWZNAB_MIN_INTERVAL_S` | 2 | least seconds between two requests to one indexer |
| `NEWZNAB_GRAB_INTERVAL` | 1 | least seconds between two NZB grabs |

An indexer is dropped for the rest of the window after 3 errors in a row, and at once on a 429 or a newznab limit error (`<error code="429|500|501">`, also when it comes back as the answer to a grab). It is left alone for the `Retry-After` time, given in seconds or as an HTTP date and not capped; a missing or unreadable value means the whole window. A grab counts as a success only once the bytes are an NZB.

`NewznabClient(state_path=...)` keeps the request times, the error counts and the drops in a JSON file (written atomically), so a restart or redeploy of the runner does not reset the budgets. Without a path they live in memory only. A file that cannot be read is ignored. A spent budget or a dropped indexer makes a check unchecked, never failed.

The state file is shared by every client and process that names it: each write takes an `fcntl` lock on `<state_path>.lock`, reloads the file, changes it and writes it back before the lock is let go, so two writers keep one budget. Reads (`is_bad`, `attempt_info`, so `oldest_first` over many rows) use the copy in memory and reload only when the file's mtime, size or inode changed. Times read from the file are clamped to now, and a drop whose save time lies in the future keeps the time that was left counted from now, so a wall clock that went back cannot block an indexer for days. The file also holds the release ids found to have no NZB and the per-row qualify record (see Qualify).

A failure of one item is not the indexer's: a grab answered with newznab code 300 ("no such item"), a 404 or 410, or a body that is no NZB. It does not count toward the 3 errors; the release id (the guid, else a hash of the link) gets a strike. A body that is no NZB may be a maintenance page, so a release is called bad (not grabbed again) only after such answers on two different nights (at least 12 hours apart). A bad mark expires 7 days after its last strike, and a valid NZB clears it at once. When one indexer answers with non-NZB bodies for 4 different releases within an hour without giving one NZB, the indexer is the problem: those strikes are taken back, the indexer is dropped through the usual 24-hour backoff, and the grab raises `IndexerDropped` (so the row is `nzb_unsure`). Transport errors, 5xx and other error codes count as before. When a search finds every indexer dropped, out of budget or rate limited, the client raises `IndexersUnavailable` (a `NewznabError`). API keys never appear in a repr, log line or exception, and the release link (which holds the key) is never used as the guid.
- Each row's result is written as soon as it is known, through `update_if(..., expect_state="offloaded")`. A row the runner changed during the run is left alone.
- The monthly full reads pick by the oldest attempt, not the oldest success, so one bad row cannot use the whole budget. A hash mismatch on a full read is treated like `bad_chunk`: `state=failed` and an alert. Three failed attempts in a row on network errors alert.
- The report lists `offloaded` rows and every `failed` row with its `restore_note` and restore path.

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
3. In one read of the stub, hash it with SHA-256 and build the chunk list. Stop if the hash differs from the row's `sha256`. Then write the chunk list, then the manifest. The stub is still whole.
4. Create the symlink at `L.tierfs-tmp`, then `rename` it over `L`. This is atomic.
5. Wait until no process holds the old inode open. Check `/proc/*/fd`, with a timeout.
6. Punch holes in the stub, restore its mtime, and check head and tail against `head_b3` and `tail_b3`.
7. Set `state=offloaded`.

### Promote order

1. Read the middle through `RemoteReader`.
   - **Width:** while someone watches the file (`urgent()` is true), up to `TIERING_PROMOTE_STREAM_FETCHES` (default 6, at most 32) chunks are fetched at once, in cursor order. Otherwise one at a time, and `pause()` is called before each chunk; it blocks while a promote someone watches is running. A promote turns wide as soon as its file is watched. Chunks are written and listed only by the promote's own thread, in the order they finish; the fill file is a set, so the order of its lines does not matter.
   - **Order:** start at the chunk that holds the newest `want` offset for this id and go to the end of the middle, then continue from the start of the middle. A newer `want` outside the next 64 MiB moves the cursor there. Chunks already written are skipped. With no `want`, start at the start of the middle.
   - **Each chunk:**
     - check it against the chunk list;
     - `pwrite` it into the stub's hole and `fdatasync`;
     - then append its start offset as one decimal line to `${TIERFS_MANIFESTS}/<id>.fill` and flush.
   - **Resume:** a resumed promote trusts the chunks the fill file already lists.
   - **API:** `ops.promote(..., want=None, urgent=None, pause=None)`. `want()` returns the newest wanted offset for this id, or None; `urgent()` says someone watches the file; `pause()` blocks while another, watched promote runs. The runner passes all three; promote calls them before each chunk.
   - **Two copies:** a row with a twin has two copies to read from, the upload first. A chunk the first copy fails (fetch errors for `TIERING_PROMOTE_SWITCH_S`, default 20, a short read, or a second BLAKE3 mismatch) is read from the other, and the failed copy goes to the back for the rest of the promote, so later chunks start on the one that works. Every chunk is checked against the chunk list whichever copy it came from. While someone watches the file, a chunk still out after `TIERING_PROMOTE_HEDGE_S` (default 10) is asked of the other copy too and the first checked answer is kept. The errors below are those of the last copy tried.
   - **Errors:**
     - a fetch error is retried with backoff for up to `TIERING_PROMOTE_RETRY_S` (default 600) per chunk. When that time is spent the promote stops but stays resumable (`state=promoting`), and a `failed <offset>` line is appended to the fill file so a waiting player gets EIO now instead of after `TIERFS_WAIT_S`. The line stays 1.5 s (`FAILED_HOLD_S`, tierfs looks every 0.2 s at most) and is then taken off, because tierfs sends no `want` while a failed line stands and a later read would otherwise fail at once and never ask for the file;
     - a BLAKE3 mismatch on two fetches stops the promote at once: append `failed <offset>` and set `state=failed`, as in step 2. Fetches still in flight are told to stop and are not waited for.
2. Hash the whole stub with SHA-256. If it does not match the manifest, stop: set `state=failed`, keep the symlink, and alert. Nothing is swapped.
3. Restore the stub's mtime.
4. Link the stub to `L.tierfs-tmp`, then `rename` it over `L`.
5. Unlink the stub path, remove the manifest, the chunk list and the fill file, and set `state=whole`.

## Runner (`runner.py`)

Swaps are automatic. One long-running process applies the hot set.

- **Each pass (every 60 s):**
  - read Plex sessions, play history back to `now - idle_days`, on deck, in progress and the tierfs events;
  - compute `desired_whole`, then `plan_actions`.
- **Wanted promotes start within 1 s:**
  - The runner follows `${TIERFS_EVENTS}` all the time, not only on each pass.
  - The first `want` for an id starts that file's promote within 1 s. It goes ahead of every other promote and outside `TIERING_PROMOTE_CONCURRENCY`, so a wanted promote never waits in the queue.
  - Later `want`s for the same id go to the running promote and move its cursor.
  - A `want` for a row that is neither `offloaded` nor `promoting` is ignored.
  - A live Plex session on an `offloaded` or `promoting` row is a `want` too, seen on each pass.
- **Watched files go first, at full speed:**
  - A `want` or a live session marks the file watched for 10 minutes (`WATCHED_TTL`).
  - Its promote fetches several chunks at once (see **Promote order**).
  - Every promote nobody watches pauses before its next chunk while a watched one runs, and goes on when it ends. The demote worker pauses the same way before each file.
  - A `want` ignores the retry backoff, because a player is waiting. Wanted attempts on one id are at least 60 s apart (kept in `kv['want_last']`). A row in `state=failed` (for example after two BLAKE3 mismatches) is not retried by a `want`; the fill file's `failed` line already gives the player EIO.
- **Other promotes run at once:**
  - wanted promotes first, then weekly-flagged rows, then a live session's season in episode order, then everything else;
  - at most `TIERING_PROMOTE_CONCURRENCY` (default 2) at a time, not counting wanted promotes.
- **Play history is checked, never trusted blindly:**
  - The runner keeps its own record of the last play of every rating key, so a wiped Plex history cannot make a played file look idle.
  - It also refuses tonight's demotes (alert `history_shrunk`) when Plex's history is much shorter than it should be. "Should be" is the plays it saw last pass (`kv['history_times']`) that are still inside the idle window now, because the window slides and a play that ages out is not a lost play. The check trips when the history holds under 80% of those.
  - Reset after a deliberate purge: `DELETE FROM kv WHERE key IN ('history_count', 'history_times')`.
- **Weekly check:**
  - The runner calls `weekly.run_weekly` at the start of the demote window when `kv['weekly_last_ts']` is 7 or more days old, and on start-up when it is. It then sets the timestamp.
  - That night's demotes wait until the check is done.
  - If the check fails, the runner alerts `weekly_failed` and retries after an hour. Demotes stay blocked until a check succeeds.
  - Flags come only from `weekly_flag` events (`detail.reasons`), read after the cursor `kv['weekly_flag_cursor']`. They never come from `restore_note`, which the next good week overwrites.
  - A flagged row is promoted whether or not the hot set wants it. Its flag is cleared only when its promote finishes.
- **Qualify results:**
  - `nzb_unsure` (a partial search or a spent budget) means "not tonight". The file stays whole, nothing is recorded, and it is tried the next night.
  - Only `no_exact_nzb` means it is not on usenet.
  - The runner keeps one `NewznabClient` for its whole life, built with `state_path=${TIERING_NEWZNAB_STATE}` (default `newznab_state.json` next to `TIERING_DB`), so budgets survive restarts.
- **Demotes run only at night:**
  - only inside `TIERING_DEMOTE_WINDOW` (default `02:00-06:00`, local time);
  - only files that pass `qualify` that night, with the glacier gate "free" and nlink 1;
  - the gate and nlink are looked at again right after `qualify` and before the offload starts, because `qualify` reads the whole file and takes minutes: a torrent that began to seed it meanwhile keeps it whole;
  - capped by `TIERING_DEMOTE_GB_PER_NIGHT` (default 200);
  - no demotes on a night when any promote is queued or any hash check has failed;
  - optionally only the files in `TIERING_ONLY_PATHS` (see **Config from env**).
- **A `bad_chunk` event:** promoting cannot help, because the remote copy is bad. Set `state=failed`, alert, and list it in the weekly report with its restore path. The file stays a working symlink for the parts that are good.
- **Reconcile each pass:** if `L` is no longer the symlink a row expects (an arr upgrade or delete replaced it), stop managing that row. Set `state=failed` with `restore_note="L replaced"`. Leave the stub and the manifest for the weekly report; nothing is deleted.
- **Config from env:**
  - `TIERING_MAX_STREAM_KBPS` gives `Cfg.max_stream_kbps`; `TIERING_KEEP_4K` gives `Cfg.keep_4k`;
  - `TIERING_NEVER_SECTIONS` is a comma list (default `Adult`).
  - `TIERING_ONLY_PATHS` (optional) is an opt-in demote allowlist, for a first live test on one file:
    - comma- or newline-separated library paths, exactly as the runner stores them (the share path, after `TIERING_PATH_MAP` mapping); a value starting with `@` names a file with one path per line. `#` lines and blank lines are ignored. Each path is stripped and loses a trailing slash;
    - when set, only a row whose `lib_path` is listed may be demoted. Any other row is dropped from the demote plan before the disk path, `qualify` or any remote or indexer call, and `_demote_inner` checks again before `qualify`. A half-done offload of an unlisted row is not resumed;
    - promotes, reconcile, the weekly check, wants and the idle clocks are untouched: the allowlist never blocks a promote;
    - matching is exact: no globs, no prefixes, so a typo cannot widen it. Unset or empty means no allowlist. An unreadable `@` file, or a value or file with no paths (only commas or `#` lines), stops the start;
    - at start the runner logs the number of paths (never the paths) and, when some match no row, one warning with that count. In a dry run each skipped demote is logged at debug level and the count at info level.
- **Safety:**
  - dry run unless `TIERING_APPLY=1`, in which case it logs each planned action and changes nothing;
  - one instance at a time (a lock file next to `TIERING_DB`);
  - a heartbeat plus `/healthz` on localhost.

## Safety

- Dry run is the default everywhere. Nothing changes a file unless `TIERING_APPLY=1` is set.
- No code path deletes media. The only unlinks are the stub's extra link, made in step 2 of an offload, and a temp symlink.
- The tests run in GitHub Actions or the sandbox, never against the live library.
- No unverified remote byte is ever served or written: tierfs and promote both check every chunk against the chunk list.
