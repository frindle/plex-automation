# Glacier mover

Nightly Unraid User Script with two phases that share one nightly cap (`PHASES`, default `evict fill`):
- **evict**: moves what keeps disk1 awake (the data of every torrent that is not paused, plus `EVICT_HOT_ROOTS`) from disk1 to disk2, so disk1 can spin down;
- **fill**: moves the least-recently-opened media from disk2/disk3 to disk1 until disk1 is ~98% full. Data of seeding or queued torrents stays where it is.

disk1 = glacier, disk2 = warm (seeding, hot data), disk3 stays empty and asleep until disk2 fills. Up-moves (Unspin) and cache-to-array down-moves (Mover Tuning) are separate plugins; this only does glacier.

Dry run unless called with `--apply`. Pure bash plus stock Unraid tools (rsync, find, awk, sort, sha256sum, fuser/lsof, curl). No python3 on the host.

## What it will move (allowlist)
Only files that are all of:
- under a `MEDIA_ROOTS` folder (default `data/Media`, relative to `/mnt/diskN`; the Plex share is not a default root);
- video/audio/audiobook (`MAIN_EXT`) or a subtitle/`.nfo` sidecar (`SIDE_EXT`). A sidecar moves only together with a main file whose name it starts with, same folder, as one unit. Orphan sidecars (`tvshow.nfo`) stay;
- not modified in the last `MIN_AGE_DAYS` (30);
- not open right now (`fuser`, else `lsof`), checked at planning, again right before the copy, and again right before deleting the source.

Never moved even if the extension matches: any path part named Downloads, Incomplete, InfluxDB, Docker, Documents, Sandbox, ollama-models, appdata, system, domains, Vault, bloom-ledger, GC (case-insensitive; `EXCLUDE_NAMES`), or containing `timemachine` or `resell` (`EXCLUDE_CONTAINS`); `*.sparsebundle`; database files (`.db .sqlite* .tsm .wal .shm`); VM disks (`.img .qcow2 .vdisk ...`); anything in or under a hidden (dot) name.

Hardlinked files move as a group (all links on that disk, copied with `rsync -H`) or not at all. A group with any link outside the media roots (Downloads, another share), any link that fails a rule above, or a link count that doesn't match, is skipped whole.

## Evict (disk1 -> disk2)
- Torrent data: `GET $TIERING_API/tiering/active-roots` lists the content root of every torrent that is not paused, stopped or erroring. Roots inside the `EVICT_SHARE` share (`data`) with data on disk1 are evicted whole, any file type, no age limit. A root that would sweep a whole folder (`data/Downloads`, `data/Media/TV`) is ignored with a warning. No answer: torrent data stays this run, hot roots still move.
- Hot roots: every file under `EVICT_HOT_ROOTS` (`data/Documents data/ollama-models`) except database and VM disk files.
- Hardlinks: one scan of disk1 finds every link of each inode, so a pack hardlinked into the library moves with its library link and stays linked; torrents sharing an inode (cross-seeds) become one unit and one pause. A unit with a link in a never-touch folder (`EVICT_NEVER_TOP`: Sandbox, Vault, bloom-ledger, appdata, ...; or containing `timemachine`/`resell`), a sparsebundle, or a link count that doesn't add up stays on disk1 and is listed in the summary.
- The gate runs without `keep_seeding`, so complete torrents are paused around the move and resumed after; downloading/checking/erroring ones wait. Deluge holds seeding files open, so the open-file check happens after the pause (it still skips a file that stays open).
- Folders a move empties on disk1 are removed when the same folder exists on disk2 (never `data` or `data/Downloads`).
- disk2 never goes past `EVICT_DEST_PCT` (95%).
- Spindown: looking for torrent and hot data on disk1 would wake it, so evict skips the look when Unraid reports disk1 spun down (`/var/local/emhttp/disks.ini`) and its read counter (`/proc/diskstats`) has not moved since the last live run ended: nothing on it was read, so nothing on it is hot. The counter is noted in `/var/run/glacier-reads` (RAM, resets at reboot like the counter) after a live run whose evict finished. No note, a reboot, or an unreadable file means evict looks as before. `EVICT_IDLE_SKIP=0` looks every night.
- A live evict needs disk1 excluded from the share (`shareExclude="disk1"` in `/boot/config/shares/data.cfg`, or an include list without disk1); otherwise new downloads land on disk1 again and the run skips evict. A dry run warns and plans anyway.
- In a dry run, fill counts the room evict would free on disk1.

## Each move
Copy (`rsync -aHAXS`: times, perms, owner, ACLs, xattrs) -> verify (size, mtime, sha256 of source vs copy) -> confirm the source did not change and is still closed -> delete the source. A failed verify deletes only the copy and leaves the source. Existing destinations are never overwritten. Paths are `/mnt/diskN/...` only, never `/mnt/user`.

Fill stops when disk1 reaches `TARGET_PCT` (98), evict when disk2 reaches `EVICT_DEST_PCT` (95); both stop at `NIGHTLY_CAP_BYTES` (1.5 TB) together, evict first.

Fill only starts on a source disk that is over `FILL_START_PCT` (85) used, and moves from it until it is down to `FILL_STOP_PCT` (75), so most nights fill moves nothing and a full disk gets one bigger move (spread over nights by the cap). `FILL_START_PCT=0` fills whenever disk1 has room (the old behavior). In a dry run, the source disk counts the bytes evict would put on it.

## Plex yield and run status
Both files live in `STATE_DIR` (default `/mnt/cache/appdata/ollama-queue/state/glacier`, which the ollama-queue container sees as `/home/queue/state/glacier`). The run creates only the last folder, and only when its parent exists; otherwise neither file is used. Dry runs use neither.
- `plex-busy`: the queue's Plex watcher touches it while Plex is streaming. Before each unit, a live run that finds it less than `PLEX_BUSY_MAX_AGE` (600) seconds old logs `YIELD`, checks again every `YIELD_POLL_SEC` (60), and stops for the night if the move window closes while it waits. An older file is ignored, so a dead watcher never blocks moves. The notification says how long it waited.
- `glacier-status`: one line `<epoch> <state> phase=evict|fill moved_bytes=N yielded_sec=N [reason=...]`, written atomically at start, before each unit, while waiting, and on exit. States: `start`, `moving`, `yield`, `done`, `stopped` (`reason=window` or `reason=signal`), `failed` (`reason=copy`, or no reason when the run died early).

## Ranking and access times
Ranked by atime (`stat` only, file contents are never read for ranking). Today the disks are mounted `noatime`, so atime is roughly the date each file was added; the script warns and still works. After access times are turned on (`relatime,nodiratime`), ranking becomes real last-opened. `REQUIRE_ATIME=1` makes it abort on `noatime` instead.

## Torrents
Torrents are paused around each move and never removed. The path the torrent sees (`/mnt/user/...`) does not change when a file goes disk to disk, and `rsync -a` keeps size and mtime, so a resumed torrent keeps seeding with no recheck.

1. Classification, once per chunk: `POST $TIERING_API/tiering/torrent-gate` (no side effects). `free` = no torrent owns the file. `pause` = every owning torrent is complete (progress 100) in a known healthy state. `wait` = an owner is incomplete, erroring, checking or unknown: the unit is skipped and retried another night. The fill phase adds `keep_seeding=1`: an owner that is seeding or queued also means `wait` (only already-paused torrents' data goes to disk1), and `torrent-pause` refuses such owners. API unreachable or no answer: also `wait` (fail safe). There is no seed-time rule; the weekly stalled-seed cleanup stays the only thing that unseeds.
2. Per unit, right around the move (not per chunk): `POST /tiering/torrent-pause` with the unit's paths pauses the owners and returns the hashes it paused. The mover waits `PAUSE_SETTLE_SEC` (3) for Deluge to close the files, re-runs the open-file check inside `move_unit`, moves, then `POST /tiering/torrent-resume` with those hashes. Resume happens on every outcome (moved, skipped, failed). If the pause call fails, the unit is treated as `wait`.
3. A torrent already paused by someone else is not paused and not resumed (the file still moves). The resume endpoint only resumes hashes the gate paused.
4. Crash safety: the server keeps a ledger of paused hashes + time (`TIERING_PAUSE_STATE_PATH`, default `/data/tiering_paused.json`). Every gate/pause/resume call first resumes entries older than `TIERING_PAUSE_MAX_MIN` (default 360). The mover also resumes its own paused hashes from an EXIT/INT/TERM trap. If a user pauses a torrent by hand after the gate paused it, the stale auto-resume can resume it.
5. Dry run never calls pause/resume; it prints `WOULD MOVE ... [torrent: pause]`.

Rebuild/redeploy the plex-automation container so the new endpoints exist (`tiering_gate.py` is in the Dockerfile). `TIERING_API` has no default: set it to the container's URL (the User Script wrapper exports it). On Unraid the host often cannot reach a br0 container IP, so use an address the host can reach. Keep `/data` persistent so the ledger survives a container restart.

## Install (User Scripts plugin)
1. Copy `glacier-mover.sh` to `/boot/config/plugins/user.scripts/scripts/glacier-mover/script`:
   ```
   mkdir -p /boot/config/plugins/user.scripts/scripts/glacier-mover
   cp glacier-mover.sh /boot/config/plugins/user.scripts/scripts/glacier-mover/script
   ```
   Or make `script` a two-line wrapper that sets env vars and runs `bash /boot/config/glacier-mover.sh "$@"`.
2. Settings -> User Scripts: set the script to run **Custom** with cron `0 22 * * *` (22:00 local). Live copies stop at the end of `MOVE_WINDOW` (default `22:00-06:00`, host local time, may wrap midnight; `""` = any time); a unit that would start after it waits for the next night.
3. Dry run first: click "Run Script" with no flag, read the output. Check the allowlist roots, rejected-by-rule counts, per-disk counts and the top-20 folders.
4. When happy, change the script body to run `--apply` (or the wrapper: `exec bash .../glacier-mover.sh --apply`).
5. Set `LOG_DIR=/mnt/cache/appdata/tiering` to keep the log across reboots (default is `/var/log/tiering`, which is RAM). One Unraid notification per run with moved/failed/skipped counts.

Settings via env vars (defaults in the script header): `PHASES MEDIA_ROOTS GLACIER SRC_DISKS TARGET_PCT FILL_START_PCT FILL_STOP_PCT NIGHTLY_CAP_BYTES MIN_AGE_DAYS EXCLUDE_NAMES EXCLUDE_CONTAINS MAIN_EXT SIDE_EXT EVICT_TO EVICT_SHARE EVICT_HOT_ROOTS EVICT_DEST_PCT EVICT_NEVER_TOP SHARE_CFG TIERING_API PAUSE_SETTLE_SEC NOTIFY_ON_DRY LOG_DIR TURBO_WRITE TURBO_MIN_BYTES EVICT_IDLE_SKIP MOVE_WINDOW STATE_DIR PLEX_BUSY_MAX_AGE YIELD_POLL_SEC`. `PHASES=fill` turns evict off.

## Turbo write
A live run turns Unraid's turbo write on (`mdcmd set md_write_method 1`, reconstruct write) just before its first copy and sets it back to read/modify/write (`0`) when it ends, including on INT/TERM. Copies to the array run much faster, but every array disk spins while the run copies. If the setting already reads `1` in `/var/local/emhttp/var.ini` (turned on by hand), the run leaves it alone both ways. The value to restore is kept in `/var/run/glacier-turbo-write.restore` until it is set back, so a run that was killed outright is undone by the next run (dry runs too). Dry runs and runs with nothing to move never touch it. `TURBO_WRITE=0` turns this off. Turbo is only used for a phase that plans at least `TURBO_MIN_BYTES` (100 GB): a smaller move uses read/modify/write, so only the disks in the copy and parity spin up (`0` = any size). Settings -> Disk Settings keeps showing the saved choice (Auto); a reboot applies the saved choice again.

The run aborts if the array is stopped, a parity check/rebuild is running, or another glacier run holds the lock. If the mover is running (its pid file `/var/run/mover.pid`, which Mover Tuning also writes, or a process named `mover`), a live run waits for it to finish, at the start and before each unit, so both share one spin-up; if the move window ends first, the rest waits for the next night. A dry run goes ahead with a warning.

## Deploy with compose (offload tier)

`docker-compose.tiering.yml` adds one service to the stack: `tiering`, a single container (image built from `tiering/Dockerfile`) that runs tierfs (the FUSE mount) and the offload runner. `offload/supervise.py` is its main process: it starts tierfs, waits for its health check, then starts the runner. If the runner exits it is started again after a back-off (10 s, doubling to 5 min) while the mount stays up; if tierfs exits, the runner is stopped and the container exits, so Docker restarts both. `docker stop` stops the runner first, then tierfs, which unmounts. The service is not loaded until `.env` says so, and the runner is a dry run until `TIERING_APPLY=1`.

1. Host, once per array start (User Scripts, "At Startup of Array"): `bash tiering/tierfs-host-mount.sh`. It makes `TIERFS_MOUNT` a shared mount point so the FUSE mount reaches the host.
2. Make the state folders: `mkdir -p "$TIERING_STATE_DIR"/{manifests,events,nzb}` and one stub folder per media disk (`mkdir /mnt/diskN/.stubs`).
3. Fill the "Offload tier" block in `.env` (see `.env.example`; `TIERING_IP` is one free address on br0) and uncomment `COMPOSE_FILE=...`.
4. Plex and each arr: add a path mapping `TIERFS_MOUNT` -> the same path in the container, access mode **Read Only - Slave**. Library symlinks point at that path, so it must be identical everywhere. A container started before tiering still gets the mount (slave propagation); one mapped without Slave sees an empty folder and every offloaded file looks broken.
5. `./update.sh`, then check `docker logs tiering` (supervisor lines start with `supervise:`). The dry run logs `plan: demote ...` lines; read a night of them before setting `TIERING_APPLY=1`.

To pause offloading without touching the mount, create the pause file: `touch "$TIERING_STATE_DIR/runner.paused"`. The runner stops within a second and is not started again; tierfs keeps serving offloaded files, and the health check counts a paused runner as healthy. Delete the file to resume. Do not stop the container to pause: offloaded files do not open while tierfs is down, and a container restart drops any stream reading an offloaded file at that moment. Promote every file back before removing the tier.

Alerts (`alerts.jsonl` in the state folder) are only written to that file and the runner log. Nothing pushes them yet.

## Offload intake (Python, `tiering/offload`)
Env for the intake clients (the design is in `OFFLOAD_DESIGN.md`):
- `TIERING_ARR_PATH_MAP` (`arr_prefix=share_prefix`, optional) maps Radarr/Sonarr paths to share paths; `TIERING_PATH_MAP` (`share_prefix=disk_prefix`, one entry per disk) finds the file to stat. `register` stores the share path.
- `NEWZNAB_SEARCH_BUDGET` (30 per indexer per 24 hours), `NEWZNAB_GRAB_BUDGET` (10 per 24 hours), `NEWZNAB_MIN_INTERVAL_S` (2) and `NEWZNAB_GRAB_INTERVAL` (1) keep the indexers from banning the account. An indexer is dropped for the window after 3 errors in a row, or on a 429. The client can keep these budgets in a file (`state_path`), so a restart does not reset them.

## Offload runner (deploy)

`python -m offload.runner` is the long-running process that applies the hot set (see `OFFLOAD_DESIGN.md`). It is a dry run unless `TIERING_APPLY=1`. With `TIERING_APPLY=1` it refuses to start without `TIERING_GATE_PATH_MAP`, `TIERFS_STUB_ROOTS`, `TIERFS_EVENTS` (its directory must exist), `TIERFS_MOUNT` and `TIERFS_MANIFESTS`, and unless every disk prefix in `TIERING_PATH_MAP` is covered by a `TIERING_GATE_PATH_MAP` entry. A disk path no gate entry covers is never sent to the gate and counts as not free.

`lib_path` in the database is the share path, the one Plex reports. The runner and ops resolve the disk path at the moment they need it, and a file that is not on exactly one disk is skipped and logged.

Container requirements:
- `pid: host`. Plex reads through Unraid's user share, so only the shfs daemon's open file descriptor shows that a file is in use. The runner has to see host processes to find it.
- `cap_add: SYS_PTRACE`, so it can read `/proc/<pid>/fd` of those processes.
- `TIERING_PROC_CANARY`: a comma list of `comm` names, every one of which must be visible from the runner: the media server (`Plex Media Serv`, the kernel cuts `comm` to 15 characters, so write it cut) and the user-share daemon (`shfs`). If it is unset or any name is not visible, the open-file check says "not clear" and ops refuses to demote, so a container that does not see host processes cannot demote.
- Two kinds of mount: the share at the path Plex uses (so `lib_path` reads) and each `/mnt/diskN` (where file operations run, never through the share's FUSE layer).

The shape of these requirements (`docker-compose.tiering.yml` is the real service, which adds tierfs's needs: `/dev/fuse`, `SYS_ADMIN`, `apparmor:unconfined` and the mount itself):

```yaml
services:
  tiering:
    image: plex-automation
    command: python -m offload.supervise
    pid: host
    cap_add: [SYS_PTRACE]
    volumes:
      - /mnt/user/media:/media                 # the share, at Plex's path
      - /mnt/disk1:/mnt/disk1
      - /mnt/disk2:/mnt/disk2
      - /mnt/cache/appdata/tiering:/state
    environment:
      TIERING_DB: /state/tiering.db
      TIERING_APPLY: "0"                       # "1" to apply
      TIERING_PROC_CANARY: "Plex Media Serv,shfs"
      TZ: Area/City                            # the demote window is local time: set your zone
      TIERING_PATH_MAP: /media=/mnt/disk1/media,/media=/mnt/disk2/media
      TIERING_ARR_PATH_MAP: /tv=/media/tv,/movies=/media/movies
      TIERING_GATE_PATH_MAP: /mnt/disk1/media=/data/media,/mnt/disk2/media=/data/media
      TIERFS_STUB_ROOTS: /mnt/disk1/.stubs,/mnt/disk2/.stubs
      TIERFS_MOUNT: /tierfs
      TIERFS_MANIFESTS: /state/manifests
      TIERFS_EVENTS: /state/events.jsonl
```

Path settings, each a comma list:
- `TIERING_PATH_MAP`: `share_prefix=disk_prefix`, one entry per disk; several entries may share one share prefix (`/media=/mnt/disk1/media,/media=/mnt/disk2/media`). Only a candidate that exists counts.
- `TIERING_ARR_PATH_MAP`: `arr_prefix=share_prefix`, for arrs that see other paths than Plex (`/tv=/media/tv`). Used by register.
- `TIERING_GATE_PATH_MAP`: `disk_prefix=deluge_prefix`, the disk path in the torrent client's namespace. Several disk prefixes may map to one client prefix.
- `TIERFS_STUB_ROOTS`: one stub directory per disk (`/mnt/disk1/.stubs,/mnt/disk2/.stubs`). Ops picks the one on the same disk as the file.

`TIERING_DEMOTE_WINDOW` is local time in the container, so set `TZ` (without it the window runs in UTC).

Alerts go to `TIERING_ALERTS` (JSON lines): `bad_chunk`, `slot_timeout`, `promote_failed`, `promote_retrying` (3 failures in a row; retries back off from 5 minutes, doubling to 6 hours), `promote_worker_died`, `demote_failed`, `register_failed`, `weekly_failed`, `history_shrunk`, and once per id `promote_short_remote`, `takeover_orphan`, `l_missing` and `promote_stuck`. A row flagged by the weekly check is promoted whether or not the hot set wants it. The runner follows `TIERFS_EVENTS` all the time: the first `want` for a file starts its promote within 1 second, outside `TIERING_PROMOTE_CONCURRENCY`, and later `want`s move the promote's cursor. A live Plex session on an offloaded file counts as a `want`. While someone watches a file, its promote fetches `TIERING_PROMOTE_STREAM_FETCHES` (default 6) chunks at once, and every other promote and the demotes pause until it is done. A file whose qualify found a twin (a second upload of the same bytes) is promoted from either copy: a chunk the first copy fails for `TIERING_PROMOTE_SWITCH_S` (default 20) is read from the other, and while someone watches, a chunk still out after `TIERING_PROMOTE_HEDGE_S` (default 10) is asked of both. An offloaded file keeps 20 minutes of play on local disk (`TIERING_HEAD_MIN_S`, default 1200) and at least 20% of the file; a file Plex has no play time for, or one too short for that head to leave 20% offloaded, stays whole. A `want` ignores the retry backoff but not more often than every 60 seconds per file, and never retries a `failed` row. The weekly check runs on start-up and at the start of the demote window when the last run is 7 days old (`kv weekly_last_ts`); that night's demotes wait for it, and a failed run alerts and is retried after an hour. Flags come only from `weekly_flag` events (cursor `kv weekly_flag_cursor`) and end when the promote finishes. A qualify result of `nzb_unsure` means not tonight: nothing is written and the file is tried the next night. One Newznab client serves qualify and the weekly check; its budgets live in `TIERING_NEWZNAB_STATE` (default `newznab_state.json` next to `TIERING_DB`), the weekly report in `TIERING_WEEKLY_REPORT` (default `weekly_report.md` there). `/healthz` reports `worker_deaths` and `stuck_promotes`. The runner keeps its own last-play time per title in the state DB; when Plex's play history is empty or over 20% shorter than last time it refuses that night's demotes (after a deliberate purge, `DELETE FROM kv WHERE key='history_count'`). A register with errors, or with missing or ambiguous files over 5% of the files it listed, blocks that night's demotes.

## Tests (run on a Linux test box)
```
bash tiering/tests/test_glacier.sh        # fake /mnt tree, df and torrent gate stubbed
python -m pytest test_tiering_gate.py     # the endpoint, Deluge mocked
```
