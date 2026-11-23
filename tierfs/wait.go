package tierfs

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// wantSpan is how far past the last want a read may go before the handle
// writes another one.
const wantSpan = 64 << 20

var (
	errFillFailed  = errors.New("promote failed")
	errWaitTimeout = errors.New("no progress from the promote")
	errNoData      = errors.New("range is not on disk and nothing is filling it")
)

// Linux lseek whence values. They are fixed numbers there; the syscall package
// does not export them for every platform tierfs builds on.
const (
	seekData = 3
	seekHole = 4
)

// holeIn reports whether any byte of [off, off+n) in the stub is a hole, that
// is, a part the promote has not written. Anything it cannot tell counts as a
// hole: a player must never get zeros that are not the file's data.
func (h *handle) holeIn(off, n int64) bool {
	h.seekMu.Lock()
	defer h.seekMu.Unlock()
	fd := int(h.stub.Fd())
	end := off + n
	for pos := off; pos < end; {
		d, err := syscall.Seek(fd, pos, seekData)
		if err != nil || d > pos {
			return true // ENXIO: only hole to the end of the file
		}
		hs, err := syscall.Seek(fd, pos, seekHole)
		if err != nil {
			return true
		}
		pos = hs
	}
	return false
}

func (h *handle) fillFileExists() bool {
	_, err := os.Stat(filepath.Join(h.srv.cfg.Manifests, h.m.ID+".fill"))
	return err == nil
}

// noteWant writes a want event for the chunk at off, unless it falls in the
// 64 MiB after this handle's last want.
func (h *handle) noteWant(off int64) {
	h.wantMu.Lock()
	send := !h.hasWant || off < h.lastW || off >= h.lastW+wantSpan
	if send {
		h.hasWant, h.lastW = true, off
	}
	h.wantMu.Unlock()
	if send {
		h.srv.m.WantEvents.Add(1)
		h.srv.ev.EmitWant(h.m.ID, off)
	}
}

// maybeWant is the player pre-warm: once a read reaches the last quarter of
// the head, ask for the start of the middle.
func (h *handle) maybeWant(readEnd int64) {
	if readEnd < h.m.HeadLen*3/4 {
		return
	}
	h.wantNow()
}

// wantNow asks for the start of the middle, once per handle. A Plex stream
// calls it on its first read of any part of the file.
func (h *handle) wantNow() {
	if !h.warmed.CompareAndSwap(false, true) {
		return
	}
	h.noteWant(h.m.HeadLen)
}

// firstMissing returns the start of the first chunk in [first, last] that the
// fill file does not list yet.
func (h *handle) firstMissing(first, last int64) (int64, bool) {
	for c := first; c <= last; c += h.ck.size {
		if !h.fill.has(c) {
			return c, true
		}
	}
	return 0, false
}

// manifestGone reports whether the promote has finished and removed the
// manifest. From then on the stub inode is the whole file.
func (h *handle) manifestGone() bool {
	if h.allDone.Load() {
		return true
	}
	if _, err := os.Stat(filepath.Join(h.srv.cfg.Manifests, h.m.ID+".json")); errors.Is(err, os.ErrNotExist) {
		h.allDone.Store(true)
		return true
	}
	return false
}

// readPlayerMid serves a middle read from the stub, once the promote has
// listed every chunk it touches.
func (h *handle) readPlayerMid(ctx context.Context, p []byte, off int64) error {
	if h.closed.Load() {
		return errHandleClosed
	}
	if !h.allDone.Load() || h.holeIn(off, int64(len(p))) {
		first, last := h.ck.alignDown(off), h.ck.alignDown(off+int64(len(p))-1)
		if err := h.waitChunks(ctx, first, last, off, int64(len(p))); err != nil {
			return err
		}
	}
	_, err := h.stub.ReadAt(p, off)
	return err
}

func (h *handle) waitChunks(ctx context.Context, first, last, off, n int64) error {
	if _, miss := h.firstMissing(first, last); !miss {
		return nil
	}
	now := time.Now()
	h.fill.refresh(now)
	if _, miss := h.firstMissing(first, last); !miss {
		return nil
	}
	srv := h.srv
	start := now
	srv.m.WaitReads.Add(1)
	srv.m.WaitingReads.Add(1)
	defer srv.m.WaitingReads.Add(-1)
	for {
		miss, missing := h.firstMissing(first, last)
		switch {
		case !missing:
			return nil
		case h.fill.isFailed():
			srv.m.WaitFailed.Add(1)
			return errFillFailed
		case h.manifestGone():
			// The manifest is gone. That normally means the promote finished
			// and the stub is whole; if the range still has holes it is not.
			if !h.holeIn(off, n) {
				return nil
			}
			if !h.fillFileExists() {
				srv.m.WaitFailed.Add(1)
				return errNoData
			}
		}
		h.noteWant(miss)
		since := start
		if lp := h.fill.lastProgress(); lp.After(since) {
			since = lp
		}
		if time.Since(since) >= srv.cfg.WaitTimeout {
			srv.m.WaitTimeouts.Add(1)
			return errWaitTimeout
		}
		t := time.NewTimer(srv.cfg.FillPoll)
		select {
		case <-ctx.Done():
			t.Stop()
			return ctx.Err()
		case <-t.C:
		}
		if h.closed.Load() {
			return errHandleClosed
		}
		h.fill.refresh(time.Now())
	}
}
