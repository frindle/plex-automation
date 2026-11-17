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

## Tests (run on a Linux test box)
```
bash tiering/tests/test_glacier.sh        # fake /mnt tree, df and torrent gate stubbed
python -m pytest test_tiering_gate.py     # the endpoint, Deluge mocked
```
