"""SQLite state store for the offload tier (see tiering/OFFLOAD_DESIGN.md)."""

from __future__ import annotations

import json
import sqlite3
import time

STATES = ("whole", "offloading", "offloaded", "promoting", "failed", "never")

COLUMNS = (
    "id", "lib_path", "stub_path", "remote_path",
    "size", "mtime_ns", "sha256", "head_len", "tail_len", "head_b3", "tail_b3",
    "nzb_path", "nzb_path2", "twin_nzb_path", "twin_remote_path",
    "title", "year", "imdb_id", "tvdb_id", "season", "episode", "release_name",
    "dev", "ino",
    "rating_key", "kind", "is4k",
    "state", "step", "restore_score", "restore_note",
    "last_check_ts", "last_stat_ok_ts", "last_ok_ts", "created_ts", "updated_ts",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    lib_path TEXT,
    stub_path TEXT,
    remote_path TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    head_len INTEGER,
    tail_len INTEGER,
    head_b3 TEXT,
    tail_b3 TEXT,
    nzb_path TEXT,
    nzb_path2 TEXT,
    twin_nzb_path TEXT,
    twin_remote_path TEXT,
    title TEXT,
    year INTEGER,
    imdb_id TEXT,
    tvdb_id TEXT,
    season INTEGER,
    episode INTEGER,
    release_name TEXT,
    dev INTEGER,
    ino INTEGER,
    rating_key TEXT,
    kind TEXT CHECK (kind IS NULL OR kind IN ('movie', 'episode')),
    is4k INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'whole'
        CHECK (state IN ('whole', 'offloading', 'offloaded', 'promoting', 'failed', 'never')),
    step INTEGER NOT NULL DEFAULT 0,
    restore_score REAL,
    restore_note TEXT,
    last_check_ts REAL,
    last_stat_ok_ts REAL,
    last_ok_ts REAL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS files_lib_path ON files(lib_path) WHERE lib_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_rating_key ON files(rating_key);
CREATE INDEX IF NOT EXISTS files_state ON files(state);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_id ON events(id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS last_plays (
    rating_key TEXT PRIMARY KEY,
    last_ts REAL NOT NULL
);
"""


def open_db(path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(_SCHEMA)
    _migrate(db)
    return db


def _migrate(db) -> None:
    """Bring an older database up to date in ONE transaction (BEGIN IMMEDIATE ...
    COMMIT; sqlite3 would otherwise autocommit each ALTER), so a crash leaves
    either the old or the new layout. A half-done TEXT->REAL rebuild left by an
    earlier version (a last_stat_ok_ts_old column) is finished here."""
    db.commit()
    saved = db.isolation_level
    db.isolation_level = None  # explicit: we issue BEGIN/COMMIT ourselves
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            info = {r[1]: (r[2] or "").upper() for r in db.execute("PRAGMA table_info(files)")}
            ctype = {"year": "INTEGER", "season": "INTEGER", "episode": "INTEGER",
                     "dev": "INTEGER", "ino": "INTEGER", "last_stat_ok_ts": "REAL"}
            for c in ("title", "year", "imdb_id", "tvdb_id", "season", "episode",
                      "release_name", "dev", "ino", "last_stat_ok_ts",
                      "twin_nzb_path", "twin_remote_path"):
                if c not in info:  # databases created before these columns existed
                    db.execute(f"ALTER TABLE files ADD COLUMN {c} {ctype.get(c, 'TEXT')}")
                    info[c] = ctype.get(c, "TEXT")
            if "last_stat_ok_ts_old" not in info and info.get("last_stat_ok_ts") != "REAL":
                # an earlier migration declared it TEXT: rebuild it as REAL, keeping the values
                db.execute("ALTER TABLE files RENAME COLUMN last_stat_ok_ts "
                           "TO last_stat_ok_ts_old")
                db.execute("ALTER TABLE files ADD COLUMN last_stat_ok_ts REAL")
                info["last_stat_ok_ts_old"] = "TEXT"
            if "last_stat_ok_ts_old" in info:  # new or half-done: finish it
                db.execute("UPDATE files SET last_stat_ok_ts = CAST(last_stat_ok_ts_old AS REAL) "
                           "WHERE last_stat_ok_ts_old IS NOT NULL AND last_stat_ok_ts IS NULL")
                db.execute("ALTER TABLE files DROP COLUMN last_stat_ok_ts_old")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    finally:
        db.isolation_level = saved


def _check_state(state):
    if state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {STATES}")


def _check_columns(fields):
    bad = [k for k in fields if k not in COLUMNS]
    if bad:
        raise ValueError(f"unknown column(s): {', '.join(sorted(bad))}")


def _row(r):
    return dict(r) if r is not None else None


def get(db, id) -> dict | None:
    return _row(db.execute("SELECT * FROM files WHERE id=?", (id,)).fetchone())


def by_path(db, lib_path) -> dict | None:
    return _row(db.execute("SELECT * FROM files WHERE lib_path=?", (lib_path,)).fetchone())


def by_rating_key(db, rating_key) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM files WHERE rating_key=? ORDER BY lib_path, id", (str(rating_key),)
    ).fetchall()
    return [dict(r) for r in rows]


def upsert(db, row: dict) -> None:
    """Insert or update by id. Only the supplied columns are touched on update."""
    if not row.get("id"):
        raise ValueError("row needs an id")
    _check_columns(row)
    if row.get("state") is not None:
        _check_state(row["state"])
    now = time.time()
    fields = {k: v for k, v in row.items() if k not in ("created_ts", "updated_ts")}
    fields = {k: v for k, v in fields.items() if not (k in ("state", "step", "is4k") and v is None)}
    if fields.get("rating_key") is not None:
        fields["rating_key"] = str(fields["rating_key"])
    with db:
        if get(db, row["id"]) is None:
            cols = {**fields, "created_ts": row.get("created_ts") or now, "updated_ts": now}
            db.execute(
                f"INSERT INTO files ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                list(cols.values()),
            )
        else:
            sets = {k: v for k, v in fields.items() if k != "id"}
            sets["updated_ts"] = now
            db.execute(
                f"UPDATE files SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                [*sets.values(), row["id"]],
            )


def set_state(db, id, state, **fields) -> None:
    _check_state(state)
    _check_columns(fields)
    if "id" in fields or "created_ts" in fields:
        raise ValueError("id and created_ts cannot be changed")
    sets = {**fields, "state": state, "updated_ts": time.time()}
    with db:
        cur = db.execute(
            f"UPDATE files SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
            [*sets.values(), id],
        )
    if cur.rowcount != 1:
        raise KeyError(f"no such file id {id!r}")


def update_if(db, id, expect_state, **fields) -> bool:
    """One UPDATE ... WHERE id=? AND state=?. Fields may include state. Returns
    False, changing nothing, if the row is gone or its state has moved on."""
    _check_state(expect_state)
    _check_columns(fields)
    if "id" in fields or "created_ts" in fields or "updated_ts" in fields:
        raise ValueError("id, created_ts and updated_ts cannot be set")
    if fields.get("state") is not None:
        _check_state(fields["state"])
    elif "state" in fields:
        raise ValueError("state cannot be None")
    sets = {**fields, "updated_ts": time.time()}
    with db:
        cur = db.execute(
            f"UPDATE files SET {', '.join(f'{k}=?' for k in sets)} WHERE id=? AND state=?",
            [*sets.values(), id, expect_state],
        )
    return cur.rowcount == 1


def list_files(db, state=None) -> list[dict]:
    if state is None:
        rows = db.execute("SELECT * FROM files ORDER BY created_ts, id").fetchall()
    else:
        _check_state(state)
        rows = db.execute(
            "SELECT * FROM files WHERE state=? ORDER BY created_ts, id", (state,)
        ).fetchall()
    return [dict(r) for r in rows]


def log_event(db, id, kind, detail: dict) -> None:
    with db:
        db.execute(
            "INSERT INTO events (id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (id, time.time(), kind, json.dumps(detail, sort_keys=True, default=str)),
        )


def events(db, id=None, after_seq=0) -> list[dict]:
    """Helper (not in the contract) to read events back, oldest first. `after_seq`
    returns only rows with a larger seq (an incremental read)."""
    if id is None:
        rows = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (after_seq,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM events WHERE id=? AND seq>? ORDER BY seq",
                          (id, after_seq)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        out.append(d)
    return out


def kv_get(db, key, default=None):
    r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(db, key, value) -> None:
    with db:
        db.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, sort_keys=True)))


def last_plays(db) -> dict:
    """{rating_key: last play time} the runner has seen, kept so a wiped Plex history
    cannot make a played file look idle."""
    return {r[0]: r[1] for r in db.execute("SELECT rating_key, last_ts FROM last_plays")}


def merge_last_plays(db, plays: dict) -> None:
    """Keep the larger of the stored and the given time per rating key."""
    with db:
        db.executemany(
            "INSERT INTO last_plays (rating_key, last_ts) VALUES (?, ?) "
            "ON CONFLICT(rating_key) DO UPDATE SET last_ts=MAX(last_ts, excluded.last_ts)",
            [(str(k), float(v)) for k, v in plays.items()])
