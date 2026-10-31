"""Read-only breakdown of the Deluge torrent set (label, state, and the
unlabeled/'other' group by seeded age, save_path prefix and sample names)."""

FIELDS = ["name", "label", "state", "progress", "seeding_time", "save_path"]


def breakdown(torrents):
    raise NotImplementedError("not implemented: breakdown")
