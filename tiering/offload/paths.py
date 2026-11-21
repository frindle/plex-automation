"""Share path <-> disk path mapping (see "Path namespaces" in tiering/OFFLOAD_DESIGN.md).

The database holds the share path, the one Plex and the arrs report. File
operations need the path on the disk's own mount. TIERING_PATH_MAP holds one
`share_prefix=disk_prefix` entry per disk, comma separated; several entries may
share one share prefix (one per disk).
"""

from __future__ import annotations

import os


def _norm_prefix(p: str) -> str:
    p = os.path.normpath(p.strip())
    return p if p == "/" else p.rstrip("/")


def parse_path_map(spec) -> list[tuple[str, str]]:
    """Parse `share=disk,share=disk` into (share_prefix, disk_prefix) pairs, longest
    share prefix first (the order of equal-length entries is kept)."""
    pairs: list[tuple[str, str]] = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        share, sep, disk = item.partition("=")
        if not sep or not share.strip() or not disk.strip():
            raise ValueError(f"bad path map entry {item!r}; expected share_prefix=disk_prefix")
        pairs.append((_norm_prefix(share), _norm_prefix(disk)))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)  # sort is stable
    return pairs


def _under(path: str, prefix: str) -> bool:
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


def disk_paths(path, pairs, exists=os.path.lexists) -> list[str]:
    """Every existing disk path for a share path. A path already under a disk
    prefix is kept as is. Only the longest matching share prefix that yields an
    existing candidate counts. The caller refuses unless exactly one comes back."""
    path = os.path.normpath(str(path))
    for _share, disk in pairs:
        if _under(path, disk):
            return [path] if exists(path) else []
    out: list[str] = []
    seen_len = None
    for share, disk in pairs:  # longest share prefix first
        if not _under(path, share):
            continue
        if out and len(share) != seen_len:
            break
        cand = os.path.normpath(disk + path[len(share):] if share != "/" else disk + path)
        if exists(cand):
            if cand not in out:
                out.append(cand)
            seen_len = len(share)
    return out
