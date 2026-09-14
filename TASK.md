# Task: two small correctness fixes — symlink-safe path join + token torrent match

## Defect 1, CONFIRMED (media_share.py `safe_join`, ~line 275)
`safe_join` builds `full = os.path.normpath(os.path.join(root, rel_path))` and
`root_n = os.path.normpath(root)`. `os.path.normpath` does NOT resolve symlinks, so a symlink
planted inside a library root that points OUTSIDE it passes the `full.startswith(root_n + os.sep)`
check and escapes containment. `/delete-paths` in arr-webhook.py already uses `os.path.realpath`.

## Defect 2, CONFIRMED (arr-webhook.py `find_new_torrent_hash`, ~line 526)
The match is `if torrent_name in new_name or new_name in torrent_name:` — bidirectional substring.
A short/generic torrent name like "21" substrings "2021"/"2160p" and mis-identifies the wrong
torrent. `torrent_matches_any_title` (same file) already does safe whole-word token matching
(and skips variants whose longest word is < 3 chars).

## The property to satisfy
1. `safe_join` must reject a symlink that resolves outside `root` (raise `ValueError`), while
   still allowing a legitimate in-root subdirectory. Achieve by resolving with `os.path.realpath`
   instead of `os.path.normpath` for BOTH `full` and the root.
2. `find_new_torrent_hash` must identify the new torrent by whole-word token match, not substring,
   so a short decoy name does not win. Reuse `torrent_matches_any_title(new_name, [<torrent name>])`.

## Entry points (edit ONLY `media_share.py` and `arr-webhook.py`)
Only edit `media_share.py` and `arr-webhook.py`.
- `media_share.py:277-278` — swap `os.path.normpath` → `os.path.realpath` on both `full` and root.
- `arr-webhook.py:531` — replace the substring `if` condition inside the loop of `find_new_torrent_hash`.
Do not edit any other function, `verify.sh`, or `fixture_pathmatch.py`.

## Exactly what to change
1. In `safe_join`:
   `full = os.path.realpath(os.path.join(root, rel_path))`
   `root_n = os.path.realpath(root)`
2. In `find_new_torrent_hash`, replace
   `if torrent_name in new_name or new_name in torrent_name:`
   with
   `if torrent_matches_any_title(new_name, [info.get('name', '')]):`

## Must contain
- `os.path.realpath(os.path.join(root, rel_path))`
- `torrent_matches_any_title(new_name, [info.get('name', '')])`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints `VERIFY_OK`.
Only edit `media_share.py` and `arr-webhook.py`; do not edit `verify.sh`.
