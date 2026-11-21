package tierfs

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"syscall"

	"github.com/zeebo/blake3"
)

// Manifest describes one offloaded file. See tiering/OFFLOAD_DESIGN.md.
type Manifest struct {
	V          int    `json:"v"`
	ID         string `json:"id"`
	Ext        string `json:"ext"`
	Size       int64  `json:"size"`
	MtimeNS    int64  `json:"mtime_ns"`
	HeadLen    int64  `json:"head_len"`
	TailLen    int64  `json:"tail_len"`
	StubPath   string `json:"stub_path"`
	RemotePath string `json:"remote_path"`
	SHA256     string `json:"sha256,omitempty"`
	HeadB3     string `json:"head_b3,omitempty"`
	TailB3     string `json:"tail_b3,omitempty"`
	ChunkSize  int64  `json:"chunk_size"`
	ChunksB3   string `json:"chunks_b3"`
}

var (
	idRe  = regexp.MustCompile(`^[0-9a-f]{16}$`)
	extRe = regexp.MustCompile(`^(\.[A-Za-z0-9_+-]{1,16})*$`)
)

// Name is the directory entry name, <id><ext>.
func (m *Manifest) Name() string { return m.ID + m.Ext }

// MidEnd is the end of the remote-served middle range [HeadLen, MidEnd).
func (m *Manifest) MidEnd() int64 { return m.Size - m.TailLen }

// Validate checks a manifest for internal consistency.
func (m *Manifest) Validate() error {
	switch {
	case m.V != 1:
		return fmt.Errorf("unsupported version %d", m.V)
	case !idRe.MatchString(m.ID):
		return errors.New("bad id")
	case !extRe.MatchString(m.Ext):
		return errors.New("bad ext")
	case m.Size <= 0:
		return errors.New("size must be positive")
	case m.HeadLen <= 0 || m.TailLen < 0 || m.HeadLen >= m.Size || m.TailLen >= m.Size:
		return errors.New("bad head_len/tail_len")
	case m.HeadLen >= m.Size-m.TailLen: // written so the sum cannot overflow
		return errors.New("head_len + tail_len must be below size")
	case m.ID == "ffffffffffffffff" || m.ID == "0000000000000000":
		return errors.New("reserved id")
	case m.ChunkSize <= 0 || m.ChunkSize > 16<<20:
		return errors.New("bad chunk_size")
	case len(m.ChunksB3) != 64:
		return errors.New("bad chunks_b3")
	case !filepath.IsAbs(m.StubPath):
		return errors.New("stub_path must be absolute")
	case !strings.HasPrefix(m.RemotePath, "/"):
		return errors.New("remote_path must start with /")
	}
	return nil
}

// ParseManifest decodes and validates one manifest document.
func ParseManifest(b []byte) (*Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	if err := m.Validate(); err != nil {
		return nil, err
	}
	return &m, nil
}

// NumChunks is the number of verified pieces in the middle.
func (m *Manifest) NumChunks() int64 {
	mid := m.MidEnd() - m.HeadLen
	return (mid + m.ChunkSize - 1) / m.ChunkSize
}

// fileKey identifies one version of a file on disk. Manifests are written
// with an atomic rename, so a new write changes the inode.
type fileKey struct {
	mtimeNS, size int64
	ino           uint64
}

func keyOf(fi os.FileInfo) fileKey {
	k := fileKey{mtimeNS: fi.ModTime().UnixNano(), size: fi.Size()}
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		k.ino = uint64(st.Ino)
	}
	return k
}

type loaded struct {
	m    *Manifest
	ver  string // changes whenever the manifest or its chunk list changes
	sums [][32]byte
}

// cached remembers what a previous scan read, so an unchanged manifest or
// chunk list is neither re-read nor re-hashed.
type cached struct {
	jsonKey   fileKey
	m         *Manifest
	mErr      string
	chunkKey  fileKey
	chunksB3  string
	sums      [][32]byte
	chunksErr string
}

// chunkLoads counts chunk list reads (for tests).
var chunkLoads atomic.Int64

// loadChunks reads <id>.chunks and checks it against the manifest.
func loadChunks(dir string, m *Manifest) ([][32]byte, fileKey, error) {
	chunkLoads.Add(1)
	f, err := os.Open(filepath.Join(dir, m.ID+".chunks"))
	if err != nil {
		return nil, fileKey{}, fmt.Errorf("chunk list: %w", err)
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return nil, fileKey{}, fmt.Errorf("chunk list: %w", err)
	}
	want := 32 * m.NumChunks()
	if fi.Size() != want {
		return nil, fileKey{}, fmt.Errorf("chunk list is %d bytes, want %d", fi.Size(), want)
	}
	b, err := io.ReadAll(io.LimitReader(f, want+1))
	if err != nil || int64(len(b)) != want {
		return nil, fileKey{}, errors.New("chunk list changed while reading")
	}
	if got := blake3.Sum256(b); hex.EncodeToString(got[:]) != strings.ToLower(m.ChunksB3) {
		return nil, fileKey{}, errors.New("chunk list does not match chunks_b3")
	}
	sums := make([][32]byte, len(b)/32)
	for i := range sums {
		copy(sums[i][:], b[i*32:])
	}
	return sums, keyOf(fi), nil
}

// stubAllowed reports whether path is exactly <root>/<id><ext> for a root.
func stubAllowed(roots []string, m *Manifest) bool {
	for _, r := range roots {
		if m.StubPath == filepath.Join(r, m.Name()) {
			return true
		}
	}
	return false
}

// scanManifests reads every <id>.json in dir. Invalid files are reported
// through bad (file name -> reason) and skipped. prev is the cache from the
// last scan; the returned cache replaces it.
func scanManifests(dir string, roots []string, prev map[string]*cached) (good map[string]loaded, bad map[string]string, next map[string]*cached, err error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, nil, nil, err
	}
	good = map[string]loaded{}
	bad = map[string]string{}
	next = map[string]*cached{}
	for _, e := range ents {
		n := e.Name()
		if e.IsDir() || !strings.HasSuffix(n, ".json") {
			continue // temp files from atomic writes do not end in .json
		}
		fi, err := e.Info()
		if err != nil {
			bad[n] = err.Error()
			continue
		}
		c := &cached{jsonKey: keyOf(fi)}
		if old := prev[n]; old != nil && old.jsonKey == c.jsonKey {
			c = old
		} else {
			raw, err := os.ReadFile(filepath.Join(dir, n))
			if err != nil {
				bad[n] = err.Error()
				continue
			}
			m, err := ParseManifest(raw)
			switch {
			case err != nil:
				c.mErr = err.Error()
			case m.ID+".json" != n:
				c.mErr = "file name does not match id"
			default:
				c.m = m
			}
		}
		next[n] = c
		if c.mErr != "" {
			bad[n] = c.mErr
			continue
		}
		m := c.m
		if !stubAllowed(roots, m) {
			bad[n] = "stub_path is not <stub root>/<id><ext>"
			continue
		}
		cfi, err := os.Stat(filepath.Join(dir, m.ID+".chunks"))
		if err != nil {
			bad[n] = "chunk list: " + err.Error()
			continue
		}
		if c.sums == nil || c.chunkKey != keyOf(cfi) || c.chunksB3 != m.ChunksB3 {
			sums, key, err := loadChunks(dir, m)
			if err != nil {
				bad[n] = err.Error()
				continue
			}
			c.sums, c.chunkKey, c.chunksB3 = sums, key, m.ChunksB3
		}
		ver := fmt.Sprintf("%v|%v|%s", c.jsonKey, c.chunkKey, c.chunksB3)
		good[m.Name()] = loaded{m, ver, c.sums}
	}
	return good, bad, next, nil
}
