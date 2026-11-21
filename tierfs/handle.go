package tierfs

import (
	"context"
	"errors"
	"log"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/hanwen/go-fuse/v2/fuse"
)

// After a remote failure, further mid reads on the same handle fail at once
// for this long instead of each waiting out their own retry window.
const failCooldown = 2 * time.Second

var errHandleClosed = errors.New("handle closed")

// handle is one open file. It owns the stub descriptor and at most one remote
// stream.
type handle struct {
	srv  *Server
	m    *Manifest
	stub *os.File

	mu         sync.Mutex // guards st, warmed, lastErr
	st         *stream
	warmed     bool
	lastErr    error
	lastErrAt  time.Time
	cur        atomic.Pointer[stream] // mirror of st, so close can cancel without mu
	closed     atomic.Bool
	local      atomic.Int64
	remote     atomic.Int64
	remoteSeen atomic.Bool
}

func (s *Server) openHandle(m *Manifest) (*handle, error) {
	f, err := os.Open(m.StubPath)
	if err != nil {
		return nil, err
	}
	h := &handle{srv: s, m: m, stub: f}
	s.m.OpenHandles.Add(1)
	s.ev.Emit(m.ID, "open", 0, 0)
	return h, nil
}

// Read implements fs.FileReader.
func (h *handle) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	m := h.m
	segs := Split(m.Size, m.HeadLen, m.TailLen, off, int64(len(dest)))
	var total int64
	for _, sg := range segs {
		p := dest[sg.Off-off : sg.Off-off+sg.N]
		if sg.Kind == Mid {
			if err := h.readMid(ctx, p, sg.Off); err != nil {
				if ctx.Err() != nil {
					return nil, syscall.EINTR
				}
				log.Printf("read %s: offset %d: %v", m.ID, sg.Off, err)
				return nil, syscall.EIO
			}
			h.remote.Add(sg.N)
			h.srv.m.BytesRemote.Add(sg.N)
			if h.remoteSeen.CompareAndSwap(false, true) {
				h.srv.ev.Emit(m.ID, "remote_read", h.local.Load(), h.remote.Load())
			}
		} else {
			if _, err := h.stub.ReadAt(p, sg.Off); err != nil {
				log.Printf("read %s: stub offset %d: %v", m.ID, sg.Off, err)
				return nil, syscall.EIO
			}
			h.local.Add(sg.N)
			h.srv.m.BytesLocal.Add(sg.N)
			if sg.Kind == Head {
				h.maybeWarm(sg.Off + sg.N)
			}
		}
		total += sg.N
	}
	return fuse.ReadResultData(dest[:total]), 0
}

func (h *handle) newStream(off int64) *stream {
	s := h.srv.remote.newStream(h.m.ID, h.m.RemotePath, off, h.m.MidEnd())
	h.st = s
	h.cur.Store(s)
	return s
}

// dropStream cancels the current stream. h.mu must be held.
func (h *handle) dropStream() {
	if h.st != nil {
		h.st.Close()
		h.st = nil
		h.cur.Store(nil)
	}
}

// maybeWarm starts the remote stream at head_len once a head read gets within
// the last quarter of the head, so the middle is already flowing when the
// reader gets there.
func (h *handle) maybeWarm(readEnd int64) {
	if readEnd < h.m.HeadLen*3/4 || !h.mu.TryLock() {
		return
	}
	defer h.mu.Unlock()
	if h.warmed || h.closed.Load() {
		return
	}
	h.warmed = true
	if h.st == nil {
		h.newStream(h.m.HeadLen)
	}
}

func (h *handle) readMid(ctx context.Context, p []byte, off int64) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed.Load() {
		return errHandleClosed
	}
	if h.lastErr != nil && time.Since(h.lastErrAt) < failCooldown {
		return h.lastErr
	}
	for attempt := 0; attempt < 3; attempt++ {
		if h.st == nil || !h.st.covers(off) {
			h.dropStream() // cancels the old request at once
			h.newStream(off)
		}
		err := h.st.readAt(ctx, p, off)
		if err == nil {
			h.lastErr = nil
			return nil
		}
		h.dropStream()
		if errors.Is(err, errStreamClosed) || errors.Is(err, errOutOfWindow) {
			if h.closed.Load() {
				return errHandleClosed
			}
			continue // idle timeout or race with a trim: start a fresh stream
		}
		if ctx.Err() == nil {
			h.lastErr, h.lastErrAt = err, time.Now()
		}
		return err
	}
	return errStreamClosed
}

// Release implements fs.FileReleaser.
func (h *handle) Release(ctx context.Context) syscall.Errno {
	if !h.closed.CompareAndSwap(false, true) {
		return 0
	}
	if s := h.cur.Load(); s != nil {
		s.Close()
	}
	h.mu.Lock()
	h.dropStream()
	h.mu.Unlock()
	h.stub.Close()
	h.srv.m.OpenHandles.Add(-1)
	h.srv.ev.Emit(h.m.ID, "close", h.local.Load(), h.remote.Load())
	return 0
}
