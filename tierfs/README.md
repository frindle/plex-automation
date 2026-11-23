# tierfs

A read-only FUSE filesystem that makes offloaded media files look whole.

Each offloaded file has a manifest. tierfs shows one entry per manifest, named
`<id><ext>`, with the original size and mtime. Reads in the first part (head)
and last part (tail) come from a sparse local stub file. Reads in the middle
are fetched from a WebDAV server with HTTP Range requests. The library path is
a symlink into the mount, so Plex sees a normal file. The design is in
`tiering/OFFLOAD_DESIGN.md`.

## How a read works

- Head and tail reads use `pread` on the stub. They never touch the network.
- Middle reads go through one sequential remote stream per open handle, with a
  read-ahead buffer.
- When a read gets 75% of the way through the head, the stream starts at the end
  of the head so the middle is already flowing when the reader gets there.
- A read outside the stream's window (a seek) cancels the old request at once
  and starts a new one.
- If the remote fails, tierfs retries with backoff for up to 10 seconds and then
  returns EIO. It never returns zeros for data it could not fetch.
- Middle bytes are verified. The remote can answer a missing segment with zeros
  and a normal 206, so tierfs fetches whole chunks (`chunk_size`, 4 MiB; the last
  may be short) and checks each one's BLAKE3 against `<id>.chunks` before any of
  it is returned. A random seek fetches the chunk that holds the offset. On a
  mismatch the chunk is fetched again once with a new request. If that fails too
  the read returns EIO and a `bad_chunk` event is written. Other chunks keep
  working.
- A manifest is skipped (its entry is not shown) when `<id>.chunks` is missing,
  is not 32 bytes per chunk, or does not match the manifest's `chunks_b3`. If an
  entry was valid before, the old entry stays.
- Handles are opened with direct I/O, and the tail is `32 MiB + size mod 1 MiB`,
  so head-only and tail-only reads make no remote request.
- At most `TIERFS_MAX_STREAMS` streams are open at once. An unused stream gives
  its slot back after 30 seconds.

## Waiting reads

Each reader is classified from the FUSE request's pid: its `/proc/<pid>/comm`,
and its parent's, are compared with `TIERFS_BACKGROUND_COMMS`. A `Plex Transcoder`
is also background when its `/proc/<pid>/cmdline` is one of Plex's analysis jobs:
loudness (`-f null -`), intro and credits markers (audio written under
`Transcode/Detection/`) or voice activity (`track-NNNN.wav`). A command line that
names any other muxer (dash, segment, mp4, ...) is a player. A pid or command
line that cannot be read counts as a player. A player whose comm is in
`TIERFS_STREAM_COMMS` (Plex Media Server, a streaming Plex Transcoder, Plex DLNA
Server) is a *Plex stream*: someone pressed play.

- Background readers (the Plex scanner, its children and the analysis jobs) keep
  the remote stream described above, so they never promote the file and never
  count as a play.
- A player's middle read never uses the remote. It comes from the stub, for
  chunks listed in `<id>.fill`, and waits until the chunk is listed. The fill
  file is checked at least every 200 ms.
- The first wait for an offset writes a `want` event, and a far seek writes a new
  one. A Plex stream's first read of the file, wherever it is, writes one for the
  start of the middle, so the promote starts when play is pressed. Any other
  player (an arr's ffprobe reading the media info) writes it only once a read
  is 75% of the way through the head. The promote fetches from the newest want.
- `failed <offset>` in the fill file gives EIO at once. `TIERFS_WAIT_S` is a
  no-progress timeout: the read gets EIO only when the fill file has not grown
  for that long. An interrupted request stops waiting.
- A handle keeps its stub fd. Once the manifest is removed (the promote is done)
  every chunk is local.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TIERFS_MOUNT` | required | Where to mount. |
| `TIERFS_MANIFESTS` | required | Directory of `<id>.json` manifests. |
| `TIERFS_STUB_ROOTS` | required | Comma list of stub directories. A manifest is skipped unless `stub_path` is exactly `<root>/<id><ext>`. The stub is opened without following links and must be a regular file of the manifest's size. |
| `TIERFS_REMOTE_BASE` | required | Base URL, for example `http://altmount:8080`. The manifest's `remote_path` is appended. |
| `TIERFS_REMOTE_USER`, `TIERFS_REMOTE_PASS` | none | Basic auth. Never logged. |
| `TIERFS_EVENTS` | off | JSONL events file. Rotates at 64 MiB, keeps 3 old files. |
| `TIERFS_HTTP` | `127.0.0.1:9109` | `/healthz` and `/metrics`. Must be a localhost address. |
| `TIERFS_READAHEAD` | `32MiB` | Read-ahead per stream. Accepts `K`, `M`, `G`, `KiB`, `MiB`, `GiB`. |
| `TIERFS_MAX_STREAMS` | `16` | Open remote streams, all handles together. Each stream buffers up to `TIERFS_READAHEAD` (32 MiB) plus 8 MiB kept behind the reader, so plan for about 40 MiB of RAM per stream, 640 MiB at the default 16. |
| `TIERFS_SLOT_WAIT` | `20s` | How long a read waits for a free stream slot. After that it gets EIO and a `{"ev":"slot_timeout","id":...}` event. |
| `TIERFS_ALLOW_OTHER` | `1` | Let other users (such as Plex) read the mount. Set `0` to turn off. |
| `TIERFS_POLL` | `5s` | How often manifests are rescanned. `SIGHUP` rescans at once. |
| `TIERFS_BACKGROUND_COMMS` | `Plex Media Scan,watch-align` | Comma list of process names (`comm`, 15 characters) whose reads, and their children's, keep the remote stream. `watch-align` is the webhook's ffmpeg matching an old and a new copy's sound before an upgrade (see the main README, Watch hold). Every other reader is a player and waits for the promote. |
| `TIERFS_STREAM_COMMS` | `Plex Media Server,Plex Transcoder,Plex DLNA Server` | Comma list of process names (`comm`, 15 characters) that are Plex streams: their first read of an offloaded file asks for the promote at once. A Plex Transcoder running an analysis job stays background. Unset or empty: the default. |
| `TIERFS_WAIT_S` | `900` | Seconds a player read waits with no new line in the fill file before it gets EIO. |

Manifests that are invalid are skipped with a log line. If a manifest is
removed its entry disappears, but handles that are already open keep working.

## Events

One JSON line per event: `{"ts": 1711393377.5, "id": "...", "ev": "open", "bytes_local": 0, "bytes_remote": 0}`.
`ev` is `want` (carries `"offset"`: the player needs this part of the file next), `open`, `remote_read` (once per handle, on the first byte read from the
remote), `close` (carries the totals for the handle) or `bad_chunk` (a chunk
failed verification twice; it also carries `"offset"`, the chunk's start in the
file).

## Metrics

`/metrics` is Prometheus text: `tierfs_bytes_local_total`,
`tierfs_bytes_remote_total`, `tierfs_open_handles`, `tierfs_active_streams`,
`tierfs_remote_errors_total`, `tierfs_remote_requests_total`, `tierfs_bad_chunks_total`,
`tierfs_waiting_reads`, `tierfs_wait_reads_total`, `tierfs_wait_timeouts_total`,
`tierfs_wait_failed_total`, `tierfs_want_events_total`,
`tierfs_manifests` and the histogram `tierfs_remote_ttfb_seconds`.

## Running it

tierfs runs in the `tiering` container together with the offload runner: the
image is `tiering/Dockerfile` (built from the repo root), the service is in
`docker-compose.tiering.yml`, and `tiering/README.md` ("Deploy with compose")
has the steps. The supervisor there (`tiering/offload/supervise.py`) starts
tierfs first and stops it last.

The container needs `/dev/fuse` and `SYS_ADMIN`. The mount has to be shared back
to the host (and from there to Plex) with `rshared` propagation, on a host
directory that is itself a shared mount point (`tiering/tierfs-host-mount.sh`).
The standalone shape, for a test (`tiering/e2e/tierfs_in_container.sh` runs it
like this in CI):

    docker build -f tiering/Dockerfile -t tiering .
    docker run -d --name tiering \
      --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
      --network host --pid host \
      -v /srv/tierfs/mnt:/mnt/tierfs:rshared \
      -v /srv/tierfs/manifests:/manifests:ro \
      -v /srv/stubs:/srv/stubs \
      -v /srv/tierfs/events:/events \
      -e TIERFS_MOUNT=/mnt/tierfs \
      -e TIERFS_MANIFESTS=/manifests \
      -e TIERFS_STUB_ROOTS=/stubs \
      -e TIERFS_EVENTS=/events/events.jsonl \
      -e TIERFS_REMOTE_BASE=http://altmount:8080 \
      -e TIERFS_REMOTE_USER=user -e TIERFS_REMOTE_PASS=pass \
      -e SUPERVISE_RUNNER='["sleep","infinity"]' \
      tiering

(`SUPERVISE_RUNNER` replaces the runner, so only tierfs does any work.)

Notes:

- `--pid host` (`pid: host` in compose) is needed so the pid in each FUSE request
  can be looked up in `/proc`. Without it every reader looks unreadable and is a
  player.

- The `stub_path` in each manifest must be readable inside the container at the
  same path, so mount the stub directory there.
- The host directory behind `/mnt/tierfs` must itself be a shared mount point
  (`mount --make-rshared`) for propagation to work. Other containers then mount
  it with `:rslave` or `:rshared`.
- With `--network host`, `TIERFS_HTTP` is on the host's localhost. Without it,
  the status port is only reachable inside the container.

## Tests

    go test ./...

The FUSE tests need `/dev/fuse` and `fusermount3` and skip themselves without
them. Set `TIERFS_REQUIRE_FUSE=1` to make a missing FUSE an error. CI does.
