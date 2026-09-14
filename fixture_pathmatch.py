#!/usr/bin/env python3
"""Adversarial behavioural test for two independent correctness fixes.

#7 media_share.safe_join: os.path.normpath does NOT resolve symlinks, so a
   symlink planted inside a library root that points OUTSIDE it passes the
   prefix check and escapes containment. os.path.realpath resolves it, so the
   escape is caught. /delete-paths already uses realpath; safe_join must too.

#8 arr-webhook.find_new_torrent_hash: bidirectional substring
   (`torrent_name in new_name or new_name in torrent_name`) lets a short/generic
   torrent name (e.g. "21") mis-identify the wrong torrent (it substrings
   "2021"/"2160p"). Whole-word token matching (torrent_matches_any_title) kills
   that false positive.
"""
import os, sys, tempfile
ms = __import__("media_share")
aw = __import__("arr-webhook")

fails = 0
def check(desc, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {'ok ' if ok else 'FAIL'}: {desc}: got {got!r} want {want!r}")

# ── #7 safe_join symlink escape ──────────────────────────────────────────────
print("safe_join:")
base = os.path.realpath(tempfile.mkdtemp())
root = os.path.join(base, "library"); os.makedirs(root)
outside = os.path.join(base, "outside"); os.makedirs(outside)
os.symlink(outside, os.path.join(root, "escape"))          # symlink inside root -> outside
os.makedirs(os.path.join(root, "realsub"))                 # a genuine in-root subdir

def escapes(root, rel):
    """True if safe_join RAISED (rejected the traversal)."""
    try:
        ms.safe_join(root, rel)
        return False
    except ValueError:
        return True

check("symlink escaping root is rejected", escapes(root, "escape"), True)
check("symlink escaping root, with trailing child, rejected", escapes(root, "escape/secret.txt"), True)
check("legitimate in-root subdir is allowed", escapes(root, "realsub"), False)

# ── #8 find_new_torrent_hash token matching ──────────────────────────────────
print("find_new_torrent_hash:")
new_filename = "The.Show.2021.1080p.BluRay.x264-GROUP.mkv"
torrents = {                                    # decoy FIRST so substring bug bites
    "hDECOY": {"name": "21"},                   # substrings "2021"/"1080p"? "21" in "...2021..."
    "hREAL":  {"name": "The Show 2021 1080p"},
}
check("identifies the real torrent, not the substring decoy",
      aw.find_new_torrent_hash(new_filename, torrents), "hREAL")

# a genuinely unrelated set returns None (no false positive from tokens either)
check("no spurious match when nothing shares the title words",
      aw.find_new_torrent_hash("Totally.Different.Movie.2019.mkv",
                               {"hX": {"name": "21"}, "hY": {"name": "Some Other Film 2005"}}), None)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
