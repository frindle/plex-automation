FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/tierfs ./cmd/tierfs

FROM alpine:3.21
# fuse3 provides fusermount3, which go-fuse falls back to for mounting.
RUN apk add --no-cache fuse3 ca-certificates
COPY --from=build /out/tierfs /usr/local/bin/tierfs
ENTRYPOINT ["/usr/local/bin/tierfs"]
