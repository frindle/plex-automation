package tierfs

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
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
// "watch-align" is the webhook's ffmpeg matching the sound of an old and a new copy before an upgrade:
// a few short windows, which must not promote the file.
var DefaultBackgroundComms = []string{"Plex Media Scan", "watch-align"}

// DefaultStreamComms names Plex's own streaming processes. A read by one of them is someone pressing
// play (or a sync), so it asks for the file on its first read. Any other player (an arr's ffprobe, a
// subtitle tool) only reads the start and the index, so it asks at the last quarter of the head.
var DefaultStreamComms = []string{"Plex Media Server", "Plex Transcoder", "Plex DLNA Server"}

// readerKind is what a reader is to tierfs.
type readerKind uint8

const (
	readerPlayer     readerKind = iota // waits for the middle; asks for it at the last quarter of the head
	readerStream                       // a Plex stream: waits for the middle; asks for it on its first read
	readerBackground                   // the scanner and analysis jobs: reads the middle from the remote
)

type classEntry struct {
	kind readerKind
	at   time.Time
}

// classifier tells background readers (the Plex scanner and Plex's analysis
// transcodes) from players, using the caller's pid from the FUSE request. A pid
// it cannot read is a player.
type classifier struct {
	procRoot string
	names    map[string]struct{}
	streams  map[string]struct{}

	mu    sync.Mutex
	cache map[uint32]classEntry
}

func newClassifier(procRoot string, comms []string) *classifier {
	if procRoot == "" {
		procRoot = "/proc"
	}
	c := &classifier{procRoot: procRoot, names: commSet(comms), cache: map[uint32]classEntry{}}
	c.streams = commSet(DefaultStreamComms)
	return c
}

// withStreams replaces the streaming process names (nil keeps the default).
func (c *classifier) withStreams(comms []string) *classifier {
	if comms != nil {
		c.streams = commSet(comms)
	}
	return c
}

func commSet(comms []string) map[string]struct{} {
	set := map[string]struct{}{}
	for _, n := range comms {
		if n = strings.TrimSpace(n); n != "" {
			set[truncComm(n)] = struct{}{}
		}
	}
	return set
}

func truncComm(s string) string {
	if len(s) > commMax {
		return s[:commMax]
	}
	return s
}

// background reports whether the request in ctx comes from a background reader.
func (c *classifier) background(ctx context.Context) bool {
	return c.kind(ctx) == readerBackground
}

// kind classifies the caller of the request in ctx. A caller it cannot see is a player.
func (c *classifier) kind(ctx context.Context) readerKind {
	caller, ok := fuse.FromContext(ctx)
	if !ok || caller.Pid == 0 || len(c.names)+len(c.streams) == 0 {
		return readerPlayer
	}
	pid := caller.Pid
	now := time.Now()
	c.mu.Lock()
	if e, ok := c.cache[pid]; ok && now.Sub(e.at) < classTTL {
		c.mu.Unlock()
		return e.kind
	}
	c.mu.Unlock()
	k := c.classify(pid)
	c.mu.Lock()
	if len(c.cache) >= classCacheMax {
		c.cache = map[uint32]classEntry{}
	}
	c.cache[pid] = classEntry{k, now}
	c.mu.Unlock()
	return k
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

// tgid maps a thread id (what the FUSE request carries) to its process id.
func (c *classifier) tgid(tid uint32) (uint32, bool) {
	b, err := os.ReadFile(filepath.Join(c.procRoot, strconv.FormatUint(uint64(tid), 10), "status"))
	if err != nil {
		return 0, false
	}
	for _, ln := range strings.Split(string(b), "\n") {
		if v, ok := strings.CutPrefix(ln, "Tgid:"); ok {
			n, err := strconv.ParseUint(strings.TrimSpace(v), 10, 32)
			return uint32(n), err == nil && n > 0
		}
	}
	return 0, false
}

// classify looks at the comm of the caller's process (the thread group
// leader, so a renamed worker thread does not matter) and of its parent.
func (c *classifier) classify(tid uint32) readerKind {
	pid, ok := c.tgid(tid)
	if !ok {
		return readerPlayer // cannot resolve it: a player
	}
	name, ok := c.comm(pid)
	if !ok {
		return readerPlayer // cannot read it: a player
	}
	if _, bg := c.names[truncComm(name)]; bg {
		return readerBackground
	}
	if pp, ok := c.ppid(pid); ok {
		if pn, ok := c.comm(pp); ok {
			if _, bg := c.names[truncComm(pn)]; bg {
				return readerBackground
			}
		}
	}
	if name == transcoderComm {
		if args, ok := c.cmdline(pid); ok && analysisJob(args) {
			return readerBackground
		}
	}
	if _, st := c.streams[truncComm(name)]; st {
		return readerStream
	}
	return readerPlayer
}

func (c *classifier) cmdline(pid uint32) ([]string, bool) {
	b, err := os.ReadFile(filepath.Join(c.procRoot, strconv.FormatUint(uint64(pid), 10), "cmdline"))
	if err != nil || len(b) == 0 {
		return nil, false
	}
	return strings.Split(strings.TrimRight(string(b), "\x00"), "\x00"), true
}

// transcoderComm is the comm of the Plex Transcoder. Plex Media Server starts
// it both to stream to a client and for its own analysis jobs, so its command
// line tells the two apart.
const transcoderComm = "Plex Transcoder"

// analysisFormats are the muxers Plex's analysis jobs write. Any other -f
// value (dash, segment, hls, mpegts, mp4, matroska, ...) is a client stream or
// a sync or optimize conversion.
var analysisFormats = map[string]bool{"null": true, "flac": true, "wav": true, "s16le": true, "image2": true}

// audioFormats are written to a file by the marker and voice jobs; written to
// a pipe they could be a client.
var audioFormats = map[string]bool{"flac": true, "wav": true, "s16le": true}

var trackWav = regexp.MustCompile(`^track-\d+\.wav$`)

// analysisJob reports whether a Plex Transcoder command line is one of Plex's
// analysis jobs, which read the file but send nothing to a client:
//   - loudness: "-af loudnorm=... -f null -"
//   - intro and credits markers: mono 8 kHz audio, "-f flac" to a file
//     (under .../Transcode/Detection/ in 1.43)
//   - voice activity: 16 kHz mono PCM written to track-NNNN.wav
//   - video preview thumbnails: keyframes to .../Contents/Indexes/tmp/img-%06d.jpg,
//     with no -f at all
//   - any "-f image2" output (chapter and scan thumbnails)
//
// A command line that also names any other muxer is a player, and so is one
// that matches none of these.
func analysisJob(args []string) bool {
	job, audio, pipe := false, false, false
	for i, a := range args {
		if a == "-f" && i+1 < len(args) {
			f := args[i+1]
			if !analysisFormats[f] {
				return false
			}
			if f == "null" || f == "image2" {
				job = true
			}
			if audioFormats[f] {
				audio = true
			}
		}
		if a == "-" || strings.HasPrefix(a, "pipe:") {
			pipe = true
		}
		if strings.Contains(a, "/Transcode/Detection/") || strings.Contains(a, "/Contents/Indexes/") ||
			trackWav.MatchString(a) {
			job = true
		}
	}
	return job || (audio && !pipe)
}
