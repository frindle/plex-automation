package tierfs

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

const kernelTTL = time.Second

// Config is everything the server needs.
type Config struct {
	Mount        string
	Manifests    string
	EventsPath   string
	EventsMax    int64
	HTTPAddr     string
	AllowOther   bool
	PollInterval time.Duration
	StubRoots    []string // stub_path must be exactly <root>/<id><ext>
	Remote       RemoteConfig
}

type entry struct {
	m    *Manifest
	ver  string
	sums [][32]byte
}

// Server ties the manifest set, the FUSE mount, the remote and the status
// endpoint together.
type Server struct {
	cfg    Config
	m      *Metrics
	remote *Remote
	ev     *EventLog

	reloadMu sync.Mutex
	mu       sync.RWMutex
	entries  map[string]*entry
	badSeen  map[string]string
	cache    map[string]*cached

	root      *rootNode
	fuse      *fuse.Server
	mounted   atomic.Bool
	unmounted atomic.Bool
	httpSrv   *http.Server
	httpLn    net.Listener
	stop      chan struct{}
	wg        sync.WaitGroup
}

// New builds a server. Nothing is mounted or listening until Start.
func New(cfg Config) (*Server, error) {
	if cfg.Mount == "" || cfg.Manifests == "" {
		return nil, errors.New("mount and manifests directories are required")
	}
	if len(cfg.StubRoots) == 0 {
		return nil, errors.New("at least one stub root is required")
	}
	roots := make([]string, 0, len(cfg.StubRoots))
	for _, r := range cfg.StubRoots {
		if !filepath.IsAbs(r) {
			return nil, fmt.Errorf("stub root %q must be absolute", r)
		}
		roots = append(roots, filepath.Clean(r))
	}
	cfg.StubRoots = roots
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = 5 * time.Second
	}
	m := &Metrics{}
	r, err := NewRemote(cfg.Remote, m)
	if err != nil {
		return nil, err
	}
	ev, err := NewEventLog(cfg.EventsPath, cfg.EventsMax)
	if err != nil {
		return nil, fmt.Errorf("events: %w", err)
	}
	return &Server{
		cfg: cfg, m: m, remote: r, ev: ev,
		entries: map[string]*entry{}, badSeen: map[string]string{}, cache: map[string]*cached{},
		stop: make(chan struct{}),
	}, nil
}

// Metrics exposes the counters (used by tests).
func (s *Server) Metrics() *Metrics { return s.m }

// HTTPAddr is the status listener address, once started.
func (s *Server) HTTPAddr() string {
	if s.httpLn == nil {
		return ""
	}
	return s.httpLn.Addr().String()
}

// Start loads the manifests, mounts, and starts polling and the status server.
func (s *Server) Start() error {
	s.Reload()
	s.root = &rootNode{srv: s}
	ttl := kernelTTL
	opts := &fs.Options{
		EntryTimeout: &ttl,
		AttrTimeout:  &ttl,
		MountOptions: fuse.MountOptions{
			FsName:        "tierfs",
			Name:          "tierfs",
			AllowOther:    s.cfg.AllowOther,
			MaxWrite:      1 << 20,
			MaxReadAhead:  1 << 20,
			Options:       []string{"ro"},
			DisableXAttrs: true,
		},
	}
	clearStaleMount(s.cfg.Mount)
	srv, err := fs.Mount(s.cfg.Mount, s.root, opts)
	if err != nil {
		return fmt.Errorf("mount: %w", err)
	}
	s.fuse = srv
	s.mounted.Store(true)
	if s.cfg.HTTPAddr != "" {
		if err := s.startHTTP(); err != nil {
			s.Close()
			return err
		}
	}
	s.wg.Add(1)
	go s.poll()
	return nil
}

// Wait blocks until the filesystem is unmounted.
func (s *Server) Wait() { s.fuse.Wait() }

// Close stops polling and the status server and unmounts.
func (s *Server) Close() error {
	select {
	case <-s.stop:
	default:
		close(s.stop)
	}
	s.wg.Wait()
	if s.httpSrv != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		s.httpSrv.Shutdown(ctx)
		cancel()
	}
	var err error
	if s.fuse != nil && !s.unmounted.Load() {
		s.mounted.Store(false)
		if err = s.fuse.Unmount(); err != nil {
			// Open files keep the mount busy. Detach it lazily so a stop
			// signal always ends the process.
			log.Printf("unmount: %v; detaching lazily", err)
			if lerr := lazyUnmount(s.cfg.Mount); lerr == nil {
				err = nil
			}
		}
		if err == nil {
			s.unmounted.Store(true)
		}
	}
	s.ev.Close()
	s.remote.Close()
	return err
}

func (s *Server) poll() {
	defer s.wg.Done()
	t := time.NewTicker(s.cfg.PollInterval)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			s.Reload()
		case <-s.stop:
			return
		}
	}
}

// Reload rescans the manifest directory. Invalid manifests are logged and
// skipped. A manifest that was valid and becomes unreadable or invalid keeps
// its old entry, so a bad edit cannot take a file out from under a player.
// Open handles are never touched.
func (s *Server) Reload() {
	s.reloadMu.Lock()
	defer s.reloadMu.Unlock()
	good, bad, next, err := scanManifests(s.cfg.Manifests, s.cfg.StubRoots, s.cache)
	if err != nil {
		if s.badSeen[""] != err.Error() {
			s.badSeen[""] = err.Error()
			log.Printf("manifests: %v (keeping the current set)", err)
		}
		return
	}
	delete(s.badSeen, "")
	s.cache = next
	for n, why := range bad {
		if s.badSeen[n] != why {
			s.badSeen[n] = why
			log.Printf("manifests: skipping %s: %s", n, why)
		}
	}
	for n := range s.badSeen {
		if _, ok := bad[n]; !ok && n != "" {
			delete(s.badSeen, n)
		}
	}

	var removed []string
	added := 0
	s.mu.Lock()
	for name, e := range s.entries {
		l, ok := good[name]
		if ok && l.ver == e.ver {
			continue
		}
		if ok { // rewritten: swap in place, the node looks the entry up by name
			s.entries[name] = &entry{m: l.m, ver: l.ver, sums: l.sums}
			continue
		}
		if _, isBad := bad[e.m.ID+".json"]; isBad && !ok {
			continue
		}
		delete(s.entries, name)
		removed = append(removed, name)
	}
	for name, l := range good {
		if _, ok := s.entries[name]; !ok {
			s.entries[name] = &entry{m: l.m, ver: l.ver, sums: l.sums}
			added++
		}
	}
	s.m.Manifests.Store(int64(len(s.entries)))
	s.mu.Unlock()

	if added > 0 || len(removed) > 0 {
		log.Printf("manifests: %d added, %d removed, %d total", added, len(removed), len(good))
	}
	if s.root != nil && s.mounted.Load() {
		for _, name := range removed {
			s.root.RmChild(name)
			s.root.NotifyEntry(name)
		}
	}
}

func (s *Server) get(name string) *entry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.entries[name]
}

func (s *Server) names() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]string, 0, len(s.entries))
	for n := range s.entries {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

func (s *Server) startHTTP() error {
	host, _, err := net.SplitHostPort(s.cfg.HTTPAddr)
	if err != nil {
		return fmt.Errorf("http address: %w", err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return fmt.Errorf("http address %q is not localhost", s.cfg.HTTPAddr)
	}
	ln, err := net.Listen("tcp", s.cfg.HTTPAddr)
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if !s.mounted.Load() {
			http.Error(w, "not mounted", http.StatusServiceUnavailable)
			return
		}
		fmt.Fprintf(w, "ok\nentries %d\n", s.m.Manifests.Load())
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		s.m.WriteProm(w)
	})
	s.httpLn = ln
	s.httpSrv = &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go s.httpSrv.Serve(ln)
	return nil
}

// ---- FUSE nodes ----

func inoOf(id string) uint64 {
	n, err := strconv.ParseUint(id, 16, 64)
	if err != nil || n == 0 {
		return 1 << 62
	}
	return n
}

func fillAttr(a *fuse.Attr, m *Manifest) {
	sec, nsec := m.MtimeNS/1e9, m.MtimeNS%1e9
	if nsec < 0 {
		sec, nsec = sec-1, nsec+1e9
	}
	a.Mode = syscall.S_IFREG | 0o444
	a.Nlink = 1
	a.Size = uint64(m.Size)
	a.Blocks = uint64((m.Size + 511) / 512)
	a.Blksize = 4096
	a.Mtime, a.Mtimensec = uint64(sec), uint32(nsec)
	a.Atime, a.Atimensec = a.Mtime, a.Mtimensec
	a.Ctime, a.Ctimensec = a.Mtime, a.Mtimensec
}

type rootNode struct {
	fs.Inode
	srv *Server
}

var (
	_ fs.NodeGetattrer = (*rootNode)(nil)
	_ fs.NodeLookuper  = (*rootNode)(nil)
	_ fs.NodeReaddirer = (*rootNode)(nil)
	_ fs.NodeGetattrer = (*fileNode)(nil)
	_ fs.NodeOpener    = (*fileNode)(nil)
	_ fs.FileReader    = (*handle)(nil)
	_ fs.FileReleaser  = (*handle)(nil)
)

func (r *rootNode) Getattr(ctx context.Context, f fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	out.Mode = syscall.S_IFDIR | 0o555
	out.Nlink = 2
	out.SetTimeout(kernelTTL)
	return 0
}

func (r *rootNode) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	e := r.srv.get(name)
	if e == nil {
		return nil, syscall.ENOENT
	}
	fillAttr(&out.Attr, e.m)
	out.SetEntryTimeout(kernelTTL)
	out.SetAttrTimeout(kernelTTL)
	if c := r.GetChild(name); c != nil {
		return c, 0
	}
	n := &fileNode{srv: r.srv, name: name}
	return r.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG, Ino: inoOf(e.m.ID)}), 0
}

func (r *rootNode) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	names := r.srv.names()
	list := make([]fuse.DirEntry, 0, len(names))
	for _, n := range names {
		if e := r.srv.get(n); e != nil {
			list = append(list, fuse.DirEntry{Name: n, Mode: syscall.S_IFREG, Ino: inoOf(e.m.ID)})
		}
	}
	return fs.NewListDirStream(list), 0
}

// fileNode resolves its manifest by name on every call. go-fuse reuses a node
// for the same inode number, so a node can outlive the entry it was made for
// (a manifest rewritten, or removed and added again, while a file is open).
type fileNode struct {
	fs.Inode
	srv  *Server
	name string
}

func (f *fileNode) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	e := f.srv.get(f.name)
	if e == nil {
		return syscall.ENOENT
	}
	fillAttr(&out.Attr, e.m)
	out.SetTimeout(kernelTTL)
	return 0
}

func (f *fileNode) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&syscall.O_ACCMODE != syscall.O_RDONLY || flags&(syscall.O_TRUNC|syscall.O_APPEND) != 0 {
		return nil, 0, syscall.EROFS
	}
	e := f.srv.get(f.name)
	if e == nil { // manifest removed since lookup
		return nil, 0, syscall.ENOENT
	}
	h, err := f.srv.openHandle(e)
	if err != nil {
		log.Printf("open %s: stub: %v", e.m.ID, err)
		return nil, 0, syscall.EIO
	}
	// Direct I/O makes the kernel pass reads through at the exact offset and
	// size the application asked for. With the page cache, a read that starts
	// near the tail would be rounded down to a page boundary and reach a few
	// bytes into the middle, and that would cost a remote request (and a
	// false "synced" event) for a tail-only read. It also keeps every read
	// visible to us, so remote_read events cannot be hidden by cached pages.
	return h, fuse.FOPEN_DIRECT_IO, 0
}

// lazyUnmount detaches the mount at once, even with files open.
func lazyUnmount(mnt string) error {
	if err := syscall.Unmount(mnt, 0x2); err == nil { // MNT_DETACH
		return nil
	}
	var last error
	for _, bin := range []string{"fusermount3", "fusermount"} {
		if p, err := exec.LookPath(bin); err == nil {
			if last = exec.Command(p, "-uz", mnt).Run(); last == nil {
				return nil
			}
		}
	}
	if last == nil {
		last = errors.New("no way to unmount lazily")
	}
	return last
}

// clearStaleMount removes a dead mount left at mnt by a killed process
// (stat fails with "transport endpoint is not connected").
func clearStaleMount(mnt string) {
	clearStaleMountWith(mnt, func(p string) error { _, err := os.Stat(p); return err }, lazyUnmount)
}

// clearStaleMountWith holds the decision: only a dead-transport error from
// stat triggers an unmount. It reports whether an unmount was attempted.
func clearStaleMountWith(mnt string, stat func(string) error, unmount func(string) error) bool {
	if err := stat(mnt); !errors.Is(err, syscall.ENOTCONN) {
		return false
	}
	log.Printf("clearing stale mount at %s", mnt)
	if err := unmount(mnt); err != nil {
		log.Printf("clearing stale mount: %v", err)
	}
	return true
}
