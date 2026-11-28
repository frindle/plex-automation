package tierfs

// Tests for player reads that wait for the promote. None mount FUSE: the
// caller pid comes from a fake /proc and fuse.NewContext.

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// playerPid is a player that is not a Plex stream; streamPid is Plex's transcoder streaming to a client.
const (
	playerPid = 5000
	streamPid = 5001
)

type wEnv struct {
	*rvEnv
	events string
	rs     *rangeServer
}

func wSetup(t *testing.T) *wEnv {
	t.Helper()
	data := randomData(200<<20+777, 41)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, nil, nil)
	e.s.class = newClassifier(fakeProc(t, map[uint32][2]string{
		scanPid:   {"Plex Media Scan", "1"},
		playerPid: {"ffprobe", "1"},
		streamPid: {"Plex Transcoder", "1"},
		6000:      {"ffprobe", "4242"}, // child of the scanner
	}), DefaultBackgroundComms)
	e.s.cfg.FillPoll = 20 * time.Millisecond
	e.s.cfg.WaitTimeout = 20 * time.Second
	ev := filepath.Join(t.TempDir(), "events.jsonl")
	l, err := NewEventLog(ev, 0)
	if err != nil {
		t.Fatal(err)
	}
	e.s.ev = l
	t.Cleanup(l.Close)
	return &wEnv{rvEnv: e, events: ev, rs: rs}
}

func (w *wEnv) readAs(ctx context.Context, h *handle, off, n int64) ([]byte, syscall.Errno) {
	dest := make([]byte, n)
	rr, errno := h.Read(ctx, dest, off)
	if errno != 0 {
		return nil, errno
	}
	b, _ := rr.Bytes(nil)
	return append([]byte(nil), b...), 0
}

// promoteChunk does what the promote does for one chunk: bytes into the stub,
// then the offset line in the fill file.
func (w *wEnv) promoteChunk(t *testing.T, off int64) {
	t.Helper()
	f, err := os.OpenFile(w.m.StubPath, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	end := min(off+w.m.ChunkSize, w.m.MidEnd())
	if _, err := f.WriteAt(w.data[off:end], off); err != nil {
		t.Fatal(err)
	}
	f.Sync()
	f.Close()
	w.fillLine(t, fmt.Sprint(off))
}

func (w *wEnv) fillLine(t *testing.T, line string) {
	t.Helper()
	f, err := os.OpenFile(filepath.Join(w.mdir, w.m.ID+".fill"), os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	fmt.Fprintln(f, line)
}

func (w *wEnv) wants(t *testing.T) []int64 {
	var out []int64
	for _, e := range readEvents(t, w.events) {
		if e.Ev == "want" && e.Offset != nil {
			out = append(out, *e.Offset)
		}
	}
	return out
}

func TestWaitPlayerReadBlocksUntilFillLine(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	off := w.m.HeadLen + 3*w.m.ChunkSize
	type res struct {
		b   []byte
		err syscall.Errno
	}
	done := make(chan res, 1)
	go func() {
		b, errno := w.readAs(ctxPid(playerPid), h, off+100, 4096)
		done <- res{b, errno}
	}()
	select {
	case r := <-done:
		t.Fatalf("read returned before the fill line: %v", r.err)
	case <-time.After(400 * time.Millisecond):
	}
	if got := w.wants(t); len(got) != 1 || got[0] != off {
		t.Fatalf("want events = %v, want one at %d", got, off)
	}
	w.promoteChunk(t, off)
	r := <-done
	if r.err != 0 || !bytes.Equal(r.b, w.data[off+100:off+100+4096]) {
		t.Fatalf("read after fill: errno %v, equal=%v", r.err, bytes.Equal(r.b, w.data[off+100:off+100+4096]))
	}
	if w.rs.reqCount() != 0 {
		t.Errorf("a player read made %d remote requests", w.rs.reqCount())
	}
	if rr := countEv(readEvents(t, w.events), "remote_read"); rr != 0 {
		t.Errorf("remote_read events = %d", rr)
	}
}

func TestWaitReadSpanningTwoChunksNeedsBoth(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	c0 := w.m.HeadLen + w.m.ChunkSize
	off := c0 + w.m.ChunkSize - 100
	done := make(chan syscall.Errno, 1)
	go func() { _, e := w.readAs(ctxPid(playerPid), h, off, 200); done <- e }()
	w.promoteChunk(t, c0)
	select {
	case <-done:
		t.Fatal("returned with only the first chunk listed")
	case <-time.After(300 * time.Millisecond):
	}
	w.promoteChunk(t, c0+w.m.ChunkSize)
	if e := <-done; e != 0 {
		t.Fatal(e)
	}
}

func TestWaitBackgroundReaderUsesRemote(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	off := w.m.HeadLen + 2*w.m.ChunkSize
	for _, pid := range []uint32{scanPid, 6000} { // the scanner and its child
		b, errno := w.readAs(ctxPid(pid), h, off, 4096)
		if errno != 0 || !bytes.Equal(b, w.data[off:off+4096]) {
			t.Fatalf("pid %d: errno %v", pid, errno)
		}
	}
	if w.rs.reqCount() == 0 {
		t.Fatal("background read did not use the remote")
	}
	if len(w.wants(t)) != 0 {
		t.Error("a background read wrote a want event")
	}
	if countEv(readEvents(t, w.events), "remote_read") != 1 {
		t.Error("want one remote_read event")
	}
}

func TestWaitFailedLineGivesEIOAtOnce(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	off := w.m.HeadLen + 5*w.m.ChunkSize
	go func() {
		time.Sleep(150 * time.Millisecond)
		w.fillLine(t, fmt.Sprintf("failed %d", off))
	}()
	start := time.Now()
	_, errno := w.readAs(ctxPid(playerPid), h, off, 4096)
	if errno != syscall.EIO {
		t.Fatalf("errno = %v, want EIO", errno)
	}
	if el := time.Since(start); el > 3*time.Second {
		t.Errorf("EIO took %v", el)
	}
	if w.rs.reqCount() != 0 {
		t.Error("remote was used")
	}
}

func TestWaitNoProgressTimesOut(t *testing.T) {
	w := wSetup(t)
	w.s.cfg.WaitTimeout = 400 * time.Millisecond
	h := w.open(t)
	off := w.m.HeadLen + 7*w.m.ChunkSize
	start := time.Now()
	if _, errno := w.readAs(ctxPid(playerPid), h, off, 4096); errno != syscall.EIO {
		t.Fatalf("errno = %v, want EIO", errno)
	}
	if el := time.Since(start); el < 350*time.Millisecond || el > 3*time.Second {
		t.Errorf("timed out after %v", el)
	}
	if w.s.m.WaitTimeouts.Load() != 1 {
		t.Error("timeout not counted")
	}
}

func TestWaitGrowingFillFileKeepsTheWaitAlive(t *testing.T) {
	w := wSetup(t)
	// Each step (a pause plus a 4 MiB write and fsync, slow under -race) is
	// far shorter than the timeout; the steps together run well past it.
	w.s.cfg.WaitTimeout = 4 * time.Second
	h := w.open(t)
	off := w.m.HeadLen + 20*w.m.ChunkSize
	done := make(chan syscall.Errno, 1)
	go func() { _, e := w.readAs(ctxPid(playerPid), h, off, 4096); done <- e }()
	start := time.Now()
	for i := int64(0); i < 20; i++ {
		time.Sleep(250 * time.Millisecond)
		w.promoteChunk(t, w.m.HeadLen+i*w.m.ChunkSize)
	}
	if time.Since(start) < w.s.cfg.WaitTimeout+time.Second {
		t.Fatal("test did not run past the timeout")
	}
	select {
	case e := <-done:
		t.Fatalf("gave up (%v) while the fill file was growing", e)
	default:
	}
	w.promoteChunk(t, off)
	if e := <-done; e != 0 {
		t.Fatal(e)
	}
}

func TestWaitInterruptedRequestStopsWaiting(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	ctx, cancel := context.WithCancel(ctxPid(playerPid))
	done := make(chan syscall.Errno, 1)
	go func() { _, e := w.readAs(ctx, h, w.m.HeadLen, 4096); done <- e }()
	time.Sleep(200 * time.Millisecond)
	cancel()
	select {
	case e := <-done:
		if e != syscall.EINTR {
			t.Fatalf("errno = %v, want EINTR", e)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("wait did not stop")
	}
	if g := w.s.m.WaitingReads.Load(); g != 0 {
		t.Errorf("waiting reads gauge = %d", g)
	}
}

func TestWaitPrewarmWantAtHeadLen(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	before := w.m.HeadLen*3/4 - 4096
	if _, errno := w.readAs(ctxPid(playerPid), h, before-4096, 4096); errno != 0 {
		t.Fatal(errno)
	}
	if len(w.wants(t)) != 0 {
		t.Fatal("want before 0.75 of the head")
	}
	if _, errno := w.readAs(ctxPid(playerPid), h, before, 8192); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 1 || got[0] != w.m.HeadLen {
		t.Fatalf("wants = %v, want [%d]", got, w.m.HeadLen)
	}
	// A later read of the first chunk is inside the 64 MiB after that want.
	w.promoteChunk(t, w.m.HeadLen)
	if _, errno := w.readAs(ctxPid(playerPid), h, w.m.HeadLen, 4096); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 1 {
		t.Fatalf("wants = %v, want still one", got)
	}
	// The background reader pre-warms the remote stream instead.
	h2 := w.open(t)
	if _, errno := w.readAs(scanCtx(), h2, before, 8192); errno != 0 {
		t.Fatal(errno)
	}
	waitFor(t, 5*time.Second, "remote pre-warm", func() bool { return w.rs.reqCount() > 0 })
	if got := w.wants(t); len(got) != 1 {
		t.Fatalf("background pre-warm wrote a want: %v", got)
	}
}

// A Plex stream asks for the middle on its first read, wherever that read is.
func TestWaitStreamWantsOnFirstRead(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	if _, errno := w.readAs(ctxPid(streamPid), h, 0, 4096); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 1 || got[0] != w.m.HeadLen {
		t.Fatalf("wants = %v, want [%d]", got, w.m.HeadLen)
	}
	if _, errno := w.readAs(ctxPid(streamPid), h, w.m.HeadLen*3/4, 8192); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 1 {
		t.Fatalf("wants = %v, want still one", got)
	}
	// A first read of the index at the end of the file counts too.
	h2 := w.open(t)
	if _, errno := w.readAs(ctxPid(streamPid), h2, w.m.Size-4096, 4096); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 2 || got[1] != w.m.HeadLen {
		t.Fatalf("wants = %v, want a second at %d", got, w.m.HeadLen)
	}
	// Another player reading the start (an arr's ffprobe) asks for nothing.
	h3 := w.open(t)
	if _, errno := w.readAs(ctxPid(playerPid), h3, 0, 1<<20); errno != 0 {
		t.Fatal(errno)
	}
	if _, errno := w.readAs(ctxPid(playerPid), h3, w.m.Size-4096, 4096); errno != 0 {
		t.Fatal(errno)
	}
	if got := w.wants(t); len(got) != 2 {
		t.Fatalf("a probe wrote a want: %v", got)
	}
}

func TestWaitWantReemittedOnFarSeek(t *testing.T) {
	w := wSetup(t)
	w.s.cfg.WaitTimeout = 100 * time.Millisecond
	h := w.open(t)
	a := w.m.HeadLen + 2*w.m.ChunkSize
	read := func(off int64) {
		if _, errno := w.readAs(ctxPid(playerPid), h, off, 4096); errno != syscall.EIO {
			t.Fatalf("errno = %v", errno)
		}
	}
	read(a)
	read(a + 8*w.m.ChunkSize) // 32 MiB on: inside the span
	far := a + 100<<20
	read(far)
	read(a) // back before the last want
	want := []int64{a, far, a}
	got := w.wants(t)
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("wants = %v, want %v", got, want)
	}
}

func TestWaitHandleSurvivesManifestRemoval(t *testing.T) {
	w := wSetup(t)
	h := w.open(t)
	// The promote finished: the whole file is in the stub, then the swap
	// removes the stub path, the manifest, the chunk list and the fill file.
	f, err := os.OpenFile(w.m.StubPath, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteAt(w.data[w.m.HeadLen:w.m.MidEnd()], w.m.HeadLen)
	f.Close()
	for _, n := range []string{".json", ".chunks", ".fill"} {
		os.Remove(filepath.Join(w.mdir, w.m.ID+n))
	}
	os.Remove(w.m.StubPath) // the open descriptor keeps the inode
	w.s.Reload()
	for _, off := range []int64{w.m.HeadLen + 50<<20, w.m.HeadLen, w.m.MidEnd() - 4096} {
		b, errno := w.readAs(ctxPid(playerPid), h, off, 4096)
		if errno != 0 || !bytes.Equal(b, w.data[off:off+4096]) {
			t.Fatalf("read at %d after removal: %v", off, errno)
		}
	}
	if w.rs.reqCount() != 0 {
		t.Error("remote used after removal")
	}
}

func TestReaderClassFromProc(t *testing.T) {
	proc := fakeProc(t, map[uint32][2]string{
		10: {"Plex Media Scan", "1"},
		11: {"Plex Transcoder", "10"}, // child of the scanner
		12: {"Plex Transcoder", "13"}, // parent is a player
		13: {"Plex Media Serv", "1"},
		14: {"ffmpeg (v2) x", "1"}, // parens and spaces in comm
		15: {"Plex Media Scan", "1"},
		16: {"x", "999"}, // parent not in /proc
	})
	c := newClassifier(proc, []string{"Plex Media Scanner", "other"}) // long name is cut to 15
	for _, tc := range []struct {
		pid  uint32
		want bool
	}{{10, true}, {11, true}, {12, false}, {13, false}, {14, false}, {15, true}, {16, false}, {777, false}, {0, false}} {
		if got := c.background(ctxPid(tc.pid)); got != tc.want {
			t.Errorf("pid %d: background = %v, want %v", tc.pid, got, tc.want)
		}
	}
	if c.background(context.Background()) {
		t.Error("a request with no caller is a player")
	}
	d := newClassifier(proc, DefaultBackgroundComms)
	if !d.background(ctxPid(11)) || d.background(ctxPid(13)) {
		t.Error("default comms wrong")
	}
	if newClassifier(proc, []string{}).background(ctxPid(10)) {
		t.Error("empty list should classify nobody as background")
	}
	// Cached: the answer survives the /proc entry going away.
	os.RemoveAll(filepath.Join(proc, "10"))
	if !c.background(ctxPid(10)) {
		t.Error("class was not cached")
	}
}

func TestReaderClassResolvesThreadToProcess(t *testing.T) {
	proc := fakeProc(t, map[uint32][2]string{
		10: {"Plex Media Scan", "1"},
		20: {"Plex Transcoder", "10"}, // a process whose parent is the scanner
		30: {"Plex Media Serv", "1"},
	})
	fakeThread(t, proc, 11, 10, "Scan Worker")     // renamed scanner thread
	fakeThread(t, proc, 21, 20, "ffmpeg-thread")   // thread of the scanner's child
	fakeThread(t, proc, 31, 30, "Plex Media Scan") // a player thread that borrowed the scanner's name
	c := newClassifier(proc, DefaultBackgroundComms)
	for _, tc := range []struct {
		tid  uint32
		want bool
	}{{11, true}, {21, true}, {31, false}} {
		if got := c.background(ctxPid(tc.tid)); got != tc.want {
			t.Errorf("tid %d: background = %v, want %v", tc.tid, got, tc.want)
		}
	}
	// No status file: the thread group cannot be resolved, so it is a player.
	os.Remove(filepath.Join(proc, "10", "status"))
	if newClassifier(proc, DefaultBackgroundComms).background(ctxPid(10)) {
		t.Error("an unresolvable pid must be a player")
	}
}

func TestConfigWaitEnv(t *testing.T) {
	env := map[string]string{"TIERFS_MOUNT": "/m", "TIERFS_MANIFESTS": "/f", "TIERFS_STUB_ROOTS": "/s", "TIERFS_REMOTE_BASE": "http://x"}
	get := func(k string) string { return env[k] }
	c, err := ConfigFromEnv(get)
	if err != nil || fmt.Sprint(c.BackgroundComms) != "[Plex Media Scan watch-align]" || c.WaitTimeout != 0 ||
		c.StreamComms != nil {
		t.Fatalf("defaults: %+v %+v %v", c.BackgroundComms, c.StreamComms, err)
	}
	env["TIERFS_BACKGROUND_COMMS"] = "a, b ,,c"
	env["TIERFS_STREAM_COMMS"] = " Plex Media Server ,, x"
	env["TIERFS_WAIT_S"] = "30"
	c, err = ConfigFromEnv(get)
	if err != nil || fmt.Sprint(c.BackgroundComms) != "[a b c]" || c.WaitTimeout != 30*time.Second ||
		fmt.Sprint(c.StreamComms) != "[Plex Media Server x]" {
		t.Fatalf("set: %+v %+v %v %v", c.BackgroundComms, c.StreamComms, c.WaitTimeout, err)
	}
	env["TIERFS_WAIT_S"] = "0"
	if _, err = ConfigFromEnv(get); err == nil {
		t.Error("TIERFS_WAIT_S=0 accepted")
	}
}

// Another viewer's want can take the promote elsewhere. A read still waiting
// writes its want again every wantResend, even inside its 64 MiB span, so the
// promote comes back to it.
func TestWaitResendsWantWhileWaiting(t *testing.T) {
	old := wantResend
	wantResend = 60 * time.Millisecond
	t.Cleanup(func() { wantResend = old })
	w := wSetup(t)
	w.s.cfg.WaitTimeout = 400 * time.Millisecond
	h := w.open(t)
	a := w.m.HeadLen + 2*w.m.ChunkSize
	if _, errno := w.readAs(ctxPid(playerPid), h, a, 4096); errno != syscall.EIO {
		t.Fatalf("errno = %v", errno)
	}
	got := w.wants(t)
	if len(got) < 3 {
		t.Fatalf("wants = %v, want the first and at least two resends", got)
	}
	for _, o := range got {
		if o != a {
			t.Fatalf("wants = %v, all at %d", got, a)
		}
	}
}
