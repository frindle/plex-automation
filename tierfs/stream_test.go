package tierfs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zeebo/blake3"
)

// These tests exercise the remote stream without FUSE.

const streamTestSize = 6 << 20

func testRemoteCfg(rs *rangeServer) RemoteConfig {
	return RemoteConfig{
		Base: rs.URL, User: testUser, Pass: testPass,
		ReadAhead: 1 << 20, BackKeep: 256 << 10, MaxStreams: 4,
		Window: 1500 * time.Millisecond, IdleTimeout: time.Second,
	}
}

func newTestRemote(t *testing.T, rs *rangeServer, mod func(*RemoteConfig)) *Remote {
	t.Helper()
	cfg := testRemoteCfg(rs)
	if mod != nil {
		mod(&cfg)
	}
	r, err := NewRemote(cfg, &Metrics{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(r.Close)
	return r
}

func TestStreamSequentialMatches(t *testing.T) {
	data := randomData(streamTestSize, 1)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, nil)
	s := r.newStream("x", testRemote, 1000, streamTestSize-1000)
	defer s.Close()
	got := make([]byte, 0, streamTestSize)
	buf := make([]byte, 100_003)
	for off := int64(1000); off < streamTestSize-1000; {
		n := min(int64(len(buf)), streamTestSize-1000-off)
		if err := s.readAt(context.Background(), buf[:n], off); err != nil {
			t.Fatal(err)
		}
		got = append(got, buf[:n]...)
		off += n
	}
	if !bytes.Equal(got, data[1000:streamTestSize-1000]) {
		t.Fatal("stream data differs from source")
	}
	if rs.reqCount() != 1 {
		t.Fatalf("want 1 request, got %d", rs.reqCount())
	}
	if rs.unauth.Load() != 0 {
		t.Fatal("request sent without credentials")
	}
}

func TestStreamReadsSlightlyOutOfOrder(t *testing.T) {
	data := randomData(streamTestSize, 2)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, nil)
	s := r.newStream("x", testRemote, 0, streamTestSize)
	defer s.Close()
	a, b := make([]byte, 64<<10), make([]byte, 64<<10)
	// Kernel read-ahead can deliver neighbouring reads in either order.
	if err := s.readAt(context.Background(), b, 64<<10); err != nil {
		t.Fatal(err)
	}
	if err := s.readAt(context.Background(), a, 0); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(a, data[:64<<10]) || !bytes.Equal(b, data[64<<10:128<<10]) {
		t.Fatal("wrong bytes")
	}
	if rs.reqCount() != 1 {
		t.Fatalf("reordered reads restarted the stream (%d requests)", rs.reqCount())
	}
}

func TestStreamSeekCancelsOldRequest(t *testing.T) {
	// Big enough that the server cannot finish into socket buffers.
	const size = 96 << 20
	data := randomData(size, 3)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, nil)
	s1 := r.newStream("x", testRemote, 0, size)
	p := make([]byte, 4096)
	if err := s1.readAt(context.Background(), p, 0); err != nil {
		t.Fatal(err)
	}
	if s1.covers(40 << 20) {
		t.Fatal("an offset far beyond the read-ahead window must not be covered")
	}
	if !s1.covers(512 << 10) {
		t.Fatal("an offset inside the window must be covered")
	}
	// The producer is now blocked writing; the server is mid-response.
	waitFor(t, 3*time.Second, "first request", func() bool { return rs.reqCount() == 1 })
	s2 := r.newStream("x", testRemote, 40<<20, size)
	defer s2.Close()
	s1.Close()
	if err := s2.readAt(context.Background(), p, 40<<20); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p, data[40<<20:40<<20+4096]) {
		t.Fatal("wrong bytes after seek")
	}
	waitFor(t, 5*time.Second, "server to see the cancelled request", func() bool { return rs.cancelled.Load() >= 1 })
	select {
	case <-s1.done:
	case <-time.After(2 * time.Second):
		t.Fatal("cancelled stream's goroutine did not exit")
	}
	// Bytes already buffered may still be served, but never new ones.
	if err := s1.readAt(context.Background(), p, 20<<20); err == nil {
		t.Fatal("read of unfetched data on a closed stream must fail")
	}
}

func TestStreamGlobalCap(t *testing.T) {
	data := randomData(streamTestSize, 4)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, func(c *RemoteConfig) { c.MaxStreams = 1 })
	s1 := r.newStream("a", testRemote, 0, streamTestSize)
	p := make([]byte, 4096)
	if err := s1.readAt(context.Background(), p, 0); err != nil {
		t.Fatal(err)
	}
	s2 := r.newStream("b", testRemote, 0, streamTestSize)
	defer s2.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	if err := s2.readAt(ctx, p, 0); err == nil {
		t.Fatal("second stream got data while the cap was full")
	}
	if rs.reqCount() != 1 || r.m.ActiveStreams.Load() != 1 {
		t.Fatalf("requests=%d active=%d, want 1 and 1", rs.reqCount(), r.m.ActiveStreams.Load())
	}
	s1.Close()
	if err := s2.readAt(context.Background(), p, 0); err != nil {
		t.Fatalf("second stream should run once a slot is free: %v", err)
	}
	if !bytes.Equal(p, data[:4096]) {
		t.Fatal("wrong bytes")
	}
}

func TestStreamRetriesThenSucceeds(t *testing.T) {
	data := randomData(streamTestSize, 5)
	rs := newRangeServer(t, data)
	rs.failFirst.Store(2)
	r := newTestRemote(t, rs, nil)
	s := r.newStream("x", testRemote, 0, streamTestSize)
	defer s.Close()
	p := make([]byte, 100_000)
	if err := s.readAt(context.Background(), p, 0); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p, data[:100_000]) {
		t.Fatal("wrong bytes after retries")
	}
	if r.m.RemoteErrors.Load() < 2 {
		t.Fatalf("expected the errors to be counted, got %d", r.m.RemoteErrors.Load())
	}
}

func TestStreamResumesAfterCutConnection(t *testing.T) {
	data := randomData(streamTestSize, 6)
	rs := newRangeServer(t, data)
	rs.cutFirst.Store(1)
	r := newTestRemote(t, rs, nil)
	s := r.newStream("x", testRemote, 0, streamTestSize)
	defer s.Close()
	got := make([]byte, 0, streamTestSize)
	buf := make([]byte, 128<<10)
	for off := int64(0); off < streamTestSize; off += int64(len(buf)) {
		if err := s.readAt(context.Background(), buf, off); err != nil {
			t.Fatal(err)
		}
		got = append(got, buf...)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("data differs after the connection was cut and resumed")
	}
	if rs.reqCount() < 2 {
		t.Fatal("expected a second request to resume")
	}
}

func TestStreamErrorsWhenRemoteDown(t *testing.T) {
	data := randomData(streamTestSize, 8)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, nil)
	rs.Close()
	s := r.newStream("x", testRemote, 0, streamTestSize)
	defer s.Close()
	p := bytes.Repeat([]byte{0xAA}, 4096)
	t0 := time.Now()
	err := s.readAt(context.Background(), p, 0)
	el := time.Since(t0)
	if err == nil {
		t.Fatal("expected an error")
	}
	if el < time.Second || el > 5*time.Second {
		t.Fatalf("gave up after %v, want about the 1.5s window", el)
	}
	if !bytes.Equal(p, bytes.Repeat([]byte{0xAA}, 4096)) {
		t.Fatal("buffer was modified on error")
	}
	if strings.Contains(err.Error(), testPass) || strings.Contains(err.Error(), "http://") {
		t.Fatalf("error leaks the URL or credentials: %v", err)
	}
}

func TestStreamRejectsServerThatIgnoresRange(t *testing.T) {
	rs := newRangeServer(t, randomData(streamTestSize, 9))
	rs.ignoreRange.Store(true)
	r := newTestRemote(t, rs, nil)
	s := r.newStream("x", testRemote, 1<<20, streamTestSize)
	defer s.Close()
	if err := s.readAt(context.Background(), make([]byte, 4096), 1<<20); err == nil {
		t.Fatal("a 200 answer to a range request must not be accepted")
	}
}

func TestStreamBadCredentials(t *testing.T) {
	rs := newRangeServer(t, randomData(streamTestSize, 10))
	r := newTestRemote(t, rs, func(c *RemoteConfig) { c.Pass = "wrong" })
	s := r.newStream("x", testRemote, 0, streamTestSize)
	defer s.Close()
	if err := s.readAt(context.Background(), make([]byte, 4096), 0); err == nil {
		t.Fatal("expected an error with bad credentials")
	}
	if rs.unauth.Load() == 0 {
		t.Fatal("server never saw the bad credentials")
	}
}

func TestStreamIdleReleasesSlot(t *testing.T) {
	rs := newRangeServer(t, randomData(streamTestSize, 11))
	r := newTestRemote(t, rs, func(c *RemoteConfig) { c.IdleTimeout = 300 * time.Millisecond })
	s := r.newStream("x", testRemote, 0, streamTestSize)
	if err := s.readAt(context.Background(), make([]byte, 4096), 0); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 3*time.Second, "idle stream to give up its slot", func() bool { return r.m.ActiveStreams.Load() == 0 })
	if err := s.readAt(context.Background(), make([]byte, 4096), 8192); err != nil {
		// Data already buffered may still be served; either is fine, but a
		// stale stream must never hand back wrong bytes.
		t.Logf("read after idle: %v", err)
	}
}

func TestRemoteStripsCredentialsFromBase(t *testing.T) {
	r, err := NewRemote(RemoteConfig{Base: "http://bob:hunter2@altmount:8080/dav/"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if r.cfg.User != "bob" || r.cfg.Pass != "hunter2" {
		t.Fatal("credentials in the base URL should move into the auth config")
	}
	u := r.urlFor("/a b/c.mkv")
	if strings.Contains(u, "hunter2") || strings.Contains(u, "bob") {
		t.Fatalf("URL keeps credentials: %s", u)
	}
	if u != "http://altmount:8080/dav/a%20b/c.mkv" {
		t.Fatalf("unexpected URL %s", u)
	}
	if _, err := NewRemote(RemoteConfig{Base: "ftp://x"}, nil); err == nil {
		t.Fatal("non-http base accepted")
	}
}

func TestParseSizeAndEnv(t *testing.T) {
	for in, want := range map[string]int64{"123": 123, "32MiB": 32 << 20, "32m": 32 << 20, "1GiB": 1 << 30, "64 KiB": 64 << 10} {
		if got, err := ParseSize(in); err != nil || got != want {
			t.Errorf("ParseSize(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
	if _, err := ParseSize("lots"); err == nil {
		t.Error("expected an error")
	}
	env := map[string]string{
		"TIERFS_MOUNT": "/mnt/t", "TIERFS_MANIFESTS": "/m", "TIERFS_REMOTE_BASE": "http://altmount:8080",
		"TIERFS_READAHEAD": "16MiB", "TIERFS_MAX_STREAMS": "3",
	}
	c, err := ConfigFromEnv(func(k string) string { return env[k] })
	if err != nil || c.Remote.ReadAhead != 16<<20 || c.Remote.MaxStreams != 3 || c.HTTPAddr != "127.0.0.1:9109" || !c.AllowOther {
		t.Fatalf("bad config %+v, %v", c, err)
	}
	delete(env, "TIERFS_MOUNT")
	if _, err := ConfigFromEnv(func(k string) string { return env[k] }); err == nil {
		t.Fatal("missing mount must be an error")
	}
}

func TestEventLogRotates(t *testing.T) {
	p := filepath.Join(t.TempDir(), "sub", "events.jsonl")
	e, err := NewEventLog(p, 300)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 40; i++ {
		e.Emit(testID, "close", int64(i), 0)
	}
	e.Close()
	for _, suf := range []string{"", ".1", ".2", ".3"} {
		b, err := os.ReadFile(p + suf)
		if err != nil {
			t.Fatalf("missing %s: %v", suf, err)
		}
		if len(b) > 300 {
			t.Fatalf("%s is %d bytes, over the limit", suf, len(b))
		}
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			var ev Event
			if err := json.Unmarshal([]byte(line), &ev); err != nil || ev.ID != testID || ev.Ev != "close" || ev.TS < 1e9 {
				t.Fatalf("bad line %q: %v", line, err)
			}
		}
	}
	if _, err := os.Stat(p + ".4"); err == nil {
		t.Fatal("kept more than 3 old files")
	}
}

func TestStatusRefusesNonLocalhost(t *testing.T) {
	for _, addr := range []string{"0.0.0.0:0", ":0", "192.0.2.1:9"} {
		s, err := New(Config{Mount: "/x", Manifests: "/y", HTTPAddr: addr, StubRoots: []string{"/stub"}, Remote: RemoteConfig{Base: "http://altmount:8080"}})
		if err != nil {
			t.Fatal(err)
		}
		if err := s.startHTTP(); err == nil {
			t.Errorf("%s accepted", addr)
		}
	}
}

func TestReloadSkipsInvalidAndKeepsOpenSetStable(t *testing.T) {
	dir := t.TempDir()
	s, err := New(Config{Mount: "/x", Manifests: dir, StubRoots: []string{"/stub"}, Remote: RemoteConfig{Base: "http://altmount:8080"}})
	if err != nil {
		t.Fatal(err)
	}
	data := randomData(100<<20+777, 3)
	m := buildManifest(int64(len(data)), "/stub/"+testID+".mkv")
	writeChunks(t, dir, m, data)
	os.WriteFile(filepath.Join(dir, "garbage.json"), []byte("{nope"), 0o644)
	os.WriteFile(filepath.Join(dir, "ffffffffffffffff.json"), []byte(`{"v":1}`), 0o644)
	p := writeManifest(t, dir, m)
	s.Reload()
	if names := s.names(); len(names) != 1 || names[0] != testID+".mkv" {
		t.Fatalf("names = %v", names)
	}
	// A manifest that turns invalid keeps its old entry.
	os.WriteFile(p, []byte("{broken"), 0o644)
	s.Reload()
	if len(s.names()) != 1 {
		t.Fatal("a broken edit removed a good entry")
	}
	// Removal drops it.
	os.Remove(p)
	s.Reload()
	if len(s.names()) != 0 {
		t.Fatal("removed manifest still listed")
	}
	// A missing directory keeps what we have.
	writeManifest(t, dir, m)
	s.Reload()
	os.RemoveAll(dir)
	s.Reload()
	if len(s.names()) != 1 {
		t.Fatal("a missing manifest dir emptied the set")
	}
}

func TestStreamVerifiedBadChunkFailsAfterOneRefetch(t *testing.T) {
	const cs = 1 << 20
	data := randomData(streamTestSize, 9)
	rs := newRangeServer(t, data)
	ck := &chunkCheck{base: 0, size: cs}
	for off := int64(0); off < streamTestSize; off += cs {
		ck.sums = append(ck.sums, blake3.Sum256(data[off:min(off+cs, streamTestSize)]))
	}
	rs.zero(2*cs, 3*cs, 1000)
	r := newTestRemote(t, rs, nil)
	s := r.newVerifiedStream("x", testRemote, 0, streamTestSize, ck)
	defer s.Close()
	p := make([]byte, 1000)
	if err := s.readAt(context.Background(), p, cs); err != nil || !bytes.Equal(p, data[cs:cs+1000]) {
		t.Fatalf("good chunk: %v", err)
	}
	err := s.readAt(context.Background(), p, 2*cs+5)
	var bc *badChunkError
	if !errors.As(err, &bc) || bc.Offset != 2*cs {
		t.Fatalf("got %v, want a bad chunk at %d", err, 2*cs)
	}
	if rs.reqCount() != 2 {
		t.Errorf("%d requests, want 2 (original and one refetch)", rs.reqCount())
	}
}
