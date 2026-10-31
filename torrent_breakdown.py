"""Read-only breakdown of the Deluge torrent set (label, state, and the
unlabeled/'other' group by seeded age, save_path prefix and sample names)."""

import posixpath
import episode_retention

FIELDS = ["name", "label", "state", "progress", "seeding_time", "save_path"]

LIBRARY_ROOTS = ("/data/Media/Movies", "/data/Media/TV Shows", "/data/Media/TV Shows - Asian")


def breakdown(torrents):
    result = {
        "total": len(torrents),
        "sonarr": {
            "count": 0,
            "by_label": {},
            "by_state": {},
            "by_age": {"<14d": 0, "14-21d": 0, "21d+": 0},
            "by_kind": {"episode": 0, "season-pack": 0},
        },
        "library_seed": {"count": 0, "by_label": {}, "by_root": {}},
        "unscoped": 0,
    }

    sonarrs = []
    library_seeds = []

    for h, t in torrents.items():
        if t is None:
            t = {}
        label = (t.get("label") or "").lower()

        # Check if sonarr group
        is_sonarr = label.startswith("sonarr")

        # Check if library_seed group
        save_path = t.get("save_path")
        is_library = False
        matched_root = None
        if isinstance(save_path, str) and save_path:
            sp_norm = posixpath.normpath(save_path).lower()
            for root in LIBRARY_ROOTS:
                root_lower = root.lower()
                if sp_norm == root_lower or sp_norm.startswith(root_lower + "/"):
                    is_library = True
                    matched_root = root
                    break

        if is_sonarr:
            sonarrs.append((h, t, label))
        if is_library:
            library_seeds.append((h, t, matched_root, label))

        if not is_sonarr and not is_library:
            result["unscoped"] += 1

    # Process sonarr torrents
    for h, t, label in sonarrs:
        result["sonarr"]["count"] += 1

        # by_label: use lowercased label
        result["sonarr"]["by_label"][label] = result["sonarr"]["by_label"].get(label, 0) + 1

        # by_state
        st = t.get("state")
        if st is None:
            st = "(unknown)"
        result["sonarr"]["by_state"][st] = result["sonarr"]["by_state"].get(st, 0) + 1

        # by_age
        st_val = t.get("seeding_time") or 0
        if st_val < 14 * 86400:
            bucket = "<14d"
        elif st_val < 21 * 86400:
            bucket = "14-21d"
        else:
            bucket = "21d+"
        result["sonarr"]["by_age"][bucket] += 1

        # by_kind
        kind = episode_retention.classify_light(t)
        if kind in ("episode", "season-pack"):
            result["sonarr"]["by_kind"][kind] += 1

    # Process library_seed torrents
    for h, t, root, label in library_seeds:
        result["library_seed"]["count"] += 1

        # by_label: empty -> "(none)"
        lbl = label
        if lbl == "":
            lbl = "(none)"
        result["library_seed"]["by_label"][lbl] = result["library_seed"]["by_label"].get(lbl, 0) + 1

        # by_root
        result["library_seed"]["by_root"][root] = result["library_seed"]["by_root"].get(root, 0) + 1

    return result
