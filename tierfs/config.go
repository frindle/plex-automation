package tierfs

import (
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// ParseSize reads a byte count such as 33554432, 32MiB, 32M or 1GiB.
func ParseSize(s string) (int64, error) {
	s = strings.TrimSpace(strings.ToLower(s))
	mult := int64(1)
	for _, u := range []struct {
		suf string
		m   int64
	}{{"gib", 1 << 30}, {"mib", 1 << 20}, {"kib", 1 << 10}, {"g", 1 << 30}, {"m", 1 << 20}, {"k", 1 << 10}, {"b", 1}} {
		if strings.HasSuffix(s, u.suf) {
			s, mult = strings.TrimSpace(strings.TrimSuffix(s, u.suf)), u.m
			break
		}
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("bad size %q", s)
	}
	return n * mult, nil
}

// ConfigFromEnv builds a Config from TIERFS_* variables. getenv is
// os.Getenv in production.
func ConfigFromEnv(getenv func(string) string) (Config, error) {
	var c Config
	c.Mount = getenv("TIERFS_MOUNT")
	c.Manifests = getenv("TIERFS_MANIFESTS")
	c.EventsPath = getenv("TIERFS_EVENTS")
	c.HTTPAddr = getenv("TIERFS_HTTP")
	if c.HTTPAddr == "" {
		c.HTTPAddr = "127.0.0.1:9109"
	}
	c.Remote.Base = getenv("TIERFS_REMOTE_BASE")
	c.Remote.User = getenv("TIERFS_REMOTE_USER")
	c.Remote.Pass = getenv("TIERFS_REMOTE_PASS")
	for _, r := range strings.Split(getenv("TIERFS_STUB_ROOTS"), ",") {
		if r = strings.TrimSpace(r); r != "" {
			if !filepath.IsAbs(r) {
				return c, fmt.Errorf("TIERFS_STUB_ROOTS: %q is not absolute", r)
			}
			c.StubRoots = append(c.StubRoots, filepath.Clean(r))
		}
	}
	c.AllowOther = getenv("TIERFS_ALLOW_OTHER") != "0"
	var err error
	if v := getenv("TIERFS_READAHEAD"); v != "" {
		if c.Remote.ReadAhead, err = ParseSize(v); err != nil {
			return c, fmt.Errorf("TIERFS_READAHEAD: %w", err)
		}
	}
	if v := getenv("TIERFS_MAX_STREAMS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return c, errors.New("TIERFS_MAX_STREAMS must be a positive integer")
		}
		c.Remote.MaxStreams = n
	}
	if v := getenv("TIERFS_SLOT_WAIT"); v != "" {
		if c.Remote.SlotWait, err = time.ParseDuration(v); err != nil || c.Remote.SlotWait <= 0 {
			return c, errors.New("TIERFS_SLOT_WAIT must be a positive duration")
		}
	}
	if v := getenv("TIERFS_POLL"); v != "" {
		if c.PollInterval, err = time.ParseDuration(v); err != nil {
			return c, fmt.Errorf("TIERFS_POLL: %w", err)
		}
	}
	c.BackgroundComms = DefaultBackgroundComms
	if v := strings.TrimSpace(getenv("TIERFS_BACKGROUND_COMMS")); v != "" {
		c.BackgroundComms = nil
		for _, n := range strings.Split(v, ",") {
			if n = strings.TrimSpace(n); n != "" {
				c.BackgroundComms = append(c.BackgroundComms, n)
			}
		}
	}
	if v := getenv("TIERFS_WAIT_S"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return c, errors.New("TIERFS_WAIT_S must be a positive number of seconds")
		}
		c.WaitTimeout = time.Duration(n) * time.Second
	}
	for _, r := range []struct{ name, val string }{
		{"TIERFS_MOUNT", c.Mount}, {"TIERFS_MANIFESTS", c.Manifests}, {"TIERFS_STUB_ROOTS", strings.Join(c.StubRoots, ",")}, {"TIERFS_REMOTE_BASE", c.Remote.Base},
	} {
		if r.val == "" {
			return c, fmt.Errorf("%s is required", r.name)
		}
	}
	return c, nil
}
