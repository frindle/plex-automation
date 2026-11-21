package tierfs

// Proof tests for the PR #81 review. None of them mount FUSE: they drive the
// handle and the go-fuse node bridge directly, so they run without /dev/fuse.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

type rvEnv struct {
	s     *Server
	stubs string
	m     *Manifest
	mdir  string
	data  []byte
}

func rvSetup(t *testing.T, data []byte, base string, rc func(*RemoteConfig), mm func(*Manifest)) *rvEnv {
	t.Helper()
	root := t.TempDir()
	mdir := filepath.Join(root, "manifests")
	stubs := filepath.Join(root, "stubs")
	for _, d := range []string{mdir, stubs} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	m := buildManifest(int64(len(data)), filepath.Join(stubs, testID+".mkv"))
	writeStub(t, m, data)
	writeChunks(t, mdir, m, data)
	if mm != nil {
		mm(m)
	}
	writeManifest(t, mdir, m)
	cfg := RemoteConfig{Base: base, User: testUser, Pass: testPass, ReadAhead: 16 << 20, MaxStreams: 8, Window: 1500 * time.Millisecond}
	if rc != nil {
		rc(&cfg)
	}
	s, err := New(Config{Mount: "/unused", Manifests: mdir, StubRoots: []string{stubs}, PollInterval: time.Hour, Remote: cfg})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.remote.Close() })
	s.Reload()
	return &rvEnv{s: s, m: m, mdir: mdir, data: data, stubs: stubs}
}

func (e *rvEnv) open(t *testing.T) *handle {
	t.Helper()
	en := e.s.get(e.m.Name())
	if en == nil {
		t.Fatal("entry not loaded")
	}
	h, err := e.s.openHandle(en)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h.Release(context.Background()) })
	return h
}

func (e *rvEnv) read(h *handle, off, n int64) ([]byte, syscall.Errno) {
	dest := make([]byte, n)
	for i := range dest {
		dest[i] = 0xA5 // poison: any byte not written by tierfs shows up
	}
	rr, errno := h.Read(context.Background(), dest, off)
	if errno != 0 {
		return nil, errno
	}
	b, st := rr.Bytes(nil)
	if !st.Ok() {
		return nil, syscall.EIO
	}
	return b, 0
}

// 1. Many goroutines on ONE handle, random and boundary reads, one chunk that
// the remote always zero-fills. No read may return a wrong byte; reads that
// touch the bad chunk must fail; the rest must succeed.
func TestHandleConcurrentSameHandleNeverWrongBytes(t *testing.T) {
	data := randomData(120<<20+12345, 11)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, nil)
	m := e.m
	badLo := m.HeadLen + 5*m.ChunkSize
	badHi := badLo + m.ChunkSize
	rs.zero(badLo, badHi, 1<<30)
	h := e.open(t)

	var cands []int64
	for k := int64(0); k <= m.NumChunks(); k++ {
		b := min(m.HeadLen+k*m.ChunkSize, m.MidEnd())
		cands = append(cands, b-1, b, b+1)
	}
	cands = append(cands, 0, m.HeadLen*3/4, m.MidEnd()-1, m.MidEnd(), m.Size-1, (m.MidEnd()/(1<<20))*(1<<20))

	var wg sync.WaitGroup
	var mu sync.Mutex
	var wrong, badOK, goodFail, total int
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			r := rand.New(rand.NewPCG(uint64(g), 99))
			for i := 0; i < 250; i++ {
				var off int64
				if r.IntN(3) == 0 {
					off = cands[r.IntN(len(cands))]
				} else {
					off = r.Int64N(m.Size)
				}
				n := int64(1 + r.IntN(1<<20+4097))
				if r.IntN(50) == 0 {
					n = 40 << 20 // a huge split read across head/mid/tail
				}
				got, errno := e.read(h, off, n)
				end := min(off+n, m.Size)
				touchesBad := off < badHi && end > badLo
				mu.Lock()
				total++
				switch {
				case errno == 0 && !bytes.Equal(got, data[off:end]):
					wrong++
				case errno == 0 && touchesBad:
					badOK++
				case errno != 0 && !touchesBad:
					goodFail++
				}
				mu.Unlock()
			}
		}(g)
	}
	wg.Wait()
	t.Logf("reads=%d wrong=%d bad-chunk-served=%d good-read-failed=%d", total, wrong, badOK, goodFail)
	if wrong > 0 || badOK > 0 {
		t.Fatalf("returned unverified bytes: wrong=%d badOK=%d", wrong, badOK)
	}
}

// 2. A remote that misbehaves in every way the brief lists. A middle read
// must return the right bytes or EIO, never anything else, and fail quickly.
func TestHandleAdversarialRemote(t *testing.T) {
	data := randomData(80<<20+4321, 12)
	total := int64(len(data))
	modes := []string{"200", "wrongstart", "shift1", "chunked-long", "chunked-short", "garbage", "zeros", "hang", "trickle", "len-minus-1", "no-content-range"}
	for _, mode := range modes {
		t.Run(mode, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				a, b, ok := parseRange(r.Header.Get("Range"))
				if !ok {
					http.Error(w, "range", 400)
					return
				}
				b = min(b, total-1)
				body := append([]byte(nil), data[a:b+1]...)
				cr := fmt.Sprintf("bytes %d-%d/%d", a, b, total)
				switch mode {
				case "200":
					w.WriteHeader(200)
					w.Write(data)
					return
				case "wrongstart":
					cr = fmt.Sprintf("bytes %d-%d/%d", a+4096, b, total)
					body = data[a+4096 : b+1]
				case "shift1":
					body = data[a+1 : b+2]
				case "garbage":
					copy(body, randomData(len(body), 77))
				case "zeros":
					clear(body)
				case "hang":
					<-r.Context().Done()
					return
				case "trickle":
					w.Header().Set("Content-Range", cr)
					w.WriteHeader(206)
					for i := 0; i < 1000; i++ {
						if _, err := w.Write(body[i*100 : i*100+100]); err != nil {
							return
						}
						w.(http.Flusher).Flush()
						select {
						case <-time.After(50 * time.Millisecond):
						case <-r.Context().Done():
							return
						}
					}
					return
				case "len-minus-1":
					w.Header().Set("Content-Length", fmt.Sprint(len(body)-1))
					body = body[:len(body)-1]
				case "no-content-range":
					cr = ""
				}
				if cr != "" {
					w.Header().Set("Content-Range", cr)
				}
				switch mode {
				case "chunked-long":
					w.WriteHeader(206)
					w.(http.Flusher).Flush()
					w.Write(body)
					w.Write(bytes.Repeat([]byte{0xEE}, 1<<20))
				case "chunked-short":
					w.WriteHeader(206)
					w.(http.Flusher).Flush()
					w.Write(body[:min(len(body), 3<<20)])
				default:
					if w.Header().Get("Content-Length") == "" {
						w.Header().Set("Content-Length", fmt.Sprint(len(body)))
					}
					w.WriteHeader(206)
					w.Write(body)
				}
			}))
			defer srv.Close()
			e := rvSetup(t, data, srv.URL, nil, nil)
			h := e.open(t)
			off := e.m.HeadLen + 3*e.m.ChunkSize + 777
			t0 := time.Now()
			got, errno := e.read(h, off, 300000)
			el := time.Since(t0)
			if errno == 0 && !bytes.Equal(got, data[off:off+300000]) {
				t.Fatalf("WRONG BYTES returned for mode %s", mode)
			}
			t.Logf("mode=%-16s errno=%v elapsed=%v", mode, errno, el.Round(10*time.Millisecond))
			if el > 6*time.Second {
				t.Errorf("took %v", el)
			}
		})
	}
}

func lookupOpen(raw fuse.RawFileSystem, name string) (uint64, fuse.Status, fuse.Status) {
	var out fuse.EntryOut
	st := raw.Lookup(nil, &fuse.InHeader{NodeId: 1}, name, &out)
	if !st.Ok() {
		return 0, st, st
	}
	var oo fuse.OpenOut
	ost := raw.Open(nil, &fuse.OpenIn{InHeader: fuse.InHeader{NodeId: out.NodeId}, Flags: syscall.O_RDONLY}, &oo)
	return out.NodeId, st, ost
}

// 4. The same id comes back (re-demote after a promote, or the manifest is
// rewritten in place) while the kernel still holds the old inode (an open
// file). go-fuse dedups on StableAttr{Ino}, so Lookup hands back the old node,
// whose entry pointer no longer matches, and every new open is ENOENT.
func TestHandleStaleInodeAfterManifestRewrite(t *testing.T) {
	for _, how := range []string{"rewrite-in-place", "remove-then-readd"} {
		t.Run(how, func(t *testing.T) {
			data := randomData(60<<20+99, 14)
			rs := newRangeServer(t, data)
			e := rvSetup(t, data, rs.URL, nil, nil)
			e.s.root = &rootNode{srv: e.s}
			raw := fs.NewNodeFS(e.s.root, &fs.Options{})
			name := e.m.Name()
			if _, _, ost := lookupOpen(raw, name); !ost.Ok() {
				t.Fatalf("first open: %v", ost)
			}
			// (no FORGET: the kernel still references the inode, as with an open file)
			p := filepath.Join(e.mdir, e.m.ID+".json")
			switch how {
			case "rewrite-in-place":
				var mm map[string]any
				b, _ := os.ReadFile(p)
				json.Unmarshal(b, &mm)
				b, _ = json.MarshalIndent(mm, "", " ") // same fields, new formatting
				os.WriteFile(p, b, 0o644)
				e.s.Reload()
				e.s.root.RmChild(name) // what Reload does when mounted
			case "remove-then-readd":
				b, _ := os.ReadFile(p)
				os.Remove(p)
				e.s.Reload()
				e.s.root.RmChild(name)
				os.WriteFile(p, b, 0o644)
				e.s.Reload()
			}
			if e.s.get(name) == nil {
				t.Fatal("entry not present after reload")
			}
			_, lst, ost := lookupOpen(raw, name)
			t.Logf("lookup=%v open=%v", lst, ost)
			if !ost.Ok() {
				t.Fatalf("file is listed but open fails with %v", ost)
			}
		})
	}
}

// 5. id ffffffffffffffff passes the id regexp and maps to the inode number
// go-fuse reserves, so Lookup panics.
func TestHandleReservedInodeID(t *testing.T) {
	data := randomData(60<<20+99, 15)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, func(m *Manifest) {})
	// rewrite under the all-f id
	old := e.m.ID
	e.m.ID = "ffffffffffffffff"
	os.Rename(filepath.Join(e.mdir, old+".chunks"), filepath.Join(e.mdir, e.m.ID+".chunks"))
	os.Remove(filepath.Join(e.mdir, old+".json"))
	writeManifest(t, e.mdir, e.m)
	e.s.Reload()
	if e.s.get(e.m.Name()) != nil {
		t.Fatal("the reserved id was loaded")
	}
}

// 6. Stream cap: one slot, handle A has read the middle and then pauses with
// the file open. Handle B gets EIO after Window+2s although A is idle.
func TestHandleSlotStarvationGivesEIO(t *testing.T) {
	data := randomData(80<<20+5, 16)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, func(c *RemoteConfig) { c.MaxStreams = 1 }, nil)
	a := e.open(t)
	if _, errno := e.read(a, e.m.HeadLen, 4096); errno != 0 {
		t.Fatal(errno)
	}
	b := e.open(t)
	t0 := time.Now()
	_, errno := e.read(b, e.m.HeadLen+20<<20, 4096)
	t.Logf("second handle: errno=%v after %v (A idle, holding the only slot)", errno, time.Since(t0).Round(100*time.Millisecond))
	if errno != 0 {
		t.Fatalf("an idle handle starved a new reader into EIO")
	}
}

var _ = errors.New
