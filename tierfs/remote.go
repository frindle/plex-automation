package tierfs

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/zeebo/blake3"
)

// unitSize is the read unit when a stream has no chunk list to check against.
const unitSize = 256 << 10

var (
	errStreamClosed = errors.New("stream closed")
	// ErrNoRoom means the stream is full and a new one should be started.
	errOutOfWindow = errors.New("offset outside stream window")
)

var chunkPool sync.Pool

func getBuf(n int) *[]byte {
	if v, ok := chunkPool.Get().(*[]byte); ok && cap(*v) >= n {
		return v
	}
	b := make([]byte, n)
	return &b
}

// chunkCheck describes the verified middle: chunk k covers
// [base+k*size, base+(k+1)*size) and must hash to sums[k].
type chunkCheck struct {
	base, size int64
	sums       [][32]byte
}

// alignDown moves off to the start of the chunk that contains it.
func (c *chunkCheck) alignDown(off int64) int64 {
	return c.base + (off-c.base)/c.size*c.size
}

// badChunkError means a chunk failed verification twice.
type badChunkError struct{ Offset int64 }

func (e *badChunkError) Error() string {
	return fmt.Sprintf("chunk at offset %d failed verification", e.Offset)
}

// RemoteConfig configures the HTTP range client shared by all handles.
type RemoteConfig struct {
	Base        string        // e.g. http://altmount:8080
	User, Pass  string        // basic auth, optional
	ReadAhead   int64         // bytes buffered ahead of the reader per stream
	MaxStreams  int           // global cap on open remote streams
	Window      time.Duration // how long to retry before giving up (EIO)
	IdleTimeout time.Duration // an unused stream gives up its slot after this
	BackKeep    int64         // bytes kept behind the reader for reordered reads
}

// Remote makes ranged HTTP requests and owns the global stream cap.
type Remote struct {
	cfg    RemoteConfig
	base   *url.URL
	client *http.Client
	sem    chan struct{}
	m      *Metrics
}

// NewRemote validates the config and builds the client.
func NewRemote(cfg RemoteConfig, m *Metrics) (*Remote, error) {
	u, err := url.Parse(cfg.Base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, errors.New("remote base must be an http(s) URL")
	}
	if u.User != nil { // never keep credentials inside a URL we might log
		if cfg.User == "" {
			cfg.User = u.User.Username()
			cfg.Pass, _ = u.User.Password()
		}
		u.User = nil
	}
	if cfg.ReadAhead <= 0 {
		cfg.ReadAhead = 32 << 20
	}
	if cfg.MaxStreams <= 0 {
		cfg.MaxStreams = 8
	}
	if cfg.Window <= 0 {
		cfg.Window = 10 * time.Second
	}
	if cfg.IdleTimeout <= 0 {
		cfg.IdleTimeout = 30 * time.Second
	}
	if cfg.BackKeep <= 0 {
		cfg.BackKeep = 8 << 20
	}
	if m == nil {
		m = &Metrics{}
	}
	tr := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout: 5 * time.Second,
		MaxIdleConnsPerHost: cfg.MaxStreams + 2,
		IdleConnTimeout:     60 * time.Second,
		DisableCompression:  true, // ranges must be byte-exact
	}
	return &Remote{cfg: cfg, base: u, client: &http.Client{Transport: tr}, sem: make(chan struct{}, cfg.MaxStreams), m: m}, nil
}

func (r *Remote) urlFor(remotePath string) string {
	u := *r.base
	u.Path = strings.TrimRight(r.base.Path, "/") + remotePath
	u.RawPath = ""
	return u.String()
}

// Close drops idle connections.
func (r *Remote) Close() { r.client.CloseIdleConnections() }

type chunk struct {
	off int64
	buf *[]byte
	n   int
}

// stream is one sequential remote read: a single HTTP range request kept
// open and read ahead into a bounded buffer, retried on errors. Cancelling it
// closes the connection at once.
type stream struct {
	r      *Remote
	id     string // manifest id, for logs only
	url    string
	start  int64
	limit  int64 // exclusive end of the range this stream may fetch
	ck     *chunkCheck
	ctx    context.Context
	cancel context.CancelFunc

	dataSig chan struct{} // producer -> reader
	roomSig chan struct{} // reader -> producer
	lastUse atomic.Int64
	done    chan struct{} // closed when the producer goroutine exits

	mu     sync.Mutex
	chunks []chunk
	low    int64 // first retained byte
	end    int64 // one past the last fetched byte
	maxPos int64 // furthest byte the reader has asked for
	err    error
}

// newStream starts fetching [start, limit) of remotePath in the background.
func (r *Remote) newStream(id, remotePath string, start, limit int64) *stream {
	return r.newVerifiedStream(id, remotePath, start, limit, nil)
}

// newVerifiedStream is newStream with every chunk checked against ck before
// it is made readable. start must be chunk-aligned when ck is set.
func (r *Remote) newVerifiedStream(id, remotePath string, start, limit int64, ck *chunkCheck) *stream {
	ctx, cancel := context.WithCancel(context.Background())
	s := &stream{
		r: r, id: id, url: r.urlFor(remotePath), start: start, limit: limit, ck: ck,
		ctx: ctx, cancel: cancel,
		dataSig: make(chan struct{}, 1), roomSig: make(chan struct{}, 1), done: make(chan struct{}),
		low: start, end: start, maxPos: start,
	}
	s.touch()
	go s.run()
	return s
}

func (s *stream) touch() { s.lastUse.Store(time.Now().UnixNano()) }

func notify(c chan struct{}) {
	select {
	case c <- struct{}{}:
	default:
	}
}

func (s *stream) finish(err error) {
	s.mu.Lock()
	if s.err == nil {
		s.err = err
	}
	s.mu.Unlock()
	notify(s.dataSig)
}

// Close cancels the in-flight request and frees the stream's slot.
func (s *stream) Close() {
	s.cancel()
	s.finish(errStreamClosed)
}

// covers reports whether reading at off can be served by this stream without
// restarting it: off is at or after the oldest retained byte and not further
// ahead than the read-ahead window.
func (s *stream) covers(off int64) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if off < s.low || off >= s.end+s.r.cfg.ReadAhead {
		return false
	}
	return s.err == nil || off < s.end
}

func (s *stream) open(off int64, deadline time.Time) (io.ReadCloser, context.CancelFunc, error) {
	rctx, cancel := context.WithCancel(s.ctx)
	t := time.AfterFunc(time.Until(deadline), cancel)
	defer t.Stop()
	req, err := http.NewRequestWithContext(rctx, http.MethodGet, s.url, nil)
	if err != nil {
		cancel()
		return nil, nil, errors.New("bad request")
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", off, s.limit-1))
	if s.r.cfg.User != "" || s.r.cfg.Pass != "" {
		req.SetBasicAuth(s.r.cfg.User, s.r.cfg.Pass)
	}
	t0 := time.Now()
	s.r.m.RemoteReqs.Add(1)
	resp, err := s.r.client.Do(req)
	if err != nil {
		cancel()
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err // the url.Error text would carry the full URL
		}
		return nil, nil, fmt.Errorf("request failed: %w", err)
	}
	s.r.m.ObserveTTFB(time.Since(t0))
	fail := func(format string, a ...any) (io.ReadCloser, context.CancelFunc, error) {
		resp.Body.Close()
		cancel()
		return nil, nil, fmt.Errorf(format, a...)
	}
	if resp.StatusCode != http.StatusPartialContent {
		return fail("remote status %d (want 206)", resp.StatusCode)
	}
	cr := resp.Header.Get("Content-Range") // bytes first-last/total
	if want := "bytes " + strconv.FormatInt(off, 10) + "-"; !strings.HasPrefix(cr, want) {
		return fail("remote returned the wrong range")
	}
	if resp.ContentLength >= 0 && resp.ContentLength != s.limit-off {
		return fail("remote returned %d bytes, want %d", resp.ContentLength, s.limit-off)
	}
	return resp.Body, cancel, nil
}

// waitRoom blocks while the buffer is full. It returns false when the stream
// was cancelled or has been idle too long.
func (s *stream) waitRoom() bool {
	idle := s.r.cfg.IdleTimeout
	for {
		s.mu.Lock()
		room := s.end-s.maxPos < s.r.cfg.ReadAhead
		s.mu.Unlock()
		if room {
			return true
		}
		t := time.NewTimer(idle / 4)
		select {
		case <-s.roomSig:
		case <-s.ctx.Done():
			t.Stop()
			return false
		case <-t.C:
			if time.Since(time.Unix(0, s.lastUse.Load())) > idle {
				s.cancel()
				return false
			}
		}
		t.Stop()
	}
}

func (s *stream) run() {
	defer close(s.done)
	defer s.finish(errStreamClosed)
	select {
	case s.r.sem <- struct{}{}:
	case <-s.ctx.Done():
		return
	}
	s.r.m.ActiveStreams.Add(1)
	defer func() {
		s.r.m.ActiveStreams.Add(-1)
		<-s.r.sem
	}()

	var body io.ReadCloser
	var cancelReq context.CancelFunc
	closeBody := func() {
		if body != nil {
			body.Close()
			cancelReq()
			body = nil
		}
	}
	defer closeBody()

	window := s.r.cfg.Window
	var failStart time.Time
	refetched := int64(-1) // chunk offset that already had its one refetch
	backoff := 250 * time.Millisecond
	// failed handles one error. It returns false when the stream is done.
	failed := func(err error, began time.Time) bool {
		if s.ctx.Err() != nil {
			return false
		}
		s.r.m.RemoteErrors.Add(1)
		log.Printf("stream %s: remote error: %v", s.id, err)
		closeBody()
		if failStart.IsZero() {
			failStart = began
		}
		remaining := window - time.Since(failStart)
		if remaining <= 0 {
			s.finish(fmt.Errorf("remote unavailable: %w", err))
			return false
		}
		d := min(backoff, remaining)
		backoff = min(backoff*2, 2*time.Second)
		select {
		case <-time.After(d):
			return true
		case <-s.ctx.Done():
			return false
		}
	}

	for {
		if !s.waitRoom() {
			return
		}
		s.mu.Lock()
		end := s.end
		s.mu.Unlock()
		if end >= s.limit {
			return
		}
		began := time.Now()
		if body == nil {
			dl := began.Add(window)
			if !failStart.IsZero() {
				dl = failStart.Add(window)
				if m := began.Add(time.Second); dl.Before(m) {
					dl = m
				}
			}
			b, c, err := s.open(end, dl)
			if err != nil {
				if !failed(err, began) {
					return
				}
				continue
			}
			body, cancelReq = b, c
		}
		unit := int64(unitSize)
		if s.ck != nil {
			unit = s.ck.size
		}
		n := int(min(unit, s.limit-end))
		bp := getBuf(n)
		wd := time.AfterFunc(window, cancelReq)
		_, err := io.ReadFull(body, (*bp)[:n])
		wd.Stop()
		if err != nil {
			chunkPool.Put(bp)
			if !failed(err, began) {
				return
			}
			continue
		}
		if s.ck != nil {
			k := (end - s.ck.base) / s.ck.size
			if k < 0 || k >= int64(len(s.ck.sums)) || blake3.Sum256((*bp)[:n]) != s.ck.sums[k] {
				chunkPool.Put(bp)
				closeBody()
				if refetched != end { // one fresh request for this chunk
					refetched = end
					log.Printf("stream %s: chunk at %d failed verification, fetching again", s.id, end)
					continue
				}
				s.finish(&badChunkError{Offset: end})
				return
			}
		}
		failStart, backoff = time.Time{}, 250*time.Millisecond
		s.mu.Lock()
		s.chunks = append(s.chunks, chunk{off: end, buf: bp, n: n})
		s.end += int64(n)
		s.mu.Unlock()
		notify(s.dataSig)
	}
}

// readAt fills p with the bytes at [off, off+len(p)), waiting for the
// producer as needed. It never returns partial data: it is all of p or an
// error. errOutOfWindow means off is behind the retained data.
func (s *stream) readAt(ctx context.Context, p []byte, off int64) error {
	s.touch()
	need := off + int64(len(p))
	stall := s.r.cfg.Window + 2*time.Second
	timer := time.NewTimer(stall)
	defer timer.Stop()
	lastEnd := int64(-1)
	for {
		s.mu.Lock()
		if off < s.low {
			s.mu.Unlock()
			return errOutOfWindow
		}
		if need > s.maxPos {
			s.maxPos = need
		}
		if s.end >= need {
			s.copyOut(p, off)
			s.trim()
			s.mu.Unlock()
			notify(s.roomSig)
			return nil
		}
		if s.err != nil {
			err := s.err
			s.mu.Unlock()
			return err
		}
		end := s.end
		s.mu.Unlock()
		notify(s.roomSig)
		if end != lastEnd { // progress resets the stall timer
			lastEnd = end
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			timer.Reset(stall)
		}
		select {
		case <-s.dataSig:
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
			return errors.New("remote stalled")
		}
	}
}

// copyOut copies [off, off+len(p)) out of the chunks. s.mu must be held.
func (s *stream) copyOut(p []byte, off int64) {
	pos := off
	for _, c := range s.chunks {
		cend := c.off + int64(c.n)
		if cend <= pos {
			continue
		}
		if c.off > pos || len(p) == int(pos-off) {
			break
		}
		n := copy(p[pos-off:], (*c.buf)[pos-c.off:c.n])
		pos += int64(n)
		if pos-off == int64(len(p)) {
			break
		}
	}
}

// trim drops chunks that are far enough behind the reader. s.mu must be held.
func (s *stream) trim() {
	cut := s.maxPos - s.r.cfg.BackKeep
	i := 0
	for i < len(s.chunks) && s.chunks[i].off+int64(s.chunks[i].n) <= cut {
		chunkPool.Put(s.chunks[i].buf)
		s.chunks[i].buf = nil
		i++
	}
	if i > 0 {
		s.chunks = append(s.chunks[:0], s.chunks[i:]...)
	}
	if len(s.chunks) > 0 {
		s.low = s.chunks[0].off
	} else {
		s.low = s.end
	}
}
