package tierfs

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

// holdSlot keeps reading sequentially from s so it is never idle or parked.
func holdSlot(t *testing.T, s *stream, start, limit int64) (stop func()) {
	t.Helper()
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		p := make([]byte, 4096)
		for off := start; off+int64(len(p)) <= limit; off += int64(len(p)) {
			select {
			case <-done:
				return
			default:
			}
			if err := s.readAt(context.Background(), p, off); err != nil {
				return
			}
			time.Sleep(15 * time.Millisecond)
		}
	}()
	return func() { close(done); wg.Wait() }
}

// A busy holder keeps the only slot: the second reader must get an error at
// about SlotWait, not hang.
func TestStreamSlotWaitTimesOut(t *testing.T) {
	data := randomData(streamTestSize, 21)
	rs := newRangeServer(t, data)
	r := newTestRemote(t, rs, func(c *RemoteConfig) { c.MaxStreams = 1; c.SlotWait = 600 * time.Millisecond })
	s1 := r.newStream("a", testRemote, 0, streamTestSize)
	defer s1.Close()
	p := make([]byte, 4096)
	if err := s1.readAt(context.Background(), p, 0); err != nil {
		t.Fatal(err)
	}
	stop := holdSlot(t, s1, 4096, streamTestSize)
	defer stop()

	s2 := r.newStream("b", testRemote, 0, streamTestSize)
	defer s2.Close()
	t0 := time.Now()
	err := s2.readAt(context.Background(), p, 0)
	el := time.Since(t0)
	if !errors.Is(err, errSlotTimeout) {
		t.Fatalf("err = %v after %v, want errSlotTimeout", err, el)
	}
	if el < 500*time.Millisecond || el > 3*time.Second {
		t.Fatalf("gave up after %v, want about 600ms", el)
	}
	if r.m.SlotTimeouts.Load() != 1 {
		t.Fatalf("slot timeouts = %d, want 1", r.m.SlotTimeouts.Load())
	}
}

// Same through a handle: EIO at about SlotWait and a slot_timeout event.
func TestHandleSlotWaitEIOAndEvent(t *testing.T) {
	data := randomData(80<<20+5, 22)
	rs := newRangeServer(t, data)
	e := rvSetup(t, data, rs.URL, func(c *RemoteConfig) { c.MaxStreams = 1; c.SlotWait = 800 * time.Millisecond }, nil)
	evPath := filepath.Join(t.TempDir(), "events.jsonl")
	ev, err := NewEventLog(evPath, 0)
	if err != nil {
		t.Fatal(err)
	}
	e.s.ev = ev

	a := e.open(t)
	if _, errno := e.read(a, e.m.HeadLen, 4096); errno != 0 {
		t.Fatal(errno)
	}
	stopCh := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for off := e.m.HeadLen + 4096; off < e.m.HeadLen+60<<20; off += 4096 {
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
	t0 := time.Now()
	_, errno := e.read(b, e.m.HeadLen+30<<20, 4096)
	el := time.Since(t0)
	if errno != syscall.EIO {
		t.Fatalf("errno = %v after %v, want EIO", errno, el)
	}
	if el < 700*time.Millisecond || el > 4*time.Second {
		t.Fatalf("EIO after %v, want about 800ms", el)
	}
	if n := countEv(readEvents(t, evPath), "slot_timeout"); n != 1 {
		t.Fatalf("slot_timeout events = %d, want 1", n)
	}
}

func TestClearStaleMountDecision(t *testing.T) {
	cases := []struct {
		name string
		stat error
		want bool
	}{
		{"healthy", nil, false},
		{"missing", &errNotExist, false},
		{"permission", syscall.EACCES, false},
		{"dead transport", &pathErr{syscall.ENOTCONN}, true},
		{"dead transport bare", syscall.ENOTCONN, true},
	}
	for _, c := range cases {
		var unmounted []string
		got := clearStaleMountWith("/mnt/x", func(string) error { return c.stat }, func(p string) error {
			unmounted = append(unmounted, p)
			return nil
		})
		if got != c.want || (len(unmounted) == 1) != c.want {
			t.Errorf("%s: attempted=%v unmounts=%v, want %v", c.name, got, unmounted, c.want)
		}
	}
	// A failing unmount is logged, not fatal, and still counts as attempted.
	if !clearStaleMountWith("/mnt/x", func(string) error { return syscall.ENOTCONN }, func(string) error { return errors.New("busy") }) {
		t.Error("failed unmount not reported as attempted")
	}
}

type pathErr struct{ err error }

func (p *pathErr) Error() string { return "stat /mnt/x: " + p.err.Error() }
func (p *pathErr) Unwrap() error { return p.err }

var errNotExist = pathErr{syscall.ENOENT}
