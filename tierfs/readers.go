package tierfs

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hanwen/go-fuse/v2/fuse"
)

const (
	commMax       = 15 // /proc/<pid>/comm is cut to 15 characters
	classTTL      = 30 * time.Second
	classCacheMax = 4096
)

// DefaultBackgroundComms names the readers that keep the remote stream.
var DefaultBackgroundComms = []string{"Plex Media Scan"}

type classEntry struct {
	background bool
	at         time.Time
}

// classifier tells background readers (the Plex scanner) from players, using
// the caller's pid from the FUSE request. A pid it cannot read is a player.
type classifier struct {
	procRoot string
	names    map[string]struct{}

	mu    sync.Mutex
	cache map[uint32]classEntry
}

func newClassifier(procRoot string, comms []string) *classifier {
	if procRoot == "" {
		procRoot = "/proc"
	}
	c := &classifier{procRoot: procRoot, names: map[string]struct{}{}, cache: map[uint32]classEntry{}}
	for _, n := range comms {
		if n = strings.TrimSpace(n); n != "" {
			c.names[truncComm(n)] = struct{}{}
		}
	}
	return c
}

func truncComm(s string) string {
	if len(s) > commMax {
		return s[:commMax]
	}
	return s
}

// background reports whether the request in ctx comes from a background reader.
func (c *classifier) background(ctx context.Context) bool {
	caller, ok := fuse.FromContext(ctx)
	if !ok || caller.Pid == 0 || len(c.names) == 0 {
		return false
	}
	pid := caller.Pid
	now := time.Now()
	c.mu.Lock()
	if e, ok := c.cache[pid]; ok && now.Sub(e.at) < classTTL {
		c.mu.Unlock()
		return e.background
	}
	c.mu.Unlock()
	bg := c.classify(pid)
	c.mu.Lock()
	if len(c.cache) >= classCacheMax {
		c.cache = map[uint32]classEntry{}
	}
	c.cache[pid] = classEntry{bg, now}
	c.mu.Unlock()
	return bg
}

func (c *classifier) comm(pid uint32) (string, bool) {
	b, err := os.ReadFile(filepath.Join(c.procRoot, strconv.FormatUint(uint64(pid), 10), "comm"))
	if err != nil {
		return "", false
	}
	return strings.TrimRight(string(b), "\n"), true
}

func (c *classifier) ppid(pid uint32) (uint32, bool) {
	b, err := os.ReadFile(filepath.Join(c.procRoot, strconv.FormatUint(uint64(pid), 10), "stat"))
	if err != nil {
		return 0, false
	}
	s := string(b)
	i := strings.LastIndexByte(s, ')') // comm may hold spaces and parens
	if i < 0 {
		return 0, false
	}
	f := strings.Fields(s[i+1:]) // state ppid ...
	if len(f) < 2 {
		return 0, false
	}
	n, err := strconv.ParseUint(f[1], 10, 32)
	return uint32(n), err == nil && n > 0
}

func (c *classifier) classify(pid uint32) bool {
	name, ok := c.comm(pid)
	if !ok {
		return false // cannot read it: a player
	}
	if _, bg := c.names[truncComm(name)]; bg {
		return true
	}
	if pp, ok := c.ppid(pid); ok {
		if pn, ok := c.comm(pp); ok {
			_, bg := c.names[truncComm(pn)]
			return bg
		}
	}
	return false
}
