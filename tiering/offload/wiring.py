"""Builds the qualify callable from the environment.

The runner's default TIERING_QUALIFY_FACTORY points at `qualify_from_env`.
The intake modules are imported inside the function, so the runner and its
tests import without them.

Environment (all read by the client constructors, none logged):
  NNTP_PRIMARY_* and NNTP_SECONDARY_*   both providers are required
  NEWZNAB_INDEXERS                      JSON list of indexers
  ALTMOUNT_URL, ALTMOUNT_API_KEY, ALTMOUNT_MOUNT_PATH, ALTMOUNT_CATEGORY
  TIERFS_REMOTE_BASE, TIERFS_REMOTE_USER, TIERFS_REMOTE_PASS   WebDAV reader
  TIERING_NZB_DIR                       NZB archive (default: "nzb" next to TIERING_DB)
"""
from __future__ import annotations

import importlib
import os


class WiringError(Exception):
    pass


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def qualify_from_env(env=None):
    """Return `qualify(row) -> QualifyResult` with real clients built from env."""
    e = os.environ if env is None else env
    nntp, newznab, altmount, qualify = (_mod(n) for n in
                                        ("nntp_stat", "newznab", "altmount", "qualify"))
    cfgs = {}
    for name in ("primary", "secondary"):
        cfg = nntp.ProviderCfg.from_env(name, e)
        if cfg is None:
            raise WiringError(f"NNTP_{name.upper()}_HOST is not set")
        cfgs[name] = cfg
    archive = e.get("TIERING_NZB_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(e.get("TIERING_DB", "."))), "nzb")
    indexers = newznab.NewznabClient(newznab.load_indexers(e.get("NEWZNAB_INDEXERS", "")))
    reader = altmount.WebDavReader()
    importer = altmount.ImportClient(reader=reader)
    stat_primary = nntp.Checker(cfgs["primary"])
    stat_secondary = nntp.Checker(cfgs["secondary"])

    def run(row):
        return qualify.qualify(row, reader=reader, importer=importer, indexers=indexers,
                               stat_primary=stat_primary, stat_secondary=stat_secondary,
                               archive_dir=archive)
    return run
