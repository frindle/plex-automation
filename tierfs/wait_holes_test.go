package tierfs

// Player reads must never return the stub's unfilled holes as data, and the
// fill file view must cope with torn lines and replaced files.

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// With the manifest gone for some other reason (dir remounted empty, manual
// cleanup) the stub may still have holes: with no fill file the read is EIO.
func TestWaitManifestMissingWithHolesIsEIO(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	os.Remove(filepath.Join(w.mdir, w.m.ID+".json"))
	start := time.Now()
	off := w.m.HeadLen + 7*w.m.ChunkSize
	b, errno := w.readAs(ctxPid(playerPid), h, off, 4096)
	if errno != syscall.EIO {
		t.Fatalf("errno = %v (%d bytes), want EIO", errno, len(b))
	}
	if time.Since(start) > 3*time.Second {
		t.Error("EIO was not quick")
	}
	// Head and tail data are real and still served.
	if b, errno := w.readAs(ctxPid(playerPid), h, 0, 4096); errno != 0 || !bytes.Equal(b, w.data[:4096]) {
		t.Fatalf("head read: %v", errno)
	}
}

// With a fill file present the read waits for the chunk instead.
func TestWaitManifestMissingWithHolesWaitsForFill(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	off := w.m.HeadLen + 7*w.m.ChunkSize
	w.fillLine(t, fmt.Sprint(w.m.HeadLen)) // the file exists, chunk `off` is not listed
	os.Remove(filepath.Join(w.mdir, w.m.ID+".json"))
	type res struct {
		b []byte
		e syscall.Errno
	}
	done := make(chan res, 1)
	go func() { b, e := w.readAs(ctxPid(playerPid), h, off, 4096); done <- res{b, e} }()
	select {
	case r := <-done:
		t.Fatalf("read returned %v on a hole", r.e)
	case <-time.After(400 * time.Millisecond):
	}
	w.promoteChunk(t, off)
	r := <-done
	if r.e != 0 || !bytes.Equal(r.b, w.data[off:off+4096]) {
		t.Fatalf("after fill: %v", r.e)
	}
}

func TestWaitHoleInDetection(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	mid := w.m.HeadLen + 3*w.m.ChunkSize
	if !h.holeIn(mid, 4096) {
		t.Fatal("an unfilled middle is a hole")
	}
	if h.holeIn(0, 4096) {
		t.Fatal("the head is data")
	}
	w.promoteChunk(t, mid)
	if h.holeIn(mid, 4096) || h.holeIn(mid, w.m.ChunkSize) {
		t.Fatal("a written chunk is data")
	}
	if !h.holeIn(mid, w.m.ChunkSize+1) {
		t.Fatal("a range reaching into the next, unwritten chunk has a hole")
	}
}

func TestFillViewIgnoresPartialLine(t *testing.T) {
	p := filepath.Join(t.TempDir(), "x.fill")
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	v := newFillView(p)
	f.WriteString("4194304\n8388")
	v.refresh(time.Now())
	if !v.has(4194304) || v.has(8388) {
		t.Fatalf("partial line counted: %v", v.have)
	}
	f.WriteString("608\nfail")
	v.refresh(time.Now())
	if !v.has(8388608) || v.has(8388) || v.isFailed() {
		t.Fatalf("after completing the line: %v failed=%v", v.have, v.isFailed())
	}
	f.WriteString("ed 8388608\n")
	v.refresh(time.Now())
	if !v.isFailed() {
		t.Fatal("completed failed line not seen")
	}
}

func TestFillViewReplacedFileStartsAgain(t *testing.T) {
	d := t.TempDir()
	p := filepath.Join(d, "x.fill")
	os.WriteFile(p, []byte("100\n200\nfailed 300\n"), 0o644)
	v := newFillView(p)
	v.refresh(time.Now())
	if !v.has(100) || !v.has(200) || !v.isFailed() {
		t.Fatal("first read")
	}
	// Replaced by a new file that is longer than the old read position, so
	// only the inode tells it apart.
	tmp := filepath.Join(d, "tmp")
	os.WriteFile(tmp, []byte("5000\n6000\n7000\n8000\n9000\n"), 0o644)
	if err := os.Rename(tmp, p); err != nil {
		t.Fatal(err)
	}
	v.refresh(time.Now())
	if v.has(100) || v.has(200) || v.isFailed() || !v.has(5000) || !v.has(9000) {
		t.Fatalf("view kept the old file: have=%v failed=%v", v.have, v.isFailed())
	}
}
