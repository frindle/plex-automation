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
	ck   *chunkCheck
	stub *os.File

	mu         sync.Mutex // guards st, lastErr
	st         *stream
	lastErr    error
	lastErrAt0 int64 // start of the chunk the last remote error hit
	lastErrAt  time.Time
	cur        atomic.Pointer[stream] // mirror of st, so close can cancel without mu
	closed     atomic.Bool
	local      atomic.Int64
	remote     atomic.Int64
	remoteSeen atomic.Bool
}

func (s *Server) openHandle(e *entry) (*handle, error) {
	m := e.m
	f, err := os.OpenFile(m.StubPath, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	// The path is a name in an admin-controlled directory, but check what it
	// really is: a regular file of the size the manifest promises.
	if fi, err := f.Stat(); err != nil || !fi.Mode().IsRegular() || fi.Size() != m.Size {
		f.Close()
		return nil, errors.New("stub is not a regular file of the manifest's size")
	}
	h := &handle{srv: s, m: m, stub: f, ck: &chunkCheck{base: m.HeadLen, size: m.ChunkSize, sums: e.sums}}
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
				var bc *badChunkError
				if errors.As(err, &bc) {
					h.srv.m.BadChunks.Add(1)
					h.srv.ev.EmitBad(m.ID, bc.Offset)
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
	s := h.srv.remote.newVerifiedStream(h.m.ID, h.m.RemotePath, h.ck.alignDown(off), h.m.MidEnd(), h.ck)
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
	if h.closed.Load() {
		return
	}
	// Start (or restart, if the stream idled out or a seek moved it away) a
	// stream at the head of the middle. A live stream that already covers it
	// is left alone.
	if h.st != nil && h.st.alive() && h.st.covers(h.m.HeadLen) {
		return
	}
	h.dropStream()
	h.newStream(h.m.HeadLen)
}

func (h *handle) readMid(ctx context.Context, p []byte, off int64) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed.Load() {
		return errHandleClosed
	}
	if h.lastErr != nil && time.Since(h.lastErrAt) < failCooldown && h.ck.alignDown(off) == h.lastErrAt0 {
		return h.lastErr // only the region that just failed fails fast
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
		var bc *badChunkError
		if ctx.Err() == nil && !errors.As(err, &bc) { // a bad chunk says nothing about other chunks
			h.lastErr, h.lastErrAt, h.lastErrAt0 = err, time.Now(), h.ck.alignDown(off)
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
