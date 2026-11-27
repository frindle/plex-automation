"""Automatic re-download of a file whose exact bytes can no longer be rebuilt.

A row is LOST when no copy of the file can be read back any more:

- (a) a promote ended ``state=failed`` because the BLAKE3 of a chunk mismatched on every
  copy (the upload and its twin);
- (b) the nightly check found an article of ``nzb_path`` truly missing on EVERY provider,
  with no live twin, and a second STAT of those ids confirmed it.

Then (only with ``TIERING_APPLY`` and ``TIERING_AUTO_REDOWNLOAD``) the arr is asked for a
new copy: the arr's file record for the dead symlink is deleted, the item is set monitored
and searched. The arr picks the release; nothing here grabs an NZB.

State machine (no new state: the row is ``failed``, the note says where it is):

    offloaded / promoting --(lost)--> failed "lost: re-download pending"
                                  --> failed "lost: re-download requested"   (arr asked)
                                  --> failed "lost: re-downloaded"           (arr has a file)

``register`` releases ``lib_path`` of such a row when a real file shows up there and treats
that file as a normal new ``whole`` row. Tracking of the requests lives in ``kv``
(``redownloads``); rows still waiting for a safe night in ``kv`` (``lost_pending``).

Guards (nothing is deleted, searched or marked if one fails):
- never on a night when a provider could not be asked, or AltMount did not answer;
- at most ``TIERING_REDOWNLOAD_MAX_PER_NIGHT`` rows a night; more lost rows than that is
  ``mass_loss``: act on none, alert, log an event (a wiped account looks like this);
- right before the delete, ``L`` must still be the symlink into the tierfs mount for this
  row's id. Otherwise nothing is done. A real media file is never deleted.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .register import ArrError, clean_path, map_path, to_share

log = logging.getLogger("tiering.redownload")

DAY = 86400.0
SEARCH_EVERY = 3 * DAY  # search again after this long with no new file
SEARCH_MAX = 3  # searches in all; the next quiet period raises redownload_stalled
LOST_PREFIX = "lost:"
NOTE_PENDING = "lost: re-download pending"
NOTE_REQUESTED = "lost: re-download requested"
NOTE_DONE = "lost: re-downloaded"


def is_lost_note(note) -> bool:
    return str(note or "").startswith(LOST_PREFIX)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


class Arrs:
    """Radarr and Sonarr (several instances of each) for one library path."""

    def __init__(self, radarrs, sonarrs, arr_map=None, disk_map=None):
        self.radarrs = list(radarrs or [])
        self.sonarrs = list(sonarrs or [])
        self.arr_map = list(arr_map or [])
        self.disk_map = list(disk_map or [])

    def _local(self, arr_path) -> str:
        """What register stores for an arr path: the share path."""
        p = map_path(clean_path(str(arr_path)), self.arr_map)
        return clean_path(to_share(p, self.disk_map))

    def find(self, lib_path: str):
        """The arr item that holds this file, or None. Raises ArrError."""
        lib = clean_path(lib_path)
        for i, c in enumerate(self.radarrs):
            for m in c.get("/api/v3/movie") or []:
                f = m.get("movieFile") if isinstance(m, dict) else None
                if f and f.get("path") and self._local(f["path"]) == lib:
                    return {"kind": "radarr", "client": i, "file_id": f.get("id"),
                            "ids": [m["id"]], "title": m.get("title"),
                            "unmonitored": [] if m.get("monitored") else [m["id"]]}
        for i, c in enumerate(self.sonarrs):
            for s in c.get("/api/v3/series") or []:
                sp = s.get("path")
                if sp and not _under(lib, self._local(sp)):
                    continue  # another series' folder: its episodes are not asked for
                eps = c.get("/api/v3/episode", seriesId=s.get("id"), includeEpisodeFile="true")
                hit = [e for e in eps or []
                       if isinstance(e.get("episodeFile"), dict)
                       and e["episodeFile"].get("path")
                       and self._local(e["episodeFile"]["path"]) == lib]
                if hit:
                    return {"kind": "sonarr", "client": i,
                            "file_id": hit[0]["episodeFile"].get("id"),
                            "ids": [e["id"] for e in hit], "title": s.get("title"),
                            "unmonitored": [e["id"] for e in hit if not e.get("monitored")]}
        return None

    def _client(self, item):
        pool = self.radarrs if item["kind"] == "radarr" else self.sonarrs
        try:
            return pool[item["client"]]
        except IndexError:
            raise ArrError("arr instance gone") from None

    def delete_file(self, item) -> None:
        """Delete the arr's file record (the arr removes the dead symlink with it)."""
        if item.get("file_id") is None:
            raise ArrError("no file id")
        path = "/api/v3/moviefile/" if item["kind"] == "radarr" else "/api/v3/episodefile/"
        self._client(item).delete(f"{path}{int(item['file_id'])}")

    def ensure_monitored(self, item) -> list:
        """Set the items (only these) monitored; returns the ids that were changed."""
        c = self._client(item)
        base = "/api/v3/movie/" if item["kind"] == "radarr" else "/api/v3/episode/"
        changed = []
        for iid in item.get("ids", []):
            cur = c.get(f"{base}{int(iid)}")
            if isinstance(cur, dict) and not cur.get("monitored"):
                cur["monitored"] = True
                c.put(f"{base}{int(iid)}", cur)
                changed.append(iid)
        return changed

    def search(self, item) -> None:
        c = self._client(item)
        ids = [int(i) for i in item["ids"]]
        if item["kind"] == "radarr":
            c.post("/api/v3/command", {"name": "MoviesSearch", "movieIds": ids})
        else:
            c.post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": ids})

    def has_file(self, item) -> bool:
        c = self._client(item)
        base = "/api/v3/movie/" if item["kind"] == "radarr" else "/api/v3/episode/"
        ids = item.get("ids") or []
        return bool(ids) and all(bool((c.get(f"{base}{int(i)}") or {}).get("hasFile"))
                                 for i in ids)


class Redownload:
    """The nightly orchestrator. All callables are the runner's; nothing here touches disk.

    ``l_ok(row)``: L is the symlink into the tierfs mount for this row's id (strict).
    ``busy(id)``: a promote or demote of the row is running. ``healthy()``: AltMount answers.
    ``alert(kind, id, **detail)``: the runner's alert.
    """

    def __init__(self, *, state, arrs, l_ok, busy, healthy, alert, kv_get, kv_set,
                 enabled=True, max_per_night=3, clock=time.time):
        self.state, self.arrs = state, arrs
        self.l_ok, self.busy, self.healthy, self.alert = l_ok, busy, healthy, alert
        self.kv_get, self.kv_set = kv_get, kv_set
        self.enabled, self.max_per_night, self.clock = enabled, max(1, int(max_per_night)), clock
        # the weekly thread (process) and the runner's main loop (retry_pending, mark_lost) both reach
        # run_pending; one at a time, so a row is acted on once
        self._lock = threading.RLock()

    # ---- kv helpers

    def pending(self, db) -> dict:
        return dict(self.kv_get(db, "lost_pending", {}) or {})

    def _save_pending(self, db, p):
        self.kv_set(db, "lost_pending", p)

    def _tracked(self, db) -> dict:
        return dict(self.kv_get(db, "redownloads", {}) or {})

    def _once(self, db, kind, id, **detail):
        """Alert once per (kind, id), ever (kept in kv)."""
        seen = set(self.kv_get(db, "redownload_alerted", []) or [])
        key = f"{kind}:{id}"
        if key in seen:
            return
        seen.add(key)
        self.kv_set(db, "redownload_alerted", sorted(seen))
        self.alert(kind, id, **detail)

    @staticmethod
    def _day(now) -> str:
        return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")

    # ---- entry points

    def mark_lost(self, db, id, kind, why):
        with self._lock:
            return self._mark_lost(db, id, kind, why)

    def _mark_lost(self, db, id, kind, why):
        """Note that a row is lost; it is acted on at the next night that passes the guards.
        Returns True when the row was newly marked."""
        row = self.state.get(db, id)
        if row is None or row["state"] not in ("offloaded", "promoting", "failed"):
            return False
        p = self.pending(db)
        if id in p:
            return False
        p[id] = {"kind": kind, "since": self.clock()}
        self._save_pending(db, p)
        self.state.log_event(db, id, "lost_exact_file", {"how": kind, "why": why})
        if row["state"] == "failed" and not is_lost_note(row.get("restore_note")):
            self.state.update_if(db, id, "failed",
                                 restore_note=f"{NOTE_PENDING}; {row.get('restore_note') or why}")
        return True

    def process(self, db, lost, info, now=None):
        with self._lock:
            return self._process(db, lost, info, now)

    def _process(self, db, lost, info, now=None):
        """Called once by the nightly check: ``lost`` are the rows it just confirmed (b),
        ``info`` says whether a provider could not be asked. Does the tracking, then, when
        every guard passes, the acting."""
        now = self.clock() if now is None else now
        p = self.pending(db)
        for l in lost:
            row = self.state.get(db, l["id"])
            if row is not None and l["id"] not in p:
                p[l["id"]] = {"kind": "b", "since": now}
        if p != self.pending(db):
            self._save_pending(db, p)
        if not self.enabled:
            for id in p:
                self._once(db, "lost_file", id, reason="auto re-download is off")
            return
        try:
            self.track(db, now)
        except Exception as e:  # noqa: BLE001
            log.error("redownload tracking failed: %s", type(e).__name__)
        why = self._guard(info)
        if why:
            if p:
                log.warning("redownload: %d lost row(s) wait: %s", len(p), why)
            return
        self.kv_set(db, "redownload_guard_day", self._day(now))
        self.run_pending(db, now)

    def _guard(self, info) -> str:
        if info.get("provider_trouble"):
            return "a provider could not be asked tonight"
        try:
            if not self.healthy():
                return "AltMount did not answer"
        except Exception:  # noqa: BLE001
            return "AltMount did not answer"
        if self.arrs is None:
            return "no arr is configured"
        return ""

    def retry_pending(self, db, now=None):
        with self._lock:
            return self._retry_pending(db, now)

    def _retry_pending(self, db, now=None):
        """Each runner pass: rows deferred earlier (a promote was running) are acted on once
        it stops, but only on a night that already passed the guards."""
        now = self.clock() if now is None else now
        if not self.enabled or self.kv_get(db, "redownload_guard_day") != self._day(now):
            return
        if self.pending(db):
            self.run_pending(db, now)

    def reset(self, db):
        """Forget the pending list and tonight's mass-loss mark (the --reset-redownload flag). Rows are not
        touched: a row still lost is marked again by the next nightly check. Returns how many were pending."""
        with self._lock:
            n = len(self.pending(db))
            self._save_pending(db, {})
            self.kv_set(db, "redownload_night", {})
            return n

    # ---- acting

    def _valid(self, row) -> bool:
        if row is None:
            return False
        if row["state"] in ("offloaded", "promoting"):
            return True
        return row["state"] == "failed" and is_lost_note(row.get("restore_note"))

    def _save_unacted(self, db, acted):
        """Save the stored list minus the rows this pass already acted on or dropped. Built from what is stored
        now, not from the list read at the start: a row marked meanwhile stays, an acted one never comes back."""
        self._save_pending(db, {k: v for k, v in self.pending(db).items() if k not in acted})

    def run_pending(self, db, now):
        with self._lock:
            return self._run_pending(db, now)

    def _run_pending(self, db, now):
        p = self.pending(db)
        acted = set()
        for id in list(p):
            if not self._valid(self.state.get(db, id)):
                p.pop(id)
                acted.add(id)
        self._save_unacted(db, acted)
        if not p:
            return
        day = self._day(now)
        night = self.kv_get(db, "redownload_night", {}) or {}
        if night.get("day") != day:
            night = {"day": day, "n": 0}
        if len(p) > self.max_per_night:
            if night.get("mass") != day:
                night["mass"] = day
                self.kv_set(db, "redownload_night", night)
                self.alert("mass_loss", "-", lost=len(p), limit=self.max_per_night)
                for id in sorted(p):
                    self.state.log_event(db, id, "mass_loss", {"lost": len(p)})
            log.error("redownload: %d rows lost (limit %d a night); acting on none. To act on them, set "
                      "TIERING_REDOWNLOAD_MAX_PER_NIGHT to at least %d and restart; to forget the list "
                      "(the next nightly check marks again what is still lost) run the runner once with "
                      "--reset-redownload", len(p), self.max_per_night, len(p))
            return
        for id in sorted(p):
            if night["n"] >= self.max_per_night:
                break
            res = self._act(db, id, now)
            if res == "requested":
                night["n"] += 1
            if res in ("requested", "dropped"):
                p.pop(id, None)
                acted.add(id)
            self.kv_set(db, "redownload_night", night)
            self._save_unacted(db, acted)

    def _act(self, db, id, now) -> str:
        """requested, dropped (nothing to do for this row any more) or wait."""
        row = self.state.get(db, id)
        if not self._valid(row):
            return "dropped"
        if self.busy(id):
            return "wait"
        if not self.l_ok(row):
            self.state.log_event(db, id, "redownload_skipped",
                                 {"reason": "L is not the tierfs symlink for this row"})
            log.warning("redownload %s: L is not the tierfs symlink for this row; nothing done",
                        id)
            return "dropped"
        try:
            item = self.arrs.find(row.get("lib_path") or "")
        except ArrError as e:
            return self._arr_failed(db, id, f"arr lookup: {e}")
        if item is None:
            self.state.log_event(db, id, "redownload_skipped", {"reason": "no arr item has it"})
            self._once(db, "redownload_failed", id, reason="no arr item holds this file")
            return "wait"
        # right before the delete: L again, and the row (the symlink may have changed
        # while the arr was asked)
        row = self.state.get(db, id)
        if not self._valid(row) or not self.l_ok(row):
            self.state.log_event(db, id, "redownload_skipped",
                                 {"reason": "L changed during the lookup"})
            return "dropped"
        if row["state"] != "failed":
            if not self.state.update_if(db, id, row["state"], state="failed",
                                        restore_note=NOTE_PENDING):
                return "wait"
        try:
            self.arrs.delete_file(item)
        except ArrError as e:
            return self._arr_failed(db, id, f"arr delete: {e}")
        ent = {**item, "searches": 0, "last_search": None, "requested_ts": now,
               "stalled": False}
        tracked = self._tracked(db)
        tracked[id] = ent
        self.kv_set(db, "redownloads", tracked)
        self.state.log_event(db, id, "redownload_requested",
                             {"title": item.get("title"), "kind": item["kind"]})
        self.state.update_if(db, id, "failed", restore_note=NOTE_REQUESTED)
        self._search_step(db, id, ent, now)
        tracked[id] = ent
        self.kv_set(db, "redownloads", tracked)
        self.alert("redownload_requested", id, title=item.get("title"), arr=item["kind"])
        return "requested"

    def _arr_failed(self, db, id, reason) -> str:
        log.error("redownload %s: %s", id, reason)
        self.state.log_event(db, id, "redownload_error", {"reason": reason})
        self._once(db, "redownload_failed", id, reason=reason)
        return "wait"

    def _search_step(self, db, id, ent, now) -> bool:
        """Monitor the item if it is not, then search. Updates ent. True when searched."""
        try:
            changed = self.arrs.ensure_monitored(ent)
            if changed:
                self.state.log_event(db, id, "redownload_monitored", {"ids": len(changed)})
            self.arrs.search(ent)
        except ArrError as e:
            self.state.log_event(db, id, "redownload_error", {"reason": f"search: {e}"})
            log.error("redownload %s: search failed: %s", id, e)
            return False
        ent["searches"] = int(ent.get("searches") or 0) + 1
        ent["last_search"] = now
        return True

    def track(self, db, now):
        """Every night: has the arr a file again? Search again after 3 days without one;
        after 3 searches raise redownload_stalled (once)."""
        tracked = self._tracked(db)
        for id, ent in list(tracked.items()):
            try:
                has = self.arrs.has_file(ent) if self.arrs is not None else False
            except ArrError:
                continue
            if has:
                self.state.log_event(db, id, "redownload_done", {})
                row = self.state.get(db, id)
                if row and row["state"] == "failed" and is_lost_note(row.get("restore_note")):
                    self.state.update_if(db, id, "failed", restore_note=NOTE_DONE)
                self.alert("redownload_done", id, title=ent.get("title"))
                tracked.pop(id)
                continue
            last = ent.get("last_search")
            if last is None or now - float(last) >= SEARCH_EVERY:
                if int(ent.get("searches") or 0) >= SEARCH_MAX:
                    if not ent.get("stalled"):
                        ent["stalled"] = True
                        self.state.log_event(db, id, "redownload_stalled",
                                             {"searches": ent["searches"]})
                        self.alert("redownload_stalled", id, title=ent.get("title"),
                                   searches=ent["searches"])
                elif self.arrs is not None:
                    self._search_step(db, id, ent, now)
        self.kv_set(db, "redownloads", tracked)
