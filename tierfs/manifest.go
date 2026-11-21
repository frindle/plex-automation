package tierfs

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
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
	case m.HeadLen <= 0 || m.TailLen < 0:
		return errors.New("bad head_len/tail_len")
	case m.HeadLen+m.TailLen >= m.Size:
		return errors.New("head_len + tail_len must be below size")
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

type loaded struct {
	m   *Manifest
	raw string
}

// scanManifests reads every <id>.json in dir. Invalid files are reported
// through bad (file name -> reason) and skipped.
func scanManifests(dir string) (good map[string]loaded, bad map[string]string, err error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, nil, err
	}
	good = map[string]loaded{}
	bad = map[string]string{}
	for _, e := range ents {
		n := e.Name()
		if e.IsDir() || !strings.HasSuffix(n, ".json") {
			continue // temp files from atomic writes do not end in .json
		}
		raw, err := os.ReadFile(filepath.Join(dir, n))
		if err != nil {
			bad[n] = err.Error()
			continue
		}
		m, err := ParseManifest(raw)
		if err != nil {
			bad[n] = err.Error()
			continue
		}
		if m.ID+".json" != n {
			bad[n] = "file name does not match id"
			continue
		}
		good[m.Name()] = loaded{m, string(raw)}
	}
	return good, bad, nil
}
