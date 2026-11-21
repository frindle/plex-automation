package tierfs

import (
	"reflect"
	"strings"
	"testing"
)

func TestSplit(t *testing.T) {
	const size, head, tail = 1000, 200, 100 // mid is [200, 900)
	cases := []struct {
		name   string
		off, n int64
		want   []Seg
	}{
		{"zero length", 10, 0, nil},
		{"negative length", 10, -5, nil},
		{"at EOF", 1000, 10, nil},
		{"past EOF", 5000, 10, nil},
		{"inside head", 10, 50, []Seg{{Head, 10, 50}}},
		{"head exact", 0, 200, []Seg{{Head, 0, 200}}},
		{"last head byte", 199, 1, []Seg{{Head, 199, 1}}},
		{"first mid byte", 200, 1, []Seg{{Mid, 200, 1}}},
		{"inside mid", 300, 100, []Seg{{Mid, 300, 100}}},
		{"mid exact", 200, 700, []Seg{{Mid, 200, 700}}},
		{"last mid byte", 899, 1, []Seg{{Mid, 899, 1}}},
		{"first tail byte", 900, 1, []Seg{{Tail, 900, 1}}},
		{"inside tail", 950, 20, []Seg{{Tail, 950, 20}}},
		{"head into mid", 190, 20, []Seg{{Head, 190, 10}, {Mid, 200, 10}}},
		{"mid into tail", 890, 20, []Seg{{Mid, 890, 10}, {Tail, 900, 10}}},
		{"head mid tail", 150, 800, []Seg{{Head, 150, 50}, {Mid, 200, 700}, {Tail, 900, 50}}},
		{"whole file", 0, 1000, []Seg{{Head, 0, 200}, {Mid, 200, 700}, {Tail, 900, 100}}},
		{"clamped at EOF", 990, 500, []Seg{{Tail, 990, 10}}},
		{"huge length", 0, 1 << 62, []Seg{{Head, 0, 200}, {Mid, 200, 700}, {Tail, 900, 100}}},
		{"overflow length", 10, 1<<63 - 1, []Seg{{Head, 10, 190}, {Mid, 200, 700}, {Tail, 900, 100}}},
	}
	for _, c := range cases {
		got := Split(size, head, tail, c.off, c.n)
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: got %v want %v", c.name, got, c.want)
		}
	}
}

func TestSplitCoversExactly(t *testing.T) {
	const size, head, tail = 997, 101, 53
	for off := int64(0); off < size; off += 7 {
		for n := int64(1); n < 400; n += 13 {
			segs := Split(size, head, tail, off, n)
			pos := off
			for _, s := range segs {
				if s.Off != pos || s.N <= 0 {
					t.Fatalf("off=%d n=%d: gap or empty seg %v", off, n, segs)
				}
				switch s.Kind {
				case Head:
					if s.Off+s.N > head {
						t.Fatalf("head seg out of range %v", s)
					}
				case Mid:
					if s.Off < head || s.Off+s.N > size-tail {
						t.Fatalf("mid seg out of range %v", s)
					}
				case Tail:
					if s.Off < size-tail {
						t.Fatalf("tail seg out of range %v", s)
					}
				}
				pos += s.N
			}
			want := off + n
			if want > size {
				want = size
			}
			if pos != want {
				t.Fatalf("off=%d n=%d: covered to %d, want %d", off, n, pos, want)
			}
		}
	}
}

func TestManifestValidate(t *testing.T) {
	const tmpl = `{"v":%V,"id":"%ID","ext":"%EXT","size":%SIZE,"mtime_ns":5,"head_len":200,"tail_len":100,"chunk_size":4194304,"chunks_b3":"abababababababababababababababababababababababababababababababab","stub_path":"%STUB","remote_path":"%REMOTE"}`
	build := func(v, id, ext, size, stub, remote string) []byte {
		return []byte(strings.NewReplacer("%V", v, "%ID", id, "%EXT", ext, "%SIZE", size, "%STUB", stub, "%REMOTE", remote).Replace(tmpl))
	}
	const id = "0123456789abcdef"
	if _, err := ParseManifest(build("1", id, ".mkv", "1000", "/s/a.mkv", "/x/y.mkv")); err != nil {
		t.Fatal(err)
	}
	bads := map[string][]byte{
		"bad json":   []byte(`{`),
		"version":    build("2", id, ".mkv", "1000", "/s/a", "/x"),
		"bad id":     build("1", "XYZ", ".mkv", "1000", "/s/a", "/x"),
		"slash ext":  build("1", id, "/../x", "1000", "/s/a", "/x"),
		"no mid":     build("1", id, ".mkv", "300", "/s/a", "/x"),
		"rel stub":   build("1", id, ".mkv", "1000", "s/a", "/x"),
		"rel remote": build("1", id, ".mkv", "1000", "/s/a", "x"),
	}
	for name, doc := range bads {
		if _, err := ParseManifest(doc); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}
