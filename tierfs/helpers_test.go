package tierfs

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testUser   = "tester"
	testPass   = "s3cret-pass"
	testRemote = "/media/movie.mkv"
	testID     = "00112233445566aa"
	testMtime  = int64(1711393377123456789)
)

var (
	bigOnce sync.Once
	bigBuf  []byte
)

// randomData returns n deterministic pseudo-random bytes.
func randomData(n int, seed byte) []byte {
	b := make([]byte, n)
	var s [32]byte
	s[0] = seed
	rand.NewChaCha8(s).Read(b)
	return b
}

// bigData is the shared ~200 MB test file, generated once.
func bigData() []byte {
	bigOnce.Do(func() { bigBuf = randomData(200_000_000+12345, 7) })
	return bigBuf
}

type reqRec struct {
	start     int64
	cancelled bool
}

// rangeServer serves data with HTTP Range support and records what clients do.
type rangeServer struct {
	*httptest.Server
	data []byte

	requests  atomic.Int64
	cancelled atomic.Int64
	unauth    atomic.Int64
	// failFirst makes the first N requests answer 503.
	failFirst atomic.Int64
	// cutFirst makes the first N requests end mid-body.
	cutFirst atomic.Int64
	// ignoreRange answers 200 with the whole body.
	ignoreRange atomic.Bool

	mu     sync.Mutex
	starts []int64
}

type errWriter struct {
	http.ResponseWriter
	failed atomic.Bool
}

func (w *errWriter) Write(p []byte) (int, error) {
	n, err := w.ResponseWriter.Write(p)
	if err != nil {
		w.failed.Store(true)
	}
	return n, err
}

func newRangeServer(t testing.TB, data []byte) *rangeServer {
	rs := &rangeServer{data: data}
	rs.Server = httptest.NewServer(http.HandlerFunc(rs.handle))
	t.Cleanup(rs.Server.Close)
	return rs
}

func (rs *rangeServer) handle(w http.ResponseWriter, r *http.Request) {
	if u, p, ok := r.BasicAuth(); !ok || u != testUser || p != testPass {
		rs.unauth.Add(1)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if r.URL.Path != testRemote {
		http.NotFound(w, r)
		return
	}
	rs.requests.Add(1)
	rs0, re0, haveRange := parseRange(r.Header.Get("Range"))
	if haveRange {
		rs.mu.Lock()
		rs.starts = append(rs.starts, rs0)
		rs.mu.Unlock()
	}
	if rs.failFirst.Add(-1) >= 0 {
		http.Error(w, "busy", http.StatusServiceUnavailable)
		return
	}
	if rs.ignoreRange.Load() {
		r.Header.Del("Range")
	}
	if haveRange && rs.cutFirst.Add(-1) >= 0 {
		// Promise the full range, send part of it, then drop the connection.
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", rs0, re0, len(rs.data)))
		w.Header().Set("Content-Length", strconv.FormatInt(re0-rs0+1, 10))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(rs.data[rs0 : rs0+min(re0-rs0+1, 300<<10)])
		w.(http.Flusher).Flush()
		panic(http.ErrAbortHandler)
	}
	ew := &errWriter{ResponseWriter: w}
	http.ServeContent(ew, r, "movie.mkv", time.Time{}, bytes.NewReader(rs.data))
	if ew.failed.Load() {
		// The client went away mid-body. Wait for the server to notice and
		// cancel the request context, then record it.
		select {
		case <-r.Context().Done():
		case <-time.After(3 * time.Second):
		}
	}
	if r.Context().Err() != nil {
		rs.cancelled.Add(1)
	}
}

func parseRange(h string) (start, end int64, ok bool) {
	n, err := fmt.Sscanf(h, "bytes=%d-%d", &start, &end)
	return start, end, err == nil && n == 2
}

func (rs *rangeServer) reqCount() int { return int(rs.requests.Load()) }

func (rs *rangeServer) startOffsets() []int64 {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	return append([]int64(nil), rs.starts...)
}

func waitFor(t testing.TB, d time.Duration, what string, cond func() bool) {
	t.Helper()
	end := time.Now().Add(d)
	for !cond() {
		if time.Now().After(end) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// buildManifest sizes the head and tail the way the offloader does.
func buildManifest(size int64, stub string) *Manifest {
	const mib = 1 << 20
	head := (size/5 + mib) / mib * mib
	if head*5 < size { // keep it a ceiling
		head += mib
	}
	return &Manifest{
		V: 1, ID: testID, Ext: ".mkv", Size: size, MtimeNS: testMtime,
		HeadLen: head, TailLen: 32 * mib, StubPath: stub, RemotePath: testRemote,
	}
}

// writeStub makes a sparse stub: real bytes in head and tail, a hole between.
func writeStub(t testing.TB, m *Manifest, data []byte) {
	t.Helper()
	f, err := os.Create(m.StubPath)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := f.Truncate(m.Size); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt(data[:m.HeadLen], 0); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt(data[m.Size-m.TailLen:], m.Size-m.TailLen); err != nil {
		t.Fatal(err)
	}
}

func writeManifest(t testing.TB, dir string, m *Manifest) string {
	t.Helper()
	b, _ := json.Marshal(m)
	p := filepath.Join(dir, m.ID+".json")
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, p); err != nil {
		t.Fatal(err)
	}
	return p
}
