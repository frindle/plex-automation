# The `tiering` container (docker-compose.tiering.yml): tierfs, the FUSE mount
# offloaded files are read through, and the offload runner, in one image.
# offload/supervise.py starts tierfs first, restarts the runner on its own when
# it exits, and stops tierfs last. Build context: the repo root
# (docker build -f tiering/Dockerfile .).
FROM golang:1.24-alpine AS tierfs
WORKDIR /src
COPY tierfs/go.mod tierfs/go.sum ./
RUN go mod download
COPY tierfs/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/tierfs ./cmd/tierfs

FROM python:3.15-slim
# fuse3 brings fusermount3, which tierfs mounts and unmounts with.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fuse3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir blake3
WORKDIR /app
COPY --from=tierfs /out/tierfs /usr/local/bin/tierfs
COPY tiering/offload ./offload
CMD ["python", "-u", "-m", "offload.supervise"]
