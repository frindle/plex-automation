# syntax=docker/dockerfile:1
# AltMount v0.4.0 with one change: nntppool pinned to the commit that fixes javi11/nntppool#104. In nntppool v5.0.0
# an attempt the attempt timer abandons can leave its first chunk in the caller's buffer, and the retry then appends
# the whole body after it. The CRC passes, so AltMount serves (and caches) the article as its first chunk followed
# by the full body again (kipsilabs/altmount#1089). With one provider this happens on the escalated retry pass.
#
# Everything except /app/altmount is the upstream image. The binary is built the way upstream's release image is
# (docker/Dockerfile.ci, used by release.yml): Debian bookworm toolchain, static glibc link. The alpine builder in
# docker/Dockerfile cannot link it: gopar-turbo's prebuilt libgf16 needs glibc's __memcpy_chk. Non-CLI build: the
# web UI is served from /app/frontend/dist, which the upstream image already has.
#
# Remove this directory and the build: in docker-compose.tiering.yml once an AltMount release requires a nntppool
# that includes the fix (javi11/nntppool#107 asks for the tag).
ARG ALTMOUNT_IMAGE=ghcr.io/kipsilabs/altmount:v0.4.0

FROM golang:1.27-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git gcc g++ libc6-dev libsqlite3-dev \
 && rm -rf /var/lib/apt/lists/*
# AltMount v0.4.0's commit, and the nntppool commit that merged #104.
ARG ALTMOUNT_COMMIT=276157560ecd510de127c69b5368aaa0e0ce4277
ARG NNTPPOOL_COMMIT=292745291188a4a1afd497837356b97e24dfe54b
WORKDIR /src
RUN git init -q . \
 && git fetch -q --depth 1 https://github.com/kipsilabs/altmount.git "$ALTMOUNT_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$ALTMOUNT_COMMIT"
# go get turns the commit into a pseudo-version; the new go.sum lines are checked against sum.golang.org.
RUN go get "github.com/javi11/nntppool/v5@$NNTPPOOL_COMMIT" \
 && go list -m github.com/javi11/nntppool/v5 | tee /tmp/nntppool.version \
 && grep -q "$(echo "$NNTPPOOL_COMMIT" | cut -c1-12)" /tmp/nntppool.version
# The fix's own regression test (single provider), run from a writable copy of the module so AltMount's go.mod
# stays as go get left it. -v plus the grep: a -run pattern that matches nothing would otherwise pass.
RUN cp -r "$(go list -m -f '{{.Dir}}' github.com/javi11/nntppool/v5)" /tmp/nntppool \
 && chmod -R u+w /tmp/nntppool \
 && cd /tmp/nntppool \
 && go test -count=1 -timeout 10m -v -run '^TestAttemptTimerDoesNotSpliceBodiesInCallerWriter$' ./... | tee /tmp/fix-test.log \
 && grep -q -- '--- PASS: TestAttemptTimerDoesNotSpliceBodiesInCallerWriter' /tmp/fix-test.log
# AltMount's usenet package (the article reader and its retry, hedge and cache paths) against the new nntppool.
RUN go test -count=1 -timeout 15m ./internal/usenet/
RUN CGO_ENABLED=1 GOOS=linux go build -a \
    -ldflags "-linkmode external -extldflags \"-static\" -X 'github.com/kipsilabs/altmount/internal/version.Version=v0.4.0' -X 'github.com/kipsilabs/altmount/internal/version.GitCommit=$ALTMOUNT_COMMIT'" \
    -o /out/altmount cmd/altmount/main.go \
 && go version -m /out/altmount | grep -E "github.com/javi11/nntppool/v5[[:space:]]" | tee /tmp/linked \
 && grep -q "$(echo "$NNTPPOOL_COMMIT" | cut -c1-12)" /tmp/linked

FROM ${ALTMOUNT_IMAGE}
ARG NNTPPOOL_COMMIT=292745291188a4a1afd497837356b97e24dfe54b
COPY --from=build /out/altmount /app/altmount
LABEL altmount.nntppool.commit=$NNTPPOOL_COMMIT
