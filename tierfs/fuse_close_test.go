package tierfs

import (
	"testing"
	"time"
)

func TestFuseCloseWithOpenFileDoesNotHang(t *testing.T) {
	e := startFuse(t, nil)
	f := e.open()
	e.readAtMust(f, 0, 4096)
	done := make(chan error, 1)
	go func() { done <- e.srv.Close() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("close with an open file: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("close with an open file hung")
	}
}
