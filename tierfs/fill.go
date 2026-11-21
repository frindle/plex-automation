package tierfs

import (
	"bytes"
	"os"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// fillView follows one <id>.fill file. The promote appends one decimal chunk
// start per line after the chunk is on disk, and `failed <offset>` when it
// gives up. The view only ever reads the new bytes.
type fillView struct {
	path string

	mu       sync.Mutex
	pos      int64
	ino      uint64
	partial  []byte
	have     map[int64]struct{}
	failed   bool
	lastGrow time.Time // when a refresh last found a new line
}

func newFillView(path string) *fillView {
	return &fillView{path: path, have: map[int64]struct{}{}}
}

// refresh reads whatever was appended since the last call.
func (v *fillView) refresh(now time.Time) {
	f, err := os.Open(v.path)
	if err != nil {
		return
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	var ino uint64
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		ino = uint64(st.Ino)
	}
	if fi.Size() < v.pos || (v.ino != 0 && ino != v.ino) { // replaced: start again
		v.pos, v.partial, v.have, v.failed = 0, nil, map[int64]struct{}{}, false
	}
	v.ino = ino
	buf := make([]byte, 64<<10)
	for {
		n, _ := f.ReadAt(buf, v.pos)
		if n <= 0 {
			return
		}
		v.pos += int64(n)
		v.partial = append(v.partial, buf[:n]...)
		for {
			i := bytes.IndexByte(v.partial, '\n')
			if i < 0 {
				break
			}
			line := strings.TrimSpace(string(v.partial[:i]))
			v.partial = v.partial[i+1:]
			if v.addLine(line) {
				v.lastGrow = now
			}
		}
	}
}

func (v *fillView) addLine(line string) bool {
	if rest, ok := strings.CutPrefix(line, "failed"); ok {
		if _, err := strconv.ParseInt(strings.TrimSpace(rest), 10, 64); err == nil {
			v.failed = true
			return true
		}
		return false
	}
	n, err := strconv.ParseInt(line, 10, 64)
	if err != nil || n < 0 {
		return false
	}
	v.have[n] = struct{}{}
	return true
}

func (v *fillView) has(off int64) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	_, ok := v.have[off]
	return ok
}

func (v *fillView) isFailed() bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.failed
}

func (v *fillView) lastProgress() time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.lastGrow
}
