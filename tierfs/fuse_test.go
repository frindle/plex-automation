package tierfs

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

// These tests mount a real FUSE filesystem. They skip when FUSE is missing,
// unless TIERFS_REQUIRE_FUSE=1 (set in CI), where a missing FUSE is a failure.

type fuseEnv struct {
	t      *testing.T
	data   []byte
	m      *Manifest
	rs     *rangeServer
	srv    *Server
	mnt    string
	mdir   string
	events string
	path   string // the file inside the mount
}

func requireFuse(t *testing.T) {
	t.Helper()
	missing := ""
	if _, err := os.Stat("/dev/fuse"); err != nil {
		missing = "/dev/fuse"
	} else if _, err := exec.LookPath("fusermount3"); err != nil {
		if _, err := exec.LookPath("fusermount"); err != nil {
			missing = "fusermount"
		}
	}
	if missing == "" {
		return
	}
	if os.Getenv("TIERFS_REQUIRE_FUSE") == "1" {
		t.Fatalf("FUSE is required but %s is missing", missing)
	}
	t.Skipf("no FUSE here (%s missing)", missing)
}

func startFuse(t *testing.T, mod func(*Config)) *fuseEnv {
	t.Helper()
	requireFuse(t)
	data := bigData()
	root := t.TempDir()
	e := &fuseEnv{t: t, data: data}
	e.mnt = filepath.Join(root, "mnt")
	e.mdir = filepath.Join(root, "manifests")
	e.events = filepath.Join(root, "events", "events.jsonl")
	for _, d := range []string{e.mnt, e.mdir, filepath.Join(root, "stubs")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	e.m = buildManifest(int64(len(data)), filepath.Join(root, "stubs", testID+".mkv"))
	writeStub(t, e.m, data)
	writeChunks(t, e.mdir, e.m, data)
	writeManifest(t, e.mdir, e.m)
	e.rs = newRangeServer(t, data)

	cfg := Config{
		Mount: e.mnt, Manifests: e.mdir, EventsPath: e.events, HTTPAddr: "127.0.0.1:0",
		PollInterval: time.Hour, // tests call Reload themselves
		StubRoots:    []string{filepath.Join(root, "stubs")},
		Remote: RemoteConfig{
			Base: e.rs.URL, User: testUser, Pass: testPass, ReadAhead: 16 << 20, MaxStreams: 8,
		},
	}
	if mod != nil {
		mod(&cfg)
	}
	srv, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	e.srv = srv
	t.Cleanup(func() {
		var err error
		for i := 0; i < 20; i++ {
			if err = srv.Close(); err == nil {
				return
			}
			time.Sleep(250 * time.Millisecond)
		}
		t.Errorf("unmount: %v", err)
	})
	e.path = filepath.Join(e.mnt, e.m.Name())
	return e
}

func (e *fuseEnv) open() *os.File {
	e.t.Helper()
	f, err := os.Open(e.path)
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { f.Close() })
	return f
}

func readEvents(t *testing.T, path string) []Event {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []Event
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var ev Event
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			t.Fatalf("bad event line %q: %v", sc.Text(), err)
		}
		out = append(out, ev)
	}
	return out
}

func countEv(evs []Event, kind string) int {
	n := 0
	for _, e := range evs {
		if e.Ev == kind {
			n++
		}
	}
	return n
}

func TestFuseStatMatchesManifest(t *testing.T) {
	e := startFuse(t, nil)
	fi, err := os.Stat(e.path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Size() != e.m.Size {
		t.Errorf("size %d, manifest %d", fi.Size(), e.m.Size)
	}
	if fi.ModTime().UnixNano() != e.m.MtimeNS {
		t.Errorf("mtime %d, manifest %d", fi.ModTime().UnixNano(), e.m.MtimeNS)
	}
	if fi.Mode().Perm() != 0o444 || !fi.Mode().IsRegular() {
		t.Errorf("mode %v, want regular 0444", fi.Mode())
	}
	if st := fi.Sys().(*syscall.Stat_t); st.Nlink != 1 {
		t.Errorf("nlink %d, want 1", st.Nlink)
	}
	ents, err := os.ReadDir(e.mnt)
	if err != nil || len(ents) != 1 || ents[0].Name() != e.m.Name() {
		t.Fatalf("readdir = %v, %v", ents, err)
	}
	if _, err := os.OpenFile(e.path, os.O_WRONLY, 0); err == nil {
		t.Error("opened for writing")
	}
	if e.rs.reqCount() != 0 {
		t.Error("stat caused remote requests")
	}
}

func TestFuseFullReadMatchesSHA256(t *testing.T) {
	e := startFuse(t, nil)
	want := sha256.Sum256(e.data)
	f := e.open()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		t.Fatal(err)
	}
	if n != e.m.Size {
		t.Fatalf("read %d bytes, want %d", n, e.m.Size)
	}
	if hex.EncodeToString(h.Sum(nil)) != hex.EncodeToString(want[:]) {
		t.Fatal("sha256 differs from the original")
	}
	if got := e.rs.reqCount(); got != 1 {
		t.Errorf("a sequential read made %d remote requests, want 1", got)
	}
	if e.rs.unauth.Load() != 0 {
		t.Error("remote saw unauthenticated requests")
	}
	m := e.srv.Metrics()
	mid := e.m.MidEnd() - e.m.HeadLen
	if m.BytesRemote.Load() != mid || m.BytesLocal.Load() != e.m.Size-mid {
		t.Errorf("metrics local=%d remote=%d, want %d and %d", m.BytesLocal.Load(), m.BytesRemote.Load(), e.m.Size-mid, mid)
	}
}

func TestFuseHeadOnlyAndTailOnlyNeverTouchTheRemote(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	// Read the first half of the head, below the pre-warm point.
	buf := make([]byte, 1<<20)
	for off := int64(0); off+int64(len(buf)) <= e.m.HeadLen/2; off += int64(len(buf)) {
		if _, err := f.ReadAt(buf, off); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(buf, e.data[off:off+int64(len(buf))]) {
			t.Fatalf("head bytes differ at %d", off)
		}
	}
	// Read the whole tail, like a player looking for the index.
	tail := make([]byte, e.m.TailLen)
	if _, err := f.ReadAt(tail, e.m.Size-e.m.TailLen); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(tail, e.data[e.m.Size-e.m.TailLen:]) {
		t.Fatal("tail bytes differ")
	}
	// A read that ends exactly at EOF, and one past it.
	last := make([]byte, 100)
	if n, err := f.ReadAt(last, e.m.Size-50); n != 50 || err != io.EOF {
		t.Fatalf("read across EOF gave n=%d err=%v", n, err)
	}
	time.Sleep(300 * time.Millisecond)
	if got := e.rs.reqCount(); got != 0 {
		t.Fatalf("head/tail reads made %d remote requests, want 0", got)
	}
	f.Close()
	waitFor(t, 5*time.Second, "close event", func() bool { return countEv(readEvents(t, e.events), "close") == 1 })
	evs := readEvents(t, e.events)
	if countEv(evs, "remote_read") != 0 {
		t.Error("remote_read emitted for a local-only handle")
	}
}

func TestFusePrewarmStartsBeforeReaderReachesHeadLen(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	buf := make([]byte, 1<<20)
	var off int64
	readTo := func(limit int64) {
		for off < limit {
			n := min(int64(len(buf)), limit-off)
			if _, err := f.ReadAt(buf[:n], off); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(buf[:n], e.data[off:off+n]) {
				t.Fatalf("wrong bytes at %d", off)
			}
			off += n
		}
	}
	readTo(e.m.HeadLen / 2)
	time.Sleep(200 * time.Millisecond)
	if e.rs.reqCount() != 0 {
		t.Fatal("stream started before 0.75 * head_len")
	}
	readTo(e.m.HeadLen * 4 / 5)
	waitFor(t, 3*time.Second, "pre-warm request", func() bool { return e.rs.reqCount() >= 1 })
	if off >= e.m.HeadLen {
		t.Fatal("test bug: reader already past head_len")
	}
	if st := e.rs.startOffsets(); len(st) != 1 || st[0] != e.m.HeadLen {
		t.Fatalf("pre-warm range starts = %v, want [%d]", st, e.m.HeadLen)
	}
	// No remote byte has been delivered to the reader yet.
	if e.srv.Metrics().BytesRemote.Load() != 0 {
		t.Error("pre-warm counted as remote bytes read")
	}
	// Reading on into the middle continues from the warm stream.
	readTo(e.m.HeadLen + 4<<20)
	if e.rs.reqCount() != 1 {
		t.Errorf("crossing into the middle made %d requests, want 1", e.rs.reqCount())
	}
}

func TestFuseSeekCancelsStaleRequest(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	off1 := e.m.HeadLen + 10<<20
	off2 := e.m.HeadLen + 60<<20 // far beyond the 16 MiB read-ahead
	p := make([]byte, 4096)
	if _, err := f.ReadAt(p, off1); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p, e.data[off1:off1+4096]) {
		t.Fatal("wrong bytes at first offset")
	}
	if e.rs.reqCount() != 1 {
		t.Fatalf("requests = %d, want 1", e.rs.reqCount())
	}
	if _, err := f.ReadAt(p, off2); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p, e.data[off2:off2+4096]) {
		t.Fatal("wrong bytes after seek")
	}
	waitFor(t, 5*time.Second, "server to see the stale request cancelled", func() bool { return e.rs.cancelled.Load() >= 1 })
	if e.rs.reqCount() != 2 {
		t.Errorf("requests = %d, want 2", e.rs.reqCount())
	}
	// Seek backwards, too.
	if _, err := f.ReadAt(p, e.m.HeadLen+1<<20); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p, e.data[e.m.HeadLen+1<<20:e.m.HeadLen+1<<20+4096]) {
		t.Fatal("wrong bytes after backwards seek")
	}
	waitFor(t, 5*time.Second, "second cancel", func() bool { return e.rs.cancelled.Load() >= 2 })
}

func TestFuseConcurrentReaders(t *testing.T) {
	e := startFuse(t, nil)
	starts := []int64{0, e.m.HeadLen - 3<<20, e.m.HeadLen + 50<<20, e.m.MidEnd() - 8<<20}
	var wg sync.WaitGroup
	errs := make(chan error, len(starts))
	for i, start := range starts {
		wg.Add(1)
		go func(i int, start int64) {
			defer wg.Done()
			f, err := os.Open(e.path)
			if err != nil {
				errs <- err
				return
			}
			defer f.Close()
			buf := make([]byte, 512<<10+i*4097) // odd sizes on purpose
			off := start
			for done := int64(0); done < 24<<20 && off < e.m.Size; {
				n, err := f.ReadAt(buf, off)
				if err != nil && err != io.EOF {
					errs <- err
					return
				}
				if !bytes.Equal(buf[:n], e.data[off:off+int64(n)]) {
					errs <- errors.New("reader got wrong bytes")
					return
				}
				off += int64(n)
				done += int64(n)
				if n == 0 {
					break
				}
			}
		}(i, start)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
}

func TestFuseRemoteDownGivesEIONotZeros(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	// Head and tail still work with the remote gone.
	p := make([]byte, 64<<10)
	e.rs.Close()
	if _, err := f.ReadAt(p, 0); err != nil || !bytes.Equal(p, e.data[:len(p)]) {
		t.Fatalf("head read with remote down: %v", err)
	}
	off := e.m.HeadLen + 20<<20
	for i := range p {
		p[i] = 0xEE
	}
	t0 := time.Now()
	n, err := f.ReadAt(p, off)
	el := time.Since(t0)
	if !errors.Is(err, syscall.EIO) {
		t.Fatalf("got n=%d err=%v, want EIO", n, err)
	}
	if n != 0 {
		t.Errorf("returned %d bytes along with the error", n)
	}
	if el < 5*time.Second || el > 15*time.Second {
		t.Errorf("EIO after %v, want about 10s", el)
	}
	if e.srv.Metrics().RemoteErrors.Load() == 0 {
		t.Error("remote errors not counted")
	}
	// A read that spans head into the dead middle fails as a whole.
	span := make([]byte, 2<<20)
	if _, err := f.ReadAt(span, e.m.HeadLen-1<<20); err == nil {
		t.Error("read crossing into the dead middle succeeded")
	}
}

func TestFuseSyncReadEmitsOneRemoteReadAndClose(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	if n, err := io.Copy(io.Discard, f); err != nil || n != e.m.Size {
		t.Fatalf("copy: %d, %v", n, err)
	}
	f.Close()
	waitFor(t, 5*time.Second, "close event", func() bool { return countEv(readEvents(t, e.events), "close") >= 1 })
	evs := readEvents(t, e.events)
	if countEv(evs, "open") != 1 || countEv(evs, "remote_read") != 1 || countEv(evs, "close") != 1 {
		t.Fatalf("events = %+v, want one open, one remote_read, one close", evs)
	}
	var rr, cl Event
	for _, ev := range evs {
		switch ev.Ev {
		case "remote_read":
			rr = ev
		case "close":
			cl = ev
		}
		if ev.ID != e.m.ID || ev.TS < 1e9 {
			t.Errorf("bad event %+v", ev)
		}
	}
	if rr.TS > cl.TS {
		t.Error("remote_read after close")
	}
	mid := e.m.MidEnd() - e.m.HeadLen
	if cl.BytesRemote != mid || cl.BytesLocal != e.m.Size-mid {
		t.Errorf("close totals local=%d remote=%d, want %d and %d", cl.BytesLocal, cl.BytesRemote, e.m.Size-mid, mid)
	}
	if rr.BytesRemote <= 0 || rr.BytesRemote > cl.BytesRemote {
		t.Errorf("remote_read bytes_remote = %d", rr.BytesRemote)
	}
}

func TestFuseRemovedManifestKeepsOpenHandle(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	p := make([]byte, 1<<20)
	if _, err := f.ReadAt(p, 0); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(e.mdir, e.m.ID+".json")); err != nil {
		t.Fatal(err)
	}
	e.srv.Reload()
	waitFor(t, 5*time.Second, "entry to disappear", func() bool {
		_, err := os.Stat(e.path)
		return errors.Is(err, os.ErrNotExist)
	})
	if ents, err := os.ReadDir(e.mnt); err != nil || len(ents) != 0 {
		t.Fatalf("readdir after removal = %v, %v", ents, err)
	}
	if _, err := os.Open(e.path); err == nil {
		t.Fatal("new open succeeded after the manifest was removed")
	}
	// The old handle still reads head, middle and tail.
	for _, off := range []int64{4 << 20, e.m.HeadLen + 30<<20, e.m.Size - 1<<20} {
		if _, err := f.ReadAt(p, off); err != nil {
			t.Fatalf("read at %d after removal: %v", off, err)
		}
		if !bytes.Equal(p, e.data[off:off+int64(len(p))]) {
			t.Fatalf("wrong bytes at %d after removal", off)
		}
	}
	if fi, err := f.Stat(); err != nil || fi.Size() != e.m.Size {
		t.Fatalf("fstat after removal: %v, %v", fi, err)
	}
}

func TestFuseNewManifestAppearsOnReload(t *testing.T) {
	e := startFuse(t, nil)
	m2 := *e.m
	m2.ID, m2.Ext = "ffeeddccbbaa9988", ".mp4"
	m2.StubPath = filepath.Join(filepath.Dir(e.m.StubPath), m2.ID+".mp4")
	writeStub(t, &m2, e.data)
	writeChunks(t, e.mdir, &m2, e.data)
	os.WriteFile(filepath.Join(e.mdir, "junk.json"), []byte("not json"), 0o644)
	writeManifest(t, e.mdir, &m2)
	e.srv.Reload()
	waitFor(t, 5*time.Second, "new entry", func() bool {
		_, err := os.Stat(filepath.Join(e.mnt, m2.Name()))
		return err == nil
	})
	ents, _ := os.ReadDir(e.mnt)
	if len(ents) != 2 {
		t.Fatalf("readdir = %v", ents)
	}
}

func TestFuseStatusEndpoints(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	buf := make([]byte, 1<<20)
	f.ReadAt(buf, e.m.HeadLen+1<<20)
	get := func(path string) (int, string) {
		resp, err := http.Get("http://" + e.srv.HTTPAddr() + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}
	if code, body := get("/healthz"); code != 200 || !bytes.Contains([]byte(body), []byte("ok")) {
		t.Fatalf("healthz = %d %q", code, body)
	}
	code, body := get("/metrics")
	if code != 200 {
		t.Fatalf("metrics = %d", code)
	}
	for _, want := range []string{
		"tierfs_bytes_local_total", "tierfs_bytes_remote_total", "tierfs_open_handles 1",
		"tierfs_active_streams 1", "tierfs_remote_errors_total 0", "tierfs_remote_ttfb_seconds_count 1",
	} {
		if !bytes.Contains([]byte(body), []byte(want)) {
			t.Errorf("metrics missing %q:\n%s", want, body)
		}
	}
}

func (e *fuseEnv) chunkOff(k int64) int64 { return e.m.HeadLen + k*e.m.ChunkSize }

func (e *fuseEnv) readAtMust(f *os.File, off int64, n int) {
	e.t.Helper()
	p := make([]byte, n)
	if _, err := f.ReadAt(p, off); err != nil {
		e.t.Fatalf("read at %d: %v", off, err)
	}
	if !bytes.Equal(p, e.data[off:off+int64(n)]) {
		e.t.Fatalf("data at %d differs", off)
	}
}

func TestFuseZeroFilledChunkGivesEIOAndEvent(t *testing.T) {
	e := startFuse(t, nil)
	bad := e.chunkOff(3)
	e.rs.zero(bad, bad+e.m.ChunkSize, 1000)
	f := e.open()
	e.readAtMust(f, e.chunkOff(2), 64<<10)
	p := make([]byte, 64<<10)
	for i := range p {
		p[i] = 0xEE
	}
	n, err := f.ReadAt(p, bad+100)
	if !errors.Is(err, syscall.EIO) || n != 0 {
		t.Fatalf("got n=%d err=%v, want EIO and no bytes", n, err)
	}
	for _, b := range p {
		if b == 0 {
			t.Fatal("zeros were returned")
		}
	}
	waitFor(t, 5*time.Second, "bad_chunk event", func() bool { return countEv(readEvents(t, e.events), "bad_chunk") >= 1 })
	for _, ev := range readEvents(t, e.events) {
		if ev.Ev == "bad_chunk" && (ev.Offset == nil || *ev.Offset != bad || ev.ID != testID) {
			t.Fatalf("bad_chunk event %+v, want offset %d", ev, bad)
		}
	}
	if e.srv.Metrics().BadChunks.Load() == 0 {
		t.Error("bad chunks not counted")
	}
	// Other chunks still read, with no waiting out a cooldown.
	t0 := time.Now()
	e.readAtMust(f, e.chunkOff(5)+12345, 100<<10)
	e.readAtMust(f, e.chunkOff(1), 100<<10)
	e.readAtMust(f, e.chunkOff(3)-4096, 4096)
	if time.Since(t0) > 3*time.Second {
		t.Error("reads of good chunks were slow after a bad chunk")
	}
}

func TestFuseTransientBadChunkIsRefetched(t *testing.T) {
	e := startFuse(t, nil)
	bad := e.chunkOff(3)
	e.rs.zero(bad, bad+e.m.ChunkSize, 1)
	f := e.open()
	before := e.rs.reqCount()
	e.readAtMust(f, bad+777, 256<<10)
	if got := e.rs.reqCount() - before; got != 2 {
		t.Errorf("%d remote requests, want 2 (one refetch)", got)
	}
	if n := countEv(readEvents(t, e.events), "bad_chunk"); n != 0 {
		t.Errorf("%d bad_chunk events for a chunk that verified on refetch", n)
	}
}

func TestFuseLastShortChunkVerifies(t *testing.T) {
	e := startFuse(t, nil)
	mid := e.m.MidEnd() - e.m.HeadLen
	last := mid / e.m.ChunkSize * e.m.ChunkSize
	if mid%e.m.ChunkSize == 0 {
		t.Fatal("test data has no short last chunk")
	}
	f := e.open()
	e.readAtMust(f, e.m.MidEnd()-5000, 5000)
	// A zeroed short chunk must fail too.
	e.rs.zero(e.m.HeadLen+last, e.m.MidEnd(), 1000)
	f2 := e.open()
	p := make([]byte, 5000)
	if _, err := f2.ReadAt(p, e.m.MidEnd()-5000); !errors.Is(err, syscall.EIO) {
		t.Fatalf("zeroed last chunk: err=%v, want EIO", err)
	}
}

func TestFuseBadChunkListHidesEntry(t *testing.T) {
	e := startFuse(t, nil)
	good, err := os.ReadFile(filepath.Join(e.mdir, testID+".chunks"))
	if err != nil {
		t.Fatal(err)
	}
	mk := func(id string, chunks []byte) string {
		m2 := *e.m
		m2.ID = id
		m2.StubPath = filepath.Join(filepath.Dir(e.m.StubPath), id+".mkv")
		writeStub(t, &m2, e.data)
		writeChunks(t, e.mdir, &m2, e.data)
		if chunks != nil {
			os.WriteFile(filepath.Join(e.mdir, id+".chunks"), chunks, 0o644)
		}
		writeManifest(t, e.mdir, &m2)
		return m2.Name()
	}
	flipped := append([]byte(nil), good...)
	flipped[40] ^= 1
	names := []string{
		mk("a000000000000001", good[:len(good)-32]), // cut short
		mk("a000000000000002", flipped),             // wrong content
		mk("a000000000000003", nil),                 // fine, control
	}
	names = append(names, mk("a000000000000004", nil))
	os.Remove(filepath.Join(e.mdir, "a000000000000004.chunks")) // missing list
	e.srv.Reload()
	waitFor(t, 5*time.Second, "control entry", func() bool {
		_, err := os.Stat(filepath.Join(e.mnt, names[2]))
		return err == nil
	})
	for _, i := range []int{0, 1, 3} {
		if _, err := os.Stat(filepath.Join(e.mnt, names[i])); err == nil {
			t.Errorf("%s with a bad chunk list is visible", names[i])
		}
	}
	// A list that goes bad after the entry was valid keeps the old entry.
	os.WriteFile(filepath.Join(e.mdir, testID+".chunks"), good[:100], 0o644)
	e.srv.Reload()
	if _, err := os.Stat(e.path); err != nil {
		t.Errorf("previously valid entry vanished: %v", err)
	}
}
