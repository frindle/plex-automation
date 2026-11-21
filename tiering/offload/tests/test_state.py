import pytest

from tiering.offload import state as st


@pytest.fixture
def db(tmp_path):
    d = st.open_db(tmp_path / "t.db")
    yield d
    d.close()


def test_wal_and_tables(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cols = [r[1] for r in db.execute("PRAGMA table_info(files)")]
    assert tuple(cols) == st.COLUMNS


def test_open_db_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    st.open_db(p).close()
    d = st.open_db(p)
    assert st.list_files(d) == []


def test_upsert_get_roundtrip_and_partial_update(db):
    st.upsert(db, {"id": "a" * 16, "lib_path": "/lib/a.mkv", "size": 5, "rating_key": 42,
                   "kind": "movie"})
    r = st.get(db, "a" * 16)
    assert r["state"] == "whole" and r["step"] == 0 and r["is4k"] == 0
    assert r["rating_key"] == "42"
    assert r["created_ts"] and r["updated_ts"]
    st.upsert(db, {"id": "a" * 16, "size": 9})
    r2 = st.get(db, "a" * 16)
    assert r2["size"] == 9 and r2["lib_path"] == "/lib/a.mkv"
    assert r2["created_ts"] == r["created_ts"]
    assert st.get(db, "nope") is None


def test_by_path_and_rating_key(db):
    st.upsert(db, {"id": "a" * 16, "lib_path": "/l/a.mkv", "rating_key": "7"})
    st.upsert(db, {"id": "b" * 16, "lib_path": "/l/b.mkv", "rating_key": "7"})
    st.upsert(db, {"id": "c" * 16, "lib_path": "/l/c.mkv", "rating_key": "8"})
    assert st.by_path(db, "/l/b.mkv")["id"] == "b" * 16
    assert st.by_path(db, "/l/x.mkv") is None
    assert [r["id"] for r in st.by_rating_key(db, 7)] == ["a" * 16, "b" * 16]
    assert st.by_rating_key(db, "9") == []


def test_lib_path_is_unique(db):
    st.upsert(db, {"id": "a" * 16, "lib_path": "/l/a.mkv"})
    with pytest.raises(Exception):
        st.upsert(db, {"id": "b" * 16, "lib_path": "/l/a.mkv"})


def test_state_names_validated(db):
    assert st.STATES == ("whole", "offloading", "offloaded", "promoting", "failed", "never")
    with pytest.raises(ValueError):
        st.upsert(db, {"id": "a" * 16, "state": "bogus"})
    st.upsert(db, {"id": "a" * 16})
    with pytest.raises(ValueError):
        st.set_state(db, "a" * 16, "bogus")
    for s in st.STATES:
        st.set_state(db, "a" * 16, s)
        assert st.get(db, "a" * 16)["state"] == s
    # the schema itself also refuses a bad state
    with pytest.raises(Exception):
        db.execute("UPDATE files SET state='bogus'")


def test_set_state_fields_and_updated_ts(db):
    st.upsert(db, {"id": "a" * 16})
    before = st.get(db, "a" * 16)["updated_ts"]
    st.set_state(db, "a" * 16, "offloading", step=3, restore_note="n")
    r = st.get(db, "a" * 16)
    assert (r["state"], r["step"], r["restore_note"]) == ("offloading", 3, "n")
    assert r["updated_ts"] >= before
    with pytest.raises(ValueError):
        st.set_state(db, "a" * 16, "whole", nope=1)
    with pytest.raises(KeyError):
        st.set_state(db, "missing", "whole")


def test_unknown_column_rejected(db):
    with pytest.raises(ValueError):
        st.upsert(db, {"id": "a" * 16, "x; DROP TABLE files": 1})


def test_list_files_filter(db):
    st.upsert(db, {"id": "a" * 16, "state": "offloaded"})
    st.upsert(db, {"id": "b" * 16, "state": "whole"})
    assert len(st.list_files(db)) == 2
    assert [r["id"] for r in st.list_files(db, "offloaded")] == ["a" * 16]
    with pytest.raises(ValueError):
        st.list_files(db, "bogus")


def test_log_event(db):
    st.upsert(db, {"id": "a" * 16})
    st.log_event(db, "a" * 16, "k", {"x": 1})
    ev = st.events(db, "a" * 16)
    assert len(ev) == 1 and ev[0]["kind"] == "k" and ev[0]["detail"] == {"x": 1}


def test_update_if_applies_when_state_matches(db):
    st.upsert(db, {"id": "a" * 16, "state": "offloaded"})
    before = st.get(db, "a" * 16)["updated_ts"]
    assert st.update_if(db, "a" * 16, "offloaded", last_check_ts=5.0, last_stat_ok_ts=5.0)
    r = st.get(db, "a" * 16)
    assert r["last_check_ts"] == 5.0 and r["last_stat_ok_ts"] == 5.0 and r["state"] == "offloaded"
    assert r["updated_ts"] >= before
    assert st.update_if(db, "a" * 16, "offloaded", state="failed", restore_note="x")
    assert st.get(db, "a" * 16)["state"] == "failed"


def test_update_if_gone_or_moved_on_changes_nothing(db):
    assert st.update_if(db, "nope", "offloaded", last_check_ts=1.0) is False
    st.upsert(db, {"id": "a" * 16, "state": "offloaded"})
    # the runner promotes the row between the weekly read and write
    st.set_state(db, "a" * 16, "promoting", step=1)
    before = st.get(db, "a" * 16)
    assert st.update_if(db, "a" * 16, "offloaded", last_check_ts=9.0, state="failed") is False
    assert st.get(db, "a" * 16) == before


def test_update_if_validates(db):
    st.upsert(db, {"id": "a" * 16})
    with pytest.raises(ValueError):
        st.update_if(db, "a" * 16, "bogus", last_check_ts=1)
    with pytest.raises(ValueError):
        st.update_if(db, "a" * 16, "whole", state="bogus")
    with pytest.raises(ValueError):
        st.update_if(db, "a" * 16, "whole", nope=1)


def test_new_columns_present(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
    assert {"last_stat_ok_ts", "title", "year", "imdb_id", "tvdb_id", "season", "episode",
            "release_name"} <= cols


def test_old_db_gets_migrated(tmp_path):
    p = tmp_path / "old.db"
    st.open_db(p).close()
    import sqlite3
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE files DROP COLUMN last_stat_ok_ts")
    c.commit()
    c.close()
    d = st.open_db(p)
    assert "last_stat_ok_ts" in {r[1] for r in d.execute("PRAGMA table_info(files)")}
