package tierfs

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Over a real mount: the test process is a player, so its middle read waits
// for the fill line and then comes from the stub.
func TestFusePlayerReadWaitsForFill(t *testing.T) {
	e := startFuse(t, func(c *Config) { c.BackgroundComms = []string{"no-such-comm"} })
	f := e.open()
	off := e.m.HeadLen + 2*e.m.ChunkSize
	p := make([]byte, 4096)
	done := make(chan error, 1)
	go func() { _, err := f.ReadAt(p, off); done <- err }()
	select {
	case <-done:
		t.Fatal("read finished before the promote listed the chunk")
	case <-time.After(400 * time.Millisecond):
	}
	sf, err := os.OpenFile(e.m.StubPath, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	sf.WriteAt(e.data[off:off+e.m.ChunkSize], off)
	sf.Sync()
	sf.Close()
	fl, err := os.OpenFile(filepath.Join(e.mdir, e.m.ID+".fill"), os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	fmt.Fprintln(fl, off)
	fl.Close()
	if err := <-done; err != nil || !bytes.Equal(p, e.data[off:off+4096]) {
		t.Fatalf("read: %v", err)
	}
	if e.rs.reqCount() != 0 {
		t.Error("player used the remote")
	}
	var wants int
	for _, ev := range readEvents(t, e.events) {
		if ev.Ev == "want" && ev.Offset != nil && *ev.Offset == off {
			wants++
		}
	}
	if wants != 1 {
		t.Errorf("want events at %d = %d", off, wants)
	}
}
