package tierfs

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	defaultEventsMax  = 64 << 20
	eventsKeepOld     = 3
	eventsErrLogEvery = time.Minute
)

// Event is one JSON line in the events file.
type Event struct {
	TS          float64 `json:"ts"`
	ID          string  `json:"id"`
	Ev          string  `json:"ev"`
	BytesLocal  int64   `json:"bytes_local"`
	BytesRemote int64   `json:"bytes_remote"`
	Offset      *int64  `json:"offset,omitempty"` // bad_chunk: start of the chunk
}

// EventLog appends JSON lines to a file and rotates it by size. A nil
// *EventLog discards events. Write failures never reach the read path.
type EventLog struct {
	path string
	max  int64

	mu      sync.Mutex
	f       *os.File
	size    int64
	lastErr time.Time
}

// NewEventLog opens (or creates) the events file. An empty path disables it.
func NewEventLog(path string, maxSize int64) (*EventLog, error) {
	if path == "" {
		return nil, nil
	}
	if maxSize <= 0 {
		maxSize = defaultEventsMax
	}
	e := &EventLog{path: path, max: maxSize}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	if err := e.open(); err != nil {
		return nil, err
	}
	return e, nil
}

func (e *EventLog) open() error {
	f, err := os.OpenFile(e.path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	e.f, e.size = f, st.Size()
	return nil
}

func (e *EventLog) rotate() error {
	e.f.Close()
	e.f = nil
	os.Remove(fmt.Sprintf("%s.%d", e.path, eventsKeepOld))
	for i := eventsKeepOld - 1; i >= 1; i-- {
		os.Rename(fmt.Sprintf("%s.%d", e.path, i), fmt.Sprintf("%s.%d", e.path, i+1))
	}
	os.Rename(e.path, e.path+".1")
	return e.open()
}

// Emit appends one event.
func (e *EventLog) Emit(id, ev string, local, remote int64) {
	e.emit(Event{ID: id, Ev: ev, BytesLocal: local, BytesRemote: remote})
}

// EmitBad records a chunk that failed verification twice.
func (e *EventLog) EmitBad(id string, offset int64) {
	e.emit(Event{ID: id, Ev: "bad_chunk", Offset: &offset})
}

// EmitSlotTimeout records a read that gave up waiting for a stream slot.
func (e *EventLog) EmitSlotTimeout(id string) {
	e.emit(Event{ID: id, Ev: "slot_timeout"})
}

func (e *EventLog) emit(ev Event) {
	if e == nil {
		return
	}
	ev.TS = float64(time.Now().UnixNano()) / 1e9
	line, _ := json.Marshal(ev)
	line = append(line, '\n')
	e.mu.Lock()
	defer e.mu.Unlock()
	var err error
	if e.f == nil {
		err = e.open()
	}
	if err == nil && e.size > 0 && e.size+int64(len(line)) > e.max {
		err = e.rotate()
	}
	if err == nil && e.f != nil {
		var n int
		n, err = e.f.Write(line)
		e.size += int64(n)
	}
	if err != nil && time.Since(e.lastErr) > eventsErrLogEvery {
		e.lastErr = time.Now()
		log.Printf("events: write failed: %v", err)
	}
}

// Close closes the file.
func (e *EventLog) Close() {
	if e == nil {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.f != nil {
		e.f.Close()
		e.f = nil
	}
}
