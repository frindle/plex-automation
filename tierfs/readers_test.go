package tierfs

import (
	"bytes"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

const (
	tc   = "/usr/lib/plexmediaserver/Plex Transcoder"
	file = "/data/TV/Show - S01E01.mkv"
	prog = "http://127.0.0.1:32400/video/:/transcode/session/abc/def/progress"
)

// Command lines as Plex Media Server 1.43 runs them (from its log, with names
// and ids replaced).
var (
	loudnessArgs = []string{tc, "-nostats", "-i", file, "-map", "0:1",
		"-af", "loudnorm=I=-16:TP=-1:LRA=9:print_format=json", "-f", "null", "-", "-progressurl", prog}
	loudnessEAEArgs = []string{tc, "-eae_prefix", "loudness-1_", "-i", file, "-map", "0:1",
		"-af", "loudnorm=I=-16:TP=-1:LRA=9:print_format=json", "-f", "null", "-"}
	markerArgs = []string{tc, "-codec:1", "eac3_eae", "-analyzeduration", "20000000", "-i", file,
		"-y", "-nostats", "-progressurl", prog,
		"-filter_complex", "[0:1] aresample=async=1:ochl='mono':osr=8000[0]", "-map", "[0]",
		"-codec:0", "flac", "-b:0", "125k", "-f", "flac", "-map_metadata", "-1",
		"/transcode/Transcode/Detection/323ba1a4/audio.flac"}
	voiceArgs = []string{tc, "-i", file, "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "track-0000.wav"}
	dashArgs  = []string{tc, "-codec:0", "hevc", "-i", file, "-progressurl", prog,
		"-map", "0:0", "-f", "null", "-", "-map", "0:1", "-f", "dash", "-seg_duration", "1", "dash"}
	subtitleArgs = []string{tc, "-ss", "2196", "-i", file, "-progressurl", prog, "-map", "0:2",
		"-codec:0", "copy", "-f", "segment", "-segment_format", "srt",
		"-segment_list", "http://127.0.0.1:32400/video/:/transcode/session/abc/manifest", "chunk-%05d"}
	syncArgs = []string{tc, "-i", file, "-progressurl", prog, "-codec:0", "libx264", "-f", "mp4",
		"/transcode/Sync/item.mp4"}
	// Captured from Plex Media Server 1.43.6 on a real Analyze (homelab job 0168),
	// paths and ids replaced.
	previewArgs = []string{tc, "-codec:v", "h264", "-skip_frame:v", "nokey", "-i", file, "-threads", "0",
		"-nostats", "-q", "3", "-loglevel", "quiet", "-filter_complex",
		"[0:V:0] fps=fps=0.016667:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease [out]",
		"-map", "[out]",
		"/config/Library/Application Support/Plex Media Server/Media/localhost/d/0f3a9c/Contents/Indexes/tmp/img-%06d.jpg",
		"-progressurl", prog}
	marker143Args = []string{tc, "-codec:#0x02", "aac", "-analyzeduration", "20000000", "-probesize", "20000000",
		"-i", file, "-y", "-nostats", "-loglevel", "quiet", "-loglevel_plex", "error", "-progressurl", prog,
		"-filter_complex", "[0:#0x02] aresample=async=1:ochl='mono':rematrix_maxval=0.000000dB:first_pts=0:min_hard_comp=0.01:osr=8000[0]",
		"-map", "[0]", "-codec:0", "flac", "-b:0", "4096k", "-f", "flac", "-map_metadata", "-1",
		"-map_chapters", "-1", "/transcode/plex-marker-0f3a9c"}
	scanThumbArgs = []string{tc, "-codec:v", "h264", "-y", "-threads", "44", "-skip_frame", "noref", "-ss", "2093",
		"-i", file, "-vf", "[in]fps=0.5,scale=w=320:h=320:force_original_aspect_ratio=increase,showinfo[out]",
		"-vsync", "passthrough", "-f", "image2", "thumb-%05d.jpeg"}
)

func TestAnalysisJob(t *testing.T) {
	for _, c := range []struct {
		name string
		args []string
		want bool
	}{
		{"loudness", loudnessArgs, true},
		{"loudness through EAE", loudnessEAEArgs, true},
		{"intro and credits markers", markerArgs, true},
		{"voice activity", voiceArgs, true},
		{"video preview thumbnails (no -f)", previewArgs, true},
		{"intro and credits markers, 1.43.6 output path", marker143Args, true},
		{"scan thumbnails", scanThumbArgs, true},
		{"preview path beside a client stream", append(append([]string{}, previewArgs...), "-f", "dash", "dash"), false},
		{"wav to stdout", []string{tc, "-i", file, "-f", "wav", "-"}, false},
		{"client stream with a null side output", dashArgs, false},
		{"subtitles for a stream", subtitleArgs, false},
		{"sync conversion", syncArgs, false},
		{"flac to a client", []string{tc, "-i", file, "-f", "flac", "pipe:1"}, false},
		{"input only", []string{tc, "-i", file}, false},
		{"empty", nil, false},
	} {
		if got := analysisJob(c.args); got != c.want {
			t.Errorf("%s: analysisJob = %v, want %v", c.name, got, c.want)
		}
	}
}

func writeCmdline(t testing.TB, root string, pid uint32, args []string) {
	t.Helper()
	p := filepath.Join(root, strconv.Itoa(int(pid)), "cmdline")
	if err := os.WriteFile(p, []byte(strings.Join(args, "\x00")+"\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestClassifierTranscoderJobs(t *testing.T) {
	proc := fakeProc(t, map[uint32][2]string{
		100: {"Plex Media Serv", "1"},
		101: {"Plex Transcoder", "100"}, // loudness
		102: {"Plex Transcoder", "100"}, // intro markers
		103: {"Plex Transcoder", "100"}, // voice activity
		104: {"Plex Transcoder", "100"}, // a client stream
		105: {"Plex Transcoder", "100"}, // no readable command line
		106: {"Plex Media Serv", "1"},   // the server itself, whatever its command line
	})
	writeCmdline(t, proc, 101, loudnessArgs)
	writeCmdline(t, proc, 102, markerArgs)
	writeCmdline(t, proc, 103, voiceArgs)
	writeCmdline(t, proc, 104, dashArgs)
	writeCmdline(t, proc, 106, loudnessArgs)
	c := newClassifier(proc, DefaultBackgroundComms)
	for pid, want := range map[uint32]bool{100: false, 101: true, 102: true, 103: true, 104: false, 105: false, 106: false} {
		if got := c.background(ctxPid(pid)); got != want {
			t.Errorf("pid %d: background = %v, want %v", pid, got, want)
		}
	}
	// The server, a client stream and a transcoder whose command line cannot be read are Plex streams.
	for pid, want := range map[uint32]readerKind{100: readerStream, 101: readerBackground, 104: readerStream,
		105: readerStream, 106: readerStream} {
		if got := c.kind(ctxPid(pid)); got != want {
			t.Errorf("pid %d: kind = %v, want %v", pid, got, want)
		}
	}
}

func TestClassifierStreamsAndOtherPlayers(t *testing.T) {
	proc := fakeProc(t, map[uint32][2]string{
		100: {"Plex Media Serv", "1"},
		101: {"Plex DLNA Serve", "100"},
		102: {"ffprobe", "1"},   // an arr reading the media info
		103: {"ffprobe", "100"}, // a child of the server that is not the transcoder
		104: {"Plex Media Scan", "100"},
		105: {"Plex Transcoder", "104"}, // the scanner's transcoder
	})
	c := newClassifier(proc, DefaultBackgroundComms)
	for pid, want := range map[uint32]readerKind{100: readerStream, 101: readerStream, 102: readerPlayer,
		103: readerPlayer, 104: readerBackground, 105: readerBackground, 999: readerPlayer} {
		if got := c.kind(ctxPid(pid)); got != want {
			t.Errorf("pid %d: kind = %v, want %v", pid, got, want)
		}
	}
	// TIERFS_STREAM_COMMS replaces the list; an empty list makes every reader a plain player.
	c = newClassifier(proc, DefaultBackgroundComms).withStreams([]string{})
	if got := c.kind(ctxPid(100)); got != readerPlayer {
		t.Errorf("no stream names: kind = %v", got)
	}
	c = newClassifier(proc, nil).withStreams([]string{"ffprobe"})
	if got := c.kind(ctxPid(102)); got != readerStream {
		t.Errorf("custom stream name: kind = %v", got)
	}
}

// Plex's analysis transcodes read an offloaded file through the remote
// stream: no want, so no promote, and the file is not counted as played.
func TestWaitAnalysisTranscoderUsesRemote(t *testing.T) {
	w := wSetup(t)
	proc := fakeProc(t, map[uint32][2]string{
		100: {"Plex Media Serv", "1"},
		101: {"Plex Transcoder", "100"},
	})
	writeCmdline(t, proc, 101, loudnessArgs)
	w.s.class = newClassifier(proc, DefaultBackgroundComms)
	h := w.open(t)
	off := w.m.HeadLen + 3*w.m.ChunkSize
	b, errno := w.readAs(ctxPid(101), h, off, 4096)
	if errno != 0 || !bytes.Equal(b, w.data[off:off+4096]) {
		t.Fatalf("errno %v", errno)
	}
	if w.rs.reqCount() == 0 {
		t.Fatal("the analysis read did not use the remote")
	}
	if len(w.wants(t)) != 0 {
		t.Error("an analysis read wrote a want event")
	}
}
