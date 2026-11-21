"""Weekly safety check for offloaded files, and its report.

There are no backups, so this is the safety net. For every offloaded row it:

- runs a 100% STAT of the row's NZB on both providers;
- re-checks the restore path;
- flags the row for promotion at once if a segment is missing or there is no
  restore path.

Writes. Each result is written to the row as soon as it is known, through
``state.update_if(db, id, "offloaded", ...)`` and nothing else. If the runner
moved the row on (or it was deleted) the write returns False, the row is left
alone and the loop goes on. ``set_state`` is never called, and no row read at the
start is ever written back whole.

Unknown is not bad. A provider that cannot be asked (login refused, quota, link
down) never flags a row; it is "could not be checked", and the provider is not
asked again in that run, so a bad password cannot cause mass promotion or an
account lock. An indexer that fails or answers only in part makes the restore
check "unchecked", never "no restore path", and unchecked never promotes.
``last_check_ts`` is set on every checked row; ``last_stat_ok_ts`` only when both
providers found every segment. An alert is raised for a row whose
``last_stat_ok_ts`` is older than 14 days.

Restore-path cost. Searches and NZB grabs are the indexers' quota (the client keeps
rolling 24-hour budgets, so the caller resets nothing). A row keeps the NZB of its
verified restore candidate in ``nzb_path2`` (qualify fills it). While that NZB is a
different upload from ``nzb_path`` (checked locally, by message-id) and its segments
are all present on both providers the restore path is OK and nothing is asked of an
indexer. When ``nzb_path2`` is missing, is the same upload as ``nzb_path``, or has lost
articles, the check searches and verifies candidates by grab and message-id (a
candidate that shares message-ids with ``nzb_path``, or with a dead ``nzb_path2``, is
never counted, whatever its name), STATs the candidate on every provider before it
counts, and stores it in ``nzb_path2``. If nothing new and live is found the loss is
flagged (an alert and a promote), ``nzb_path2`` is cleared, and the row is never
reported OK. A spent budget, a dropped indexer, a failed grab or a provider that cannot
be asked makes the row "unchecked", never "no restore path".

Monthly (the first 7 days, or ``monthly=True``) it reads a rotating 5% of the
offloaded bytes in full through WebDAV and compares the SHA-256. Rows are picked
by the oldest *attempt*, not the oldest success, so a row that always fails
cannot hold the rotation: the attempt is recorded before the read, in a small
JSON file next to the report (no column). A hash mismatch is like ``bad_chunk``:
the row goes to ``state=failed`` and an alert is raised (promoting cannot help,
the remote copy is bad). Three read errors in a row, or three weekly STAT
failures in a row, on one row raise an alert.

``run_weekly`` returns the rows flagged for promotion. It promotes nothing; the
optional ``on_promote(entry)`` is called per row the moment it is flagged.
"""
from __future__ import annotations

import collections
import json
import os
import time
import uuid
from datetime import datetime, timezone

from .newznab import NewznabError, archive_nzb, nzb_segment_ids, read_archived
from .nntp_stat import NntpUnavailable
from .qualify import _SOFT, score_restore, stat_nzb_bytes, stat_status

STALE_DAYS = 14
STREAK = 3


def _title(row: dict) -> str:
    return (row.get("title") or os.path.basename(str(row.get("lib_path") or ""))
            or str(row.get("id")))


def _human(n: int) -> str:
    x = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or u == "TiB":
            return f"{x:.1f} {u}" if u != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


def _load_attempts(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_attempts(path: str, d: dict) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _pick_for_full_read(rows: list[dict], frac: float, attempts: dict) -> list[dict]:
    """Rows for the monthly read: oldest attempt first (never tried comes first)."""
    total = sum(int(r.get("size") or 0) for r in rows)
    budget = total * frac
    picked, used = [], 0

    def key(r):
        a = attempts.get(str(r.get("id")), {})
        return (a.get("read_ts") or 0, r.get("last_ok_ts") or 0, str(r.get("id")))

    for r in sorted(rows, key=key):
        if picked and used >= budget:
            break
        picked.append(r)
        used += int(r.get("size") or 0)
    return picked


def run_weekly(db, *, stat_primary, stat_secondary, indexers, report_path: str,
               state=None, reader=None, monthly: bool | None = None,
               sample_frac: float = 0.05, now: float | None = None,
               alert=None, on_promote=None, attempts_path: str | None = None) -> list[dict]:
    if state is None:
        from . import state as _state
        state = _state
    now = time.time() if now is None else now
    if monthly is None:
        monthly = datetime.fromtimestamp(now, timezone.utc).day <= 7
    attempts_path = attempts_path or f"{report_path}.attempts.json"
    attempts = _load_attempts(attempts_path)

    rows = state.list_files(db, "offloaded")
    res: dict[str, dict] = {}
    dead: dict[str, str] = {}  # provider label -> kind, once it cannot be asked
    providers = (("primary", stat_primary), ("secondary", stat_secondary))
    promote: list[dict] = []
    alerts: list[str] = []
    full = {"files": 0, "bytes": 0, "mismatch": 0}
    ran_full = bool(monthly and reader is not None and rows)
    picked = ({r["id"] for r in _pick_for_full_read(rows, sample_frac, attempts)}
              if ran_full else set())

    def raise_alert(row: dict, msg: str) -> None:
        text = f"{_title(row)}: {msg}"
        alerts.append(text)
        state.log_event(db, row["id"], "alert", {"msg": msg})
        if alert is not None:
            alert(text)

    def write(r: dict, **fields) -> bool:
        ok = state.update_if(db, r["row"]["id"], "offloaded", **fields)
        if not ok:
            r["moved"] = True
        return ok

    def flag(r: dict, reason: str, detail: str = "", **extra) -> bool:
        """Mark the row for promotion now: persist the flag, log it, tell the caller."""
        if reason not in r["flags"]:
            r["flags"].append(reason)
        note = "promote flagged: " + ", ".join(r["flags"]) + (f"; {detail}" if detail else "")
        fields = {"last_check_ts": now, "restore_note": note, **extra}
        if not write(r, **fields):
            return False
        row = r["row"]
        state.log_event(db, row["id"], "weekly_flag", {"reasons": list(r["flags"])})
        if r["entry"] is None:
            r["entry"] = {**row, "promote_reasons": r["flags"]}
            promote.append(r["entry"])
            if on_promote is not None:
                on_promote(r["entry"])
        return True

    def stat_restore(path: str) -> str:
        """STAT the stored restore NZB on both providers: ok, missing or unchecked."""
        return stat_status(path, providers, dead)

    for row in rows:
        rid = row["id"]
        att = attempts.setdefault(rid, {})
        r = {"row": row, "flags": [], "unchecked": [], "failed": [], "lost": False,
             "stat_done": 0, "moved": False, "entry": None, "hash_failed": False}
        res[rid] = r
        nzb = row.get("nzb_path")
        has_nzb = bool(nzb and os.path.exists(nzb))
        if not has_nzb:
            r["unchecked"].append("no_nzb")
        else:
            for label, fn in providers:
                if label in dead:
                    r["unchecked"].append(f"{label}_{dead[label]}")
                    continue
                try:
                    out = fn(nzb)
                except NntpUnavailable as e:
                    if e.kind in _SOFT:
                        dead[label] = e.kind
                        r["unchecked"].append(f"{label}_{e.kind}")
                    else:
                        r["failed"].append(f"{label}_{e.kind}")
                    continue
                except Exception as e:  # a bad NZB file must not stop the run
                    r["failed"].append(f"{label}_{type(e).__name__}")
                    continue
                if out["total"] == 0:
                    r["failed"].append(f"{label}_empty_nzb")
                    continue
                r["stat_done"] += 1
                if out["missing"] and not flag(r, f"missing_segments_{label}"):
                    break  # the runner moved this row on
        if r["moved"]:
            continue

        # the STAT result, written as soon as both providers are done
        upd: dict = {"last_check_ts": now}
        all_found = (r["stat_done"] == len(providers)
                     and not any(f.startswith("missing_segments") for f in r["flags"]))
        if all_found:
            upd["last_stat_ok_ts"] = now
            att["stat_net"] = 0
        elif r["failed"]:
            att["stat_net"] = int(att.get("stat_net") or 0) + 1
            if att["stat_net"] >= STREAK:
                raise_alert(row, f"{att['stat_net']} STAT checks in a row failed")
        if not write(r, **upd):
            continue
        last_ok = row.get("last_stat_ok_ts")
        if not all_found and (last_ok is None or now - float(last_ok) > STALE_DAYS * 86400):
            raise_alert(row, f"no complete STAT check in over {STALE_DAYS} days")

        # restore path
        if has_nzb:
            try:
                prev = row.get("restore_score")
                extra: dict = {}
                n2 = row.get("nzb_path2")
                own = nzb_segment_ids(read_archived(nzb))
                exclude = own
                status = "search"
                n2_dead = False
                if n2 and os.path.exists(n2):
                    try:
                        ids2 = nzb_segment_ids(read_archived(n2))
                    except Exception:
                        ids2 = frozenset()
                    if not ids2 or n2 == nzb or not ids2.isdisjoint(own):
                        n2_dead = True  # the same upload as nzb_path (or empty): no restore path
                    else:
                        status = stat_restore(n2)  # the stored candidate: STAT, no grab
                        if status == "missing":
                            n2_dead = True
                            exclude = own | ids2  # never accept the upload that just died
                elif n2:
                    n2_dead = True  # the archived file is gone
                if status == "ok":
                    sc = {"count": max(1, int(prev or 0)), "unsure": False, "partial": False,
                          "note": "restore path OK: stored alternative, all segments present"}
                elif status == "unchecked":
                    sc = {"count": 0, "unsure": True, "partial": False,
                          "note": "restore path unchecked (a provider could not be asked)"}
                else:
                    # no usable stored candidate: search, verify by grab, message-id and STAT
                    def verify(raw: bytes):
                        v = stat_nzb_bytes(raw, providers, os.path.dirname(nzb), dead)
                        return True if v == "ok" else (False if v == "missing" else None)

                    sc = score_restore(row, indexers, exclude, stop_at=1, verify=verify)
                    cand = sc.get("candidate")
                    if cand:
                        extra["nzb_path2"] = archive_nzb(cand["nzb"], os.path.dirname(nzb),
                                                         cand["title"])
                note = sc["note"]
                if r["flags"]:
                    note = "promote flagged: " + ", ".join(r["flags"]) + "; " + note
                if sc["count"] == 0 and sc["unsure"]:
                    if not r["failed"]:  # a row that failed its STAT already says why
                        r["unchecked"].append("indexers_partial" if sc["partial"]
                                              else "restore_unsure")
                elif sc["count"] == 0:
                    r["lost"] = prev is None or prev > 0
                    if n2_dead:
                        raise_alert(row, "restore NZB lost and no live replacement found")
                        state.log_event(db, rid, "restore_nzb_lost", {})
                        extra["nzb_path2"] = None
                    flag(r, "no_restore_path", sc["note"], restore_score=0, **extra)
                else:
                    write(r, restore_score=sc["count"], restore_note=note, **extra)
            except NewznabError:
                r["unchecked"].append("indexers_unreachable")
            except Exception as e:
                r["failed"].append(f"restore_{type(e).__name__}")
            if r["moved"]:
                continue

        # monthly full read
        if rid in picked:
            att["read_ts"] = now  # recorded before the read: a failure still moves the rotation on
            _save_attempts(attempts_path, attempts)
            try:
                got = reader.sha256_full(row["remote_path"])
            except Exception as e:
                r["failed"].append(f"full_read_{type(e).__name__}")
                att["read_net"] = int(att.get("read_net") or 0) + 1
                state.log_event(db, rid, "full_read_error", {"kind": type(e).__name__})
                if att["read_net"] >= STREAK:
                    raise_alert(row, f"{att['read_net']} full reads in a row failed")
            else:
                att["read_net"] = 0
                full["files"] += 1
                full["bytes"] += int(row.get("size") or 0)
                if not row.get("sha256"):
                    r["unchecked"].append("no_sha256")
                elif got == row["sha256"]:
                    write(r, last_ok_ts=now)
                else:
                    full["mismatch"] += 1
                    r["hash_failed"] = True
                    if write(r, state="failed",
                             restore_note="full read hash mismatch; remote copy is bad. "
                                          + (row.get("restore_note") or "")):
                        state.log_event(db, rid, "full_read_mismatch", {})
                        raise_alert(row, "full read hash mismatch, marked failed")
            if r["moved"]:
                continue

    live = {r["id"] for r in rows}
    _save_attempts(attempts_path, {k: v for k, v in attempts.items() if v and k in live})

    # classify for the report
    flagged, unchecked_rows, failed_rows = [], [], []
    ok = checked = moved = hash_failed = 0
    reasons: collections.Counter = collections.Counter()
    for row in rows:
        r = res[row["id"]]
        if r["moved"]:
            moved += 1
            continue
        if r["hash_failed"]:
            hash_failed += 1
            checked += 1
        elif r["flags"]:
            checked += 1
            flagged.append(r)
        elif r["unchecked"]:
            unchecked_rows.append(r)
            for x in set(r["unchecked"]):
                reasons[x] += 1
        elif r["failed"]:
            failed_rows.append(r)
            for x in set(r["failed"]):
                reasons[x] += 1
        else:
            checked += 1
            ok += 1

    lost = sum(1 for r in res.values() if r["lost"])
    failed_state = state.list_files(db, "failed")
    _write_report(report_path, now=now, rows=rows, checked=checked, ok=ok,
                  flagged=flagged, failed=failed_rows, unchecked=unchecked_rows,
                  reasons=reasons, dead=dead, promote=promote, lost=lost,
                  monthly=monthly, full=full, ran_full=ran_full, moved=moved,
                  hash_failed=hash_failed, alerts=alerts, failed_state=failed_state)
    return promote


def _write_report(path, *, now, rows, checked, ok, flagged, failed, unchecked, reasons,
                  dead, promote, lost, monthly, full, ran_full, moved, hash_failed,
                  alerts, failed_state):
    total_bytes = sum(int(r.get("size") or 0) for r in rows)
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    L = [f"# Offload weekly check, {day}", ""]
    L += [f"Files offloaded: {len(rows)}.",
          f"Bytes offloaded: {_human(total_bytes)}.",
          f"Files checked: {checked}.",
          f"Files OK: {ok}.",
          f"Files flagged: {len(flagged)}.",
          f"Files that failed to check: {len(failed)}.",
          f"Files that could not be checked: {len(unchecked)}.",
          f"Files marked failed this run: {hash_failed}.",
          f"Files changed by the runner during the run: {moved}.",
          f"Promotions requested: {len(promote)}.",
          f"Restore paths lost: {lost}.", ""]
    if ran_full:
        L += [f"Full read check: {full['files']} files, {_human(full['bytes'])}, "
              f"{full['mismatch']} mismatches.", ""]
    elif monthly:
        L += ["Full read check was due but no reader was given.", ""]
    else:
        L += ["No full read check this week.", ""]
    for label, kind in sorted(dead.items()):
        why = {"auth": "refused the login", "quota": "reported a quota or service limit"}[kind]
        L.append(f"The {label} provider {why}. Its checks were skipped and nothing was flagged for it.")
    if dead:
        L.append("")
    L.append("## Alerts")
    L += [f"- {a}" for a in alerts] or ["None."]
    L += ["", "## Failed files (state failed)"]
    if failed_state:
        for fr in sorted(failed_state, key=lambda x: _title(x).lower()):
            L.append(f"- {_title(fr)}: {fr.get('restore_note') or 'no note'}; "
                     f"restore path: {fr.get('nzb_path2') or 'none archived'}")
    else:
        L.append("None.")
    L += ["", "## Could not be checked"]
    if reasons:
        for k, n in sorted(reasons.items()):
            L.append(f"- {k}: {n} files")
    else:
        L.append("None.")
    L += ["", "## Flagged files (first 20 by title)"]
    if flagged:
        for r in sorted(flagged, key=lambda r: _title(r["row"]).lower())[:20]:
            L.append(f"- {_title(r['row'])}: {', '.join(dict.fromkeys(r['flags']))}")
        if len(flagged) > 20:
            L.append(f"- and {len(flagged) - 20} more.")
    else:
        L.append("None.")
    L.append("")
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(L))
    os.replace(tmp, path)
