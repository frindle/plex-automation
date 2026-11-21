package tierfs

// Kind says where a byte range is served from.
type Kind int

const (
	Head Kind = iota
	Mid
	Tail
)

// Seg is one piece of a read, in file offsets.
type Seg struct {
	Kind Kind
	Off  int64
	N    int64
}

// Split cuts the read [off, off+n) of a file of the given size into head,
// middle and tail pieces, in order. The read is clamped to the file size, so
// reads at or past EOF and zero-length reads give nil.
func Split(size, headLen, tailLen, off, n int64) []Seg {
	if n <= 0 || off < 0 || off >= size {
		return nil
	}
	end := off + n
	if end > size || end < off {
		end = size
	}
	midEnd := size - tailLen
	var out []Seg
	add := func(k Kind, lo, hi int64) {
		if lo < off {
			lo = off
		}
		if hi > end {
			hi = end
		}
		if hi > lo {
			out = append(out, Seg{k, lo, hi - lo})
		}
	}
	add(Head, 0, headLen)
	add(Mid, headLen, midEnd)
	add(Tail, midEnd, size)
	return out
}
