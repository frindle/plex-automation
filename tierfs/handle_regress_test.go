package tierfs

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// GETATTR on an open file must keep working after its manifest is removed
// (promote while a player holds the file), with or without the fh flag.
func TestServerGetattrAfterRemovalWithOpenFile(t *testing.T) {
	data := randomData(60<<20+99, 31)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, nil)
	e.s.root = &rootNode{srv: e.s}
	raw := fs.NewNodeFS(e.s.root, &fs.Options{})
	name := e.m.Name()
	var eo fuse.EntryOut
	if st := raw.Lookup(nil, &fuse.InHeader{NodeId: 1}, name, &eo); !st.Ok() {
		t.Fatal(st)
	}
	var oo fuse.OpenOut
	if st := raw.Open(nil, &fuse.OpenIn{InHeader: fuse.InHeader{NodeId: eo.NodeId}, Flags: syscall.O_RDONLY}, &oo); !st.Ok() {
		t.Fatal(st)
	}
	getattr := func(withFh bool) (fuse.Status, uint64) {
		in := &fuse.GetAttrIn{InHeader: fuse.InHeader{NodeId: eo.NodeId}}
		if withFh {
			in.Flags_ = fuse.FUSE_GETATTR_FH
			in.Fh_ = oo.Fh
		}
		var ao fuse.AttrOut
		st := raw.GetAttr(nil, in, &ao)
		return st, ao.Size
	}
	os.Remove(filepath.Join(e.mdir, e.m.ID+".json"))
	e.s.Reload()
	e.s.root.RmChild(name)
	for _, withFh := range []bool{false, true} {
		st, size := getattr(withFh)
		if !st.Ok() || int64(size) != e.m.Size {
			t.Fatalf("GETATTR (fh=%v) after removal = %v, size %d", withFh, st, size)
		}
	}
	buf := make([]byte, 4096)
	off := e.m.HeadLen + 5<<20
	rr, rst := raw.Read(nil, &fuse.ReadIn{InHeader: fuse.InHeader{NodeId: eo.NodeId, Caller: fuse.Caller{Pid: scanPid}}, Fh: oo.Fh, Offset: uint64(off), Size: 4096}, buf)
	if !rst.Ok() {
		t.Fatal(rst)
	}
	if b, _ := rr.Bytes(buf); !bytes.Equal(b, data[off:off+4096]) {
		t.Fatal("wrong bytes after removal")
	}
}

// Two handles reading the last quarter of the head under a cap of 1 must not
// make the warm streams evict each other and refetch the read-ahead forever.
func TestHandleWarmStreamsDoNotThrash(t *testing.T) {
	data := randomData(200<<20+7, 36)
	var served atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a, b, _ := parseRange(r.Header.Get("Range"))
		b = min(b, int64(len(data))-1)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(data)))
		w.Header().Set("Content-Length", fmt.Sprint(b-a+1))
		w.WriteHeader(206)
		for off := a; off <= b; off += 64 << 10 {
			n, err := w.Write(data[off:min(off+64<<10, b+1)])
			served.Add(int64(n))
			if err != nil {
				return
			}
		}
	}))
	defer srv.Close()
	e := rvSetup(t, data, srv.URL, func(c *RemoteConfig) { c.MaxStreams = 1; c.ReadAhead = 32 << 20 }, nil)
	var stop atomic.Bool
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		h := e.open(t)
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			off := e.m.HeadLen*3/4 + int64(i)*4096
			for !stop.Load() && off+65536 < e.m.HeadLen {
				e.read(h, off, 65536)
				off += 65536
				time.Sleep(100 * time.Millisecond)
			}
		}(i)
	}
	time.Sleep(10 * time.Second)
	stop.Store(true)
	wg.Wait()
	if got := served.Load(); got > 3*32<<20 {
		t.Fatalf("warm streams thrash: %d MiB fetched while readers used none", got>>20)
	}
}

// A reader that attaches to a warm stream still queued for a slot gets the
// full SlotWait, and a warm stream no reader has touched never times out.
func TestHandleWarmQueuedReaderGetsFullSlotWait(t *testing.T) {
	data := randomData(80<<20+5, 37)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, func(c *RemoteConfig) { c.MaxStreams = 1; c.SlotWait = 3 * time.Second }, nil)
	a := e.open(t)
	if _, errno := e.read(a, e.m.HeadLen, 4096); errno != 0 {
		t.Fatal(errno)
	}
	stopCh := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { // A keeps the only slot busy
		defer wg.Done()
		for off := e.m.HeadLen + 4096; off < e.m.MidEnd()-4096; off += 4096 {
			select {
			case <-stopCh:
				return
			default:
			}
			if _, errno := e.read(a, off, 4096); errno != 0 {
				return
			}
			time.Sleep(15 * time.Millisecond)
		}
	}()
	defer func() { close(stopCh); wg.Wait() }()
	b := e.open(t)
	e.read(b, e.m.HeadLen*3/4+4096, 4096) // starts B's warm stream, queued
	time.Sleep(3500 * time.Millisecond)   // longer than SlotWait with no reader
	if n := e.s.m.SlotTimeouts.Load(); n != 0 {
		t.Fatalf("an unread warm stream counted %d slot timeouts", n)
	}
	t0 := time.Now()
	_, errno := e.read(b, e.m.HeadLen, 4096)
	el := time.Since(t0)
	if errno == 0 {
		t.Fatal("expected EIO while the only slot is held")
	}
	if el < 2500*time.Millisecond {
		t.Fatalf("reader got EIO after %v, want about SlotWait (3s)", el)
	}
	if n := e.s.m.SlotTimeouts.Load(); n != 1 {
		t.Fatalf("slot timeouts = %d, want 1", n)
	}
}
