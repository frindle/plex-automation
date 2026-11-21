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

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TIERFS_MOUNT` | required | Where to mount. |
| `TIERFS_MANIFESTS` | required | Directory of `<id>.json` manifests. |
| `TIERFS_REMOTE_BASE` | required | Base URL, for example `http://altmount:8080`. The manifest's `remote_path` is appended. |
| `TIERFS_REMOTE_USER`, `TIERFS_REMOTE_PASS` | none | Basic auth. Never logged. |
| `TIERFS_EVENTS` | off | JSONL events file. Rotates at 64 MiB, keeps 3 old files. |
| `TIERFS_HTTP` | `127.0.0.1:9109` | `/healthz` and `/metrics`. Must be a localhost address. |
| `TIERFS_READAHEAD` | `32MiB` | Read-ahead per stream. Accepts `K`, `M`, `G`, `KiB`, `MiB`, `GiB`. |
| `TIERFS_MAX_STREAMS` | `8` | Open remote streams, all handles together. |
| `TIERFS_ALLOW_OTHER` | `1` | Let other users (such as Plex) read the mount. Set `0` to turn off. |
| `TIERFS_POLL` | `5s` | How often manifests are rescanned. `SIGHUP` rescans at once. |

Manifests that are invalid are skipped with a log line. If a manifest is
removed its entry disappears, but handles that are already open keep working.

## Events

One JSON line per event: `{"ts": 1711393377.5, "id": "...", "ev": "open", "bytes_local": 0, "bytes_remote": 0}`.
`ev` is `open`, `remote_read` (once per handle, on the first byte read from the
remote), `close` (carries the totals for the handle) or `bad_chunk` (a chunk
failed verification twice; it also carries `"offset"`, the chunk's start in the
file).

## Metrics

`/metrics` is Prometheus text: `tierfs_bytes_local_total`,
`tierfs_bytes_remote_total`, `tierfs_open_handles`, `tierfs_active_streams`,
`tierfs_remote_errors_total`, `tierfs_remote_requests_total`, `tierfs_bad_chunks_total`,
`tierfs_manifests` and the histogram `tierfs_remote_ttfb_seconds`.

## Running it

Build the image from this directory:

    docker build -t tierfs .

The container needs `/dev/fuse` and `SYS_ADMIN`. The mount has to be shared back
to the host (and from there to Plex) with `rshared` propagation:

    docker run -d --name tierfs \
      --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
      --network host \
      -v /srv/tierfs/mnt:/mnt/tierfs:rshared \
      -v /srv/tierfs/manifests:/manifests:ro \
      -v /srv/stubs:/srv/stubs \
      -v /srv/tierfs/events:/events \
      -e TIERFS_MOUNT=/mnt/tierfs \
      -e TIERFS_MANIFESTS=/manifests \
      -e TIERFS_EVENTS=/events/events.jsonl \
      -e TIERFS_REMOTE_BASE=http://altmount:8080 \
      -e TIERFS_REMOTE_USER=user -e TIERFS_REMOTE_PASS=pass \
      tierfs

Notes:

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
