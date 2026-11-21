package tierfs

import (
	"fmt"
	"io"
	"sync/atomic"
	"time"
)

var ttfbBuckets = []float64{0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10}

// Metrics holds the process-wide counters, exposed on /metrics.
type Metrics struct {
	BytesLocal    atomic.Int64
	BytesRemote   atomic.Int64
	OpenHandles   atomic.Int64
	ActiveStreams atomic.Int64
	RemoteErrors  atomic.Int64
	RemoteReqs    atomic.Int64
	Manifests     atomic.Int64
	BadChunks     atomic.Int64

	ttfbCount   atomic.Int64
	ttfbSumNS   atomic.Int64
	ttfbBuckets [9]atomic.Int64 // non-cumulative, last is +Inf
}

// ObserveTTFB records the time from sending a remote request to its response
// headers.
func (m *Metrics) ObserveTTFB(d time.Duration) {
	m.ttfbCount.Add(1)
	m.ttfbSumNS.Add(int64(d))
	s := d.Seconds()
	for i, b := range ttfbBuckets {
		if s <= b {
			m.ttfbBuckets[i].Add(1)
			return
		}
	}
	m.ttfbBuckets[len(ttfbBuckets)].Add(1)
}

// WriteProm writes the metrics in Prometheus text format.
func (m *Metrics) WriteProm(w io.Writer) {
	g := func(name, typ, help string, v int64) {
		fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n%s %d\n", name, help, name, typ, name, v)
	}
	g("tierfs_bytes_local_total", "counter", "Bytes served from the local stub.", m.BytesLocal.Load())
	g("tierfs_bytes_remote_total", "counter", "Bytes served from the remote.", m.BytesRemote.Load())
	g("tierfs_open_handles", "gauge", "Open file handles.", m.OpenHandles.Load())
	g("tierfs_active_streams", "gauge", "Remote streams holding a slot.", m.ActiveStreams.Load())
	g("tierfs_remote_errors_total", "counter", "Failed remote requests and body reads.", m.RemoteErrors.Load())
	g("tierfs_remote_requests_total", "counter", "Remote HTTP requests started.", m.RemoteReqs.Load())
	g("tierfs_bad_chunks_total", "counter", "Remote chunks that failed verification.", m.BadChunks.Load())
	g("tierfs_manifests", "gauge", "Valid manifests currently loaded.", m.Manifests.Load())
	const h = "tierfs_remote_ttfb_seconds"
	fmt.Fprintf(w, "# HELP %s Time to first byte (response headers) of remote requests.\n# TYPE %s histogram\n", h, h)
	var cum int64
	for i, b := range ttfbBuckets {
		cum += m.ttfbBuckets[i].Load()
		fmt.Fprintf(w, "%s_bucket{le=\"%g\"} %d\n", h, b, cum)
	}
	cum += m.ttfbBuckets[len(ttfbBuckets)].Load()
	fmt.Fprintf(w, "%s_bucket{le=\"+Inf\"} %d\n", h, cum)
	fmt.Fprintf(w, "%s_sum %g\n%s_count %d\n", h, float64(m.ttfbSumNS.Load())/1e9, h, m.ttfbCount.Load())
}
