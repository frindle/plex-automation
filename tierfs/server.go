package tierfs

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
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
	Remote       RemoteConfig
}

type entry struct {
	m    *Manifest
	raw  string
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

	root    *rootNode
	fuse    *fuse.Server
	mounted atomic.Bool
	httpSrv *http.Server
	httpLn  net.Listener
	stop    chan struct{}
	wg      sync.WaitGroup
}

// New builds a server. Nothing is mounted or listening until Start.
func New(cfg Config) (*Server, error) {
	if cfg.Mount == "" || cfg.Manifests == "" {
		return nil, errors.New("mount and manifests directories are required")
	}
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
		entries: map[string]*entry{}, badSeen: map[string]string{},
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
	if s.fuse != nil {
		s.mounted.Store(false)
		err = s.fuse.Unmount()
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
	good, bad, err := scanManifests(s.cfg.Manifests)
	if err != nil {
		if s.badSeen[""] != err.Error() {
			s.badSeen[""] = err.Error()
			log.Printf("manifests: %v (keeping the current set)", err)
		}
		return
	}
	delete(s.badSeen, "")
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
		if ok && l.raw == e.raw {
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
			s.entries[name] = &entry{m: l.m, raw: l.raw, sums: l.sums}
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
	n := &fileNode{srv: r.srv, e: e}
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

type fileNode struct {
	fs.Inode
	srv *Server
	e   *entry
}

func (f *fileNode) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	fillAttr(&out.Attr, f.e.m)
	out.SetTimeout(kernelTTL)
	return 0
}

func (f *fileNode) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&syscall.O_ACCMODE != syscall.O_RDONLY || flags&(syscall.O_TRUNC|syscall.O_APPEND) != 0 {
		return nil, 0, syscall.EROFS
	}
	if f.srv.get(f.e.m.Name()) != f.e { // manifest removed since lookup
		return nil, 0, syscall.ENOENT
	}
	h, err := f.srv.openHandle(f.e)
	if err != nil {
		log.Printf("open %s: stub: %v", f.e.m.ID, err)
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
