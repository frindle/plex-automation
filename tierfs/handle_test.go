package tierfs

// Tests for the handle, the stub checks and the reload cache. None of them
// mount FUSE, so they also run under the race detector.

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestHandleStubPathIsConfined(t *testing.T) {
	data := randomData(100<<20, 13)
	rs := newRangeServer(t, data)
	for _, target := range []string{"/etc/passwd", "/proc/self/environ", "relative/x.mkv", "/elsewhere/" + testID + ".mkv"} {
		e := rvSetup(t, data, rs.URL, nil, func(m *Manifest) { m.StubPath = target })
		if e.s.get(e.m.Name()) != nil {
			t.Errorf("stub_path %q was accepted", target)
		}
	}
	// A path that only normalises to the right place is not "exactly" it.
	e := rvSetup(t, data, rs.URL, nil, func(m *Manifest) {
		m.StubPath = filepath.Dir(m.StubPath) + "/sub/../" + testID + ".mkv"
	})
	if e.s.get(e.m.Name()) != nil {
		t.Error("a stub_path with .. was accepted")
	}
	e = rvSetup(t, data, rs.URL, nil, func(m *Manifest) {
		m.StubPath = filepath.Join(filepath.Dir(m.StubPath), "other.mkv")
	})
	if e.s.get(e.m.Name()) != nil {
		t.Error("a stub_path with another name was accepted")
	}

	good := func(t *testing.T) *rvEnv {
		e := rvSetup(t, data, rs.URL, nil, nil)
		if e.s.get(e.m.Name()) == nil {
			t.Fatal("a normal manifest was refused")
		}
		return e
	}
	t.Run("symlink", func(t *testing.T) {
		e := good(t)
		real := filepath.Join(t.TempDir(), "real")
		if err := os.Rename(e.m.StubPath, real); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(real, e.m.StubPath); err != nil {
			t.Fatal(err)
		}
		if h, err := e.s.openHandle(e.s.get(e.m.Name())); err == nil {
			h.Release(context.Background())
			t.Fatal("opened a symlinked stub")
		}
	})
	t.Run("fifo", func(t *testing.T) {
		e := good(t)
		os.Remove(e.m.StubPath)
		if err := syscall.Mkfifo(e.m.StubPath, 0o644); err != nil {
			t.Skip(err)
		}
		done := make(chan error, 1)
		go func() {
			h, err := e.s.openHandle(e.s.get(e.m.Name()))
			if err == nil {
				h.Release(context.Background())
			}
			done <- err
		}()
		select {
		case err := <-done:
			if err == nil {
				t.Fatal("opened a FIFO")
			}
		case <-time.After(3 * time.Second):
			t.Fatal("opening a FIFO blocked")
		}
	})
	t.Run("wrong size", func(t *testing.T) {
		e := good(t)
		if err := os.Truncate(e.m.StubPath, e.m.Size-1); err != nil {
			t.Fatal(err)
		}
		if h, err := e.s.openHandle(e.s.get(e.m.Name())); err == nil {
			h.Release(context.Background())
			t.Fatal("opened a stub of the wrong size")
		}
	})
}

func TestHandleValidateLimits(t *testing.T) {
	base := func() *Manifest {
		m := buildManifest(100<<20, "/s/"+testID+".mkv")
		m.ChunksB3 = string(bytes.Repeat([]byte("a"), 64))
		return m
	}
	if err := base().Validate(); err != nil {
		t.Fatal(err)
	}
	for name, mod := range map[string]func(*Manifest){
		"overflow":       func(m *Manifest) { m.Size = 1 << 62; m.HeadLen = 1<<63 - 1; m.TailLen = 1 << 62 },
		"chunk too big":  func(m *Manifest) { m.ChunkSize = 32 << 20 },
		"reserved ino":   func(m *Manifest) { m.ID = "ffffffffffffffff" },
		"zero id":        func(m *Manifest) { m.ID = "0000000000000000" },
		"tail over size": func(m *Manifest) { m.TailLen = m.Size + 1 },
	} {
		m := base()
		mod(m)
		if err := m.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// An unchanged manifest and chunk list are not read or hashed again.
func TestHandleReloadSkipsUnchangedFiles(t *testing.T) {
	data := randomData(60<<20+99, 17)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, nil)
	before := chunkLoads.Load()
	for i := 0; i < 3; i++ {
		e.s.Reload()
	}
	if got := chunkLoads.Load() - before; got != 0 {
		t.Fatalf("%d chunk list reloads for unchanged files", got)
	}
	// A bad rewrite is noticed, and the old entry stays.
	if err := os.WriteFile(filepath.Join(e.mdir, e.m.ID+".chunks"), []byte("short"), 0o644); err != nil {
		t.Fatal(err)
	}
	e.s.Reload()
	if chunkLoads.Load() == before {
		t.Fatal("changed chunk list was not re-read")
	}
	if e.s.get(e.m.Name()) == nil {
		t.Fatal("old entry should stay when the new list is bad")
	}
}

// A slow remote that keeps sending must not turn into EIO.
func TestHandleSlowButSteadyRemoteSucceeds(t *testing.T) {
	data := randomData(80<<20+4321, 18)
	total := int64(len(data))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a, b, _ := parseRange(r.Header.Get("Range"))
		b = min(b, total-1)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, total))
		w.Header().Set("Content-Length", fmt.Sprint(b-a+1))
		w.WriteHeader(206)
		body := data[a : b+1]
		for len(body) > 0 {
			n := min(len(body), 64<<10)
			if _, err := w.Write(body[:n]); err != nil {
				return
			}
			w.(http.Flusher).Flush()
			body = body[n:]
			time.Sleep(30 * time.Millisecond) // a 4 MiB chunk takes ~2 s, above Window
		}
	}))
	defer srv.Close()
	e := rvSetup(t, data, srv.URL, func(c *RemoteConfig) { c.Window = time.Second }, nil)
	h := e.open(t)
	off := e.m.HeadLen + 2*e.m.ChunkSize + 5
	got, errno := e.read(h, off, 100000)
	if errno != 0 || !bytes.Equal(got, data[off:off+100000]) {
		t.Fatalf("errno=%v", errno)
	}
}

// Reads of other chunks do not fail fast after one chunk's remote error.
func TestHandleCooldownOnlyForFailedRegion(t *testing.T) {
	data := randomData(80<<20+4321, 19)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, func(c *RemoteConfig) { c.Window = 700 * time.Millisecond }, nil)
	h := e.open(t)
	rs.failFirst.Store(1000)
	if _, errno := e.read(h, e.m.HeadLen+2*e.m.ChunkSize+10, 1000); errno == 0 {
		t.Fatal("read succeeded against a failing remote")
	}
	rs.failFirst.Store(0)
	off := e.m.HeadLen + 7*e.m.ChunkSize
	t0 := time.Now()
	got, errno := e.read(h, off, 1000)
	if errno != 0 || !bytes.Equal(got, data[off:off+1000]) {
		t.Fatalf("read of a good chunk failed: %v after %v", errno, time.Since(t0))
	}
}

// The middle starts flowing near the end of the head, and starts again if the
// stream was lost.
func TestHandlePrewarmRestartsAfterStreamIsLost(t *testing.T) {
	data := randomData(80<<20+4321, 20)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, nil)
	h := e.open(t)
	near := e.m.HeadLen*3/4 + 4096
	if _, errno := e.read(h, near, 4096); errno != 0 {
		t.Fatal(errno)
	}
	waitFor(t, 3*time.Second, "first warm stream", func() bool { return rs.reqCount() == 1 })
	h.mu.Lock()
	h.dropStream() // as an idle-out would
	h.mu.Unlock()
	if _, errno := e.read(h, near, 4096); errno != 0 {
		t.Fatal(errno)
	}
	waitFor(t, 3*time.Second, "second warm stream", func() bool { return rs.reqCount() == 2 })
}
