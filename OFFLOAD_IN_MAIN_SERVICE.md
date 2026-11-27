# Offload tier in the main service, and stubs beside the media: design proposal

Status: proposal, nothing built. Cites are `file:line` at the commit this file was added in.
Hosts, addresses and MACs are placeholders (`<TIER_MAC>`, `<TIER_IP>`); real values stay in `.env`.

Two requests: (1) "build tiering into plex-automation so the full life cycle lives there", because the `tiering`
container gets a new random MAC on every rebuild and breaks the router reservation; (2) "files moved into the
media folder on the share for that specific media".

**Short answer.** (1) Fix the real problem (the identity) with two lines on the existing tier service. Do not merge
the processes: a merge makes every webhook deploy drop the FUSE mount. (2) Stubs beside the media are feasible and
remove the stub-root setting, but add failure modes the hidden root does not have (folder rename/move, recursive
delete, `media_share.py` zip/size walks). Keep manifests in the state dir.

## A. Merging the tier into the main container

### A1. What the main container lacks

Main service: no `devices`, `cap_add`, `security_opt` or `pid`; mounts only `./data` and the `/mnt/user/data`
subtrees (`docker-compose.yml:158-177`). Tier service: `docker-compose.tiering.yml:28-35,106-120`.

| Need | Where today | Security cost if added to the main service |
|---|---|---|
| `/dev/fuse` | tiering.yml:29-30 | Opens the FUSE device. Low alone; only useful with SYS_ADMIN. |
| `SYS_ADMIN` | tiering.yml:31-33 | Large. Mount and namespace operations; close to root-on-host after any code execution. The main service is a Flask app on a LAN port (`PORT 9876`) whose `APP_AUTH_MODE` defaults to `log` (compose:50): it allows what it would refuse. |
| `SYS_PTRACE` + `pid: host` | tiering.yml:28,33 | Reads `/proc/<pid>/fd` of every host process (the runner needs the shfs daemon's fds; README "Container requirements"). The webhook would see every host process. |
| `apparmor:unconfined` | tiering.yml:34-35 | Drops the container's confinement profile. |
| `/mnt:/mnt:rslave` | tiering.yml:113 | Every `/mnt/diskN` read-write (file ops run on disks, not the share). Today the main service has only the `/mnt/user/data` subtrees, so a webhook bug could touch any disk directly, bypassing shfs. |
| `TIERFS_MOUNT` as `rshared` | tiering.yml:117 | The container publishes a mount to the host. Needs `tiering/tierfs-host-mount.sh` once per array start. |
| Share at Plex's path, ro | tiering.yml:119 | Overlaps the main service's own `/media/*` mounts (compose:164-173). |
| State dir `/state` | tiering.yml:120 | New writable dir (DB, manifests, events, nzb). |
| Secrets | tiering.yml:70-98 | NNTP, indexer, WebDAV and AltMount credentials enter the container that serves the HTTP API. |
| OS/Python deps | tiering/Dockerfile:14-21 | `fuse3`, `blake3` (not in `requirements.txt`), the Go-built `tierfs` binary (an extra build stage), the `offload/` package. The main Dockerfile (1-29) has none; each is a `COPY` that `test_dockerfile_copies.py` and `update.sh:65` must learn about. |
| `TZ` | tiering.yml:39 | The demote window is local time; the main container stays UTC (compose:176 maps the zone file instead). One `TZ` cannot serve both without auditing `datetime` use in `arr-webhook.py`. |

`watch-align` (main Dockerfile:6) is already a name tierfs treats as a background reader (tiering.yml:75); that name
is the only existing coupling between the two images.

### A2. Process model and the mount on redeploy

Merged: one entrypoint (extend `offload/supervise.py`) starting tierfs, then the runner and the webhook. Today the
supervisor stops the runner, then tierfs, on SIGTERM (supervise.py:205-209) and restarts only the runner, with
back-off (supervise.py:225-240). The webhook would need the same back-off, and a tierfs exit still ends the
container (supervise.py:210-214), which would now also kill the webhook.

The decisive problem: `update.sh` rebuilds the image and recreates the container (`update.sh:117,125`). Any webhook
change (`arr-webhook.py` is ~12.9k lines and changes most) would stop tierfs and unmount it. Today the tier is rebuilt
only when `tierfs/`, `tiering/offload/` or its own files change (`update.sh:65`).
- Window: `stop_grace_period 90s` (tiering.yml:37), then up to 120 s waiting for tierfs health (supervise.py:73,166-183).
  Typically seconds; the worst case is minutes.
- While the mount is down, `L` symlinks into `TIERFS_MOUNT` fail (a dead mount answers ENOTCONN, supervise.py:11-14).
  Streams of offloaded files drop (README: "a container restart drops any stream"). Plex scans and arr disk scans in
  the window see missing files.
- The main service is itself a writer: `arr_health.py:301` deletes a "broken" file through the arr and searches
  again when `ARR_HEALTH_APPLY=1` (compose:88-90). Its "many at once" guard (arr_health.py:20,380) covers the media-info
  case only. A mount outage inside the process that applies deletes must be proven safe first (C2).
- No fix inside one container: a code deploy is a container recreate. Reloading the webhook without recreating the
  container is a different, larger change.

### A3. Who needs the tier over the network

- Nothing calls the tier. Its listeners are localhost-only: the runner refuses a non-localhost `TIERING_HTTP`
  (runner.py:1583-1586), tierfs defaults to the loopback address, port 9109 (tierfs/config.go:41), and the healthcheck runs inside
  the container (tiering.yml:100-105). Plex and the arrs read tier data through the bind-mounted FUSE path.
- `TIERING_API` in `glacier-mover.sh` (line 55, calls at 210-230) points at the **main service** (`/tiering/*`
  routes, arr-webhook.py:11301-11390), not the tier. The tier is itself a caller of that API (runner.py:279-283;
  tiering.yml:62).
- The tier only makes outbound calls: main service, Plex, Sonarr, Radarr (tiering.yml:62,76-81), AltMount, NNTP,
  indexers. Plex, the arrs and main sit on br0 (compose:155-157). On Unraid a default-bridge container usually
  cannot reach a br0 (macvlan) address, and the README says the host often cannot (README, "Rebuild/redeploy" paragraph).
  Not verified here: test before relying on it.
- So the tier needs no inbound address, but it needs a network that reaches the br0 peers, which is br0. Pin the
  address; do not remove it.

### A4. Options

1. **Pin the identity.** Add `mac_address: "<TIER_MAC>"` (from a required `.env` value, `TIERING_MAC`) beside the
   existing `ipv4_address` (tiering.yml:106-108), as the main service does (compose:154); add it to `.env.example`.
   Two lines, no new capability anywhere.
2. **Share the main service's network** (`network_mode: "service:arr-webhook"`). No second MAC or IP at all. But compose
   recreates the tier whenever `arr-webhook` is recreated, so the mount drops on every webhook deploy (as in a merge),
   and `networks:`/`ipv4_address` must go.
3. **Full merge** (A1, A2).

### A5. Recommendation

Option 1. A pinned MAC is exactly what fixes the reservation, and `update.sh` already builds and starts both
services from one `COMPOSE_FILE`, so "one lifecycle" is already one command. A merge puts `SYS_ADMIN`, `pid: host`,
unconfined AppArmor and every disk read-write into the Flask service, and makes each webhook deploy cut playback of
offloaded files. Option 2 gives the same drop for less benefit. Revisit a merge only if the owner accepts those costs.

## B. Stubs beside the media

### B1. Today

- `L` is the library file; offloaded, it is a symlink to `TIERFS_MOUNT/<id><ext>` (ops.py:668-670).
- The stub is `<stub_root>/<id><ext>`, a hardlink of `L` made first (`os.link(lib, stub)`, ops.py:763); `L` is then
  replaced atomically by `renameat2(RENAME_EXCHANGE)` (ops.py:870-872). Same disk is required: `_check_fresh` compares
  `st_dev` (ops.py:925), `_pick_stub_root` matches roots by `st_dev` and needs exactly one root per disk
  (ops.py:470-493, `TIERFS_STUB_ROOTS`).
- tierfs only accepts a manifest whose `stub_path` is exactly `<root>/<id><ext>` (tierfs/manifest.go:161-165,217-219).
- File operations run on `/mnt/diskN`, never on the shfs share (`_require_real_fs`, ops.py:276-285; OFFLOAD_DESIGN.md
  "Real file system only"); `TIERING_PATH_MAP` maps `/mnt/user/...` to the disk.
- Mismatch to settle: the owner's paths are `/mnt/user/data/Movies` and `/mnt/user/data/TV Shows`; the compose file
  mounts `/mnt/user/data/Media/Movies` and `.../Media/TV Shows` (compose:165-168), and the glacier mover's default
  root is `data/Media`.

### B2. Option: a hidden stub next to `L`

Stub at `<dir of L>/.<id>.tierstub`, same disk as `L` by construction. `_pick_stub_root`, `TIERFS_STUB_ROOTS` and the
per-disk `.stubs` folders go away. Use the id, not the media name: a long name plus prefix and suffix can pass 255
bytes, and a renamed `L` would not match anyway. tierfs changes from "exact root + name" to "path under an allowed
base (e.g. `/mnt/disk*/data`) whose basename matches `.<id>.tierstub`", still opened `O_NOFOLLOW` and size-checked;
this needs a new `TIERFS_STUB_BASES` setting and a containment check (no `..`, no symlinked parents).

What sees it. Plex's dotfile rule is believed, not checked in this repo: verify in the e2e Plex stack
(`tiering/e2e/test_plex_tierfs.py`).

| Component | Effect | Needed |
|---|---|---|
| Plex scanner | Believed to skip dotfiles; no item. | e2e: no item, no trash entry, no re-analysis. |
| Sonarr/Radarr file rename | Renames the symlink only; stub found by id stays valid. | None. |
| Sonarr/Radarr **folder** rename/move | A rename carries the hidden stub, so the manifest `stub_path` goes stale and tierfs cannot open it. A move across roots is copy+delete and may leave the stub behind or drop it. | Reconcile must re-find stubs by id and rewrite the manifest (`_reconcile`, runner.py:786-792, today only reads the link), or alert. |
| Delete (series, movie, folder) | A recursive delete removes the stub too; manifest and DB row become orphans. Today a deleted `L` leaves the stub in `.stubs` and raises `l_missing` (runner.py:792). Whether an arr removes a folder that still holds a hidden file is unverified. | Treat "stub and `L` both gone" as a finished delete: drop manifest and chunk list, close the row. Test recycle bin on and off. |
| Extra-file import, recycle bin | Hidden file is no recognised extra. Whether the recycle bin follows the `L` symlink and reads the whole file through tierfs is not checked (true today as well). | e2e with the recycle bin on. |
| Glacier mover | Rejects any hidden path part (glacier-mover.sh:387) and uses `find -type f` (:412): never moves the stub or the symlink. | None. Keep the rule: a stub on another disk breaks the same-disk requirement. |
| `abandoned_audit.py` | Skips dot names (:221), hidden parts under downloads (:170) and symlinks (:92). | None. |
| `downloads_audit.py` | Skips dot names and dot dirs (:58-60). | None. |
| `media_share.py` | `os.walk` with no hidden filter in zip (:100), folder download list (:560) and size totals (:1448). The stub would be zipped (sparse, zero-filled middle: **corrupt content**) and counted at full apparent size. | Skip `*.tierstub` in those three walks. |
| Duplicate fix | Driven by the Plex API, so no stub is seen; its filesystem fallback unlinks `L` (compose:166-167) and leaves a stub. | Same orphan handling as delete. |
| Orphan cleanup in the runner | No stub directory scan exists; `_reconcile` checks only `L` and its link. | New sweep for stubs with no manifest/row. Needs a tree walk: run rarely, never wake a sleeping disk. |
| `L.tierfs-tmp` | Already created beside `L` during swaps (ops.py:870,1269). | None. |

Verdict: workable, but folder move and delete are data-loss paths the hidden root does not have (a stub in
`/mnt/diskN/.stubs` is outside every arr's reach). The gain is that the setting disappears and the head/tail bytes sit
with the media. If chosen, the folder-move reconcile is the must-have.

### B3. The other reading: the restored (promoted) file goes back to the media folder

Promote already does this. It hardlinks the stub to `L.tierfs-tmp` in `L`'s own folder, exchanges it with the symlink
(ops.py:1257-1271), then unlinks the stub and removes manifest, chunk list and fill file (ops.py:1234-1250). The result
is the original inode at the original path with the original mtime; nothing is copied. No change is needed; tell the
owner it is already so.

### B4. Manifests: state dir or beside the stub

Keep them in `TIERING_STATE_DIR` (cache):
- tierfs polls the manifest dir every 5 s (OFFLOAD_DESIGN.md "Manifest reload"). Manifests beside stubs would need a
  library walk on that cadence and wake array disks (the glacier disk is meant to sleep).
- A manifest is not self-sufficient: `nzb_path` and the state live in the SQLite DB (`TIERING_DB`). Splitting the pair
  makes crash recovery harder; the swap steps rely on chunk list, then manifest, then swap (ops.py:769-796).
- Backups: one directory holds DB, manifests, chunk lists and NZBs, so one rule covers recovery.
- Cost of keeping: that directory must be backed up. Optional hardening: write id and `stub_path` into an xattr on the
  stub so a lost manifest can be rebuilt by a scan.

## C. Migration and tests

### C1. Migration of already-offloaded rows

`TIERING_APPLY` defaults to `0` (tiering.yml:42) and the README says to read a night of dry-run lines first, so there
may be no offloaded rows. Count rows in state `offloaded` (table in `tiering/offload/state.py`). Zero: nothing to do.
Option 1 alone changes no state either. Otherwise a resumable runner step, with the pause file set, dry run by default:
1. For each `offloaded` row: `link(old_stub, <dir of L>/.<id>.tierstub)` (same disk, atomic).
2. Rewrite the manifest atomically with the new `stub_path` (temp, fsync, rename as in ops.py:144-154); tierfs reloads
   within 5 s and accepts both bases during the transition.
3. Unlink the old stub only after tierfs serves from the new path.
A crash in between leaves two names for one inode; a rerun finishes. Rows in `offloading` finish or abort first.

### C2. Tests

Option 1: extend `tiering/e2e/test_tiering_compose.sh` to assert the rendered config has the `mac_address` and that
rendering fails when `TIERING_MAC` is unset. No Python or Go change.

Stubs beside the media, if chosen:
- Change: `tiering/offload/tests/test_ops.py`, `test_paths.py`, `test_runner.py`, `test_runner_contract.py`,
  `test_runner_review.py` (they set `TIERFS_STUB_ROOTS`/stub roots); `tierfs/wait_test.go`, `tierfs/stream_test.go`
  and a manifest test for `stubAllowed`; `tiering/e2e/stack.py`, `test_tier_basic.py`, `test_tier_cross.py`
  (`.stubs` folders); `tiering/e2e/SCENARIOS.md`.
- New: tierfs rejects `..`, symlinked parents, wrong basename and a base it was not given; demote/promote with the stub
  in `L`'s folder (and refusal on a name collision); reconcile re-finds a stub after a folder rename; delete of `L`
  and stub clears manifest and row; orphan sweep; migration with a crash after each step; `media_share` zip and size
  skip `*.tierstub`; e2e per arr action (rename, folder move, delete with recycle bin on/off, extra import) plus a Plex
  scan that must create no item; a glacier-mover test that a folder holding a stub and a symlink moves nothing.

Merge, if chosen: supervisor tests for webhook restart and back-off (`test_supervise.py`); a test that `arr_health.py`
never deletes on ENOTCONN or a dangling tier symlink; `test_dockerfile_copies.py` for the new copies.

## Decisions for the owner

1. Is the real goal a stable MAC/IP? If yes, do option 1 (pin `TIERING_MAC`) and skip the merge. (yes/no)
2. If you still want the merge: do you accept `SYS_ADMIN`, `pid: host`, unconfined AppArmor and all `/mnt/diskN` read-write in the webhook container? (yes/no)
3. If merged: do you accept the FUSE mount dropping, and offloaded streams stopping, on every webhook deploy? (yes/no)
4. Do you want stubs beside the media, accepting the folder-move and delete handling in B2? (yes/no)
5. Is the media tree `data/Movies` and `data/TV Shows` (your wording) or `data/Media/...` (compose and glacier defaults)? (answer)
6. Manifests stay in the state dir (recommended)? (yes/no)
7. Has `TIERING_APPLY=1` ever been set, i.e. are there offloaded rows to migrate? (yes/no)
8. May `arr_health.py` apply mode stay on while the tier can go down, or must it first skip tier symlinks? (answer)
