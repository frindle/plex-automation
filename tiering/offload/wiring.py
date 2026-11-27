"""Builds the qualify callable from the environment.

The runner's default TIERING_QUALIFY_FACTORY points at `qualify_from_env`.
The intake modules are imported inside the function, so the runner and its
tests import without them.

Environment (all read by the client constructors, none logged):
  NNTP_PRIMARY_* and NNTP_SECONDARY_*   both providers are required, unless
  TIERING_SINGLE_PROVIDER=1             accepts the primary alone: every STAT then runs on it
                                        only (a first test, or until a second provider is set up)
  NEWZNAB_INDEXERS                      JSON list of indexers
  ALTMOUNT_URL, ALTMOUNT_API_KEY, ALTMOUNT_MOUNT_PATH, ALTMOUNT_CATEGORY
  TIERFS_REMOTE_BASE, TIERFS_REMOTE_USER, TIERFS_REMOTE_PASS   WebDAV reader
  TIERING_NZB_DIR                       NZB archive (default: "nzb" next to TIERING_DB)
  TIERING_NEWZNAB_STATE                 indexer budgets, kept across restarts
                                        (default: "newznab_state.json" next to TIERING_DB)
  TIERING_WEEKLY_REPORT                 weekly report (default: "weekly_report.md" next to TIERING_DB)
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

log = logging.getLogger(__name__)


class WiringError(Exception):
    pass


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


_clients: dict = {}
_clients_lock = threading.Lock()


def _beside_db(e, name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(e.get("TIERING_DB", "."))), name)


class Clients:
    """The clients qualify and the weekly check share. One per process and
    environment: the Newznab client holds the indexer budgets, so two of them
    would spend the same budget twice."""

    def __init__(self, e):
        nntp, newznab, altmount = (_mod(n) for n in ("nntp_stat", "newznab", "altmount"))
        single = (e.get("TIERING_SINGLE_PROVIDER") or "").strip().lower() in ("1", "true", "yes")
        cfgs = {}
        for name in ("primary", "secondary"):
            cfg = nntp.ProviderCfg.from_env(name, e)
            if cfg is None and not (name == "secondary" and single):
                hint = " (TIERING_SINGLE_PROVIDER=1 accepts the primary alone)" if name == "secondary" else ""
                raise WiringError(f"NNTP_{name.upper()}_HOST is not set{hint}")
            cfgs[name] = cfg
        self.archive = e.get("TIERING_NZB_DIR") or _beside_db(e, "nzb")
        self.report = e.get("TIERING_WEEKLY_REPORT") or _beside_db(e, "weekly_report.md")
        state_path = e.get("TIERING_NEWZNAB_STATE") or _beside_db(e, "newznab_state.json")
        self.indexers = newznab.NewznabClient(newznab.load_indexers(e.get("NEWZNAB_INDEXERS", "")),
                                              state_path=state_path)
        self.reader = altmount.WebDavReader()
        self.importer = altmount.ImportClient(reader=self.reader)
        self.stat_primary = nntp.Checker(cfgs["primary"])
        self.stat_secondary = None if cfgs["secondary"] is None else nntp.Checker(cfgs["secondary"])
        if self.stat_secondary is None:
            log.warning("TIERING_SINGLE_PROVIDER: no secondary NNTP provider; every STAT runs on the primary alone")


def clients(env=None) -> Clients:
    e = os.environ if env is None else env
    key = tuple(sorted(e.items()))
    with _clients_lock:  # the demote worker and the weekly check may ask at the same time
        if key not in _clients:
            _clients[key] = Clients(e)
        return _clients[key]


def check(env=None) -> None:
    """Build the clients now. Raises WiringError, or the client's own error, on a missing
    setting. The runner calls it at start in APPLY; a dry run never needs the clients."""
    clients(env)


def qualify_from_env(env=None):
    """Return `qualify(row) -> QualifyResult` with real clients built from env.

    The clients are built on the first call, not here: a dry run plans demotes without
    qualifying anything, so it starts before the providers, indexers and AltMount exist."""
    qualify = _mod("qualify")
    e = dict(os.environ if env is None else env)

    def run(row):
        c = clients(e)
        return qualify.qualify(row, reader=c.reader, importer=c.importer, indexers=c.indexers,
                               stat_primary=c.stat_primary, stat_secondary=c.stat_secondary,
                               archive_dir=c.archive)
    return run


def weekly_from_env(env=None):
    """Return `weekly(db) -> flagged rows`: `weekly.run_weekly` on the shared clients,
    built on the first call like qualify's."""
    weekly = _mod("weekly")
    e = dict(os.environ if env is None else env)

    def run(db):
        c = clients(e)
        return weekly.run_weekly(db, stat_primary=c.stat_primary, stat_secondary=c.stat_secondary,
                                 indexers=c.indexers, report_path=c.report, reader=c.reader)
    return run
