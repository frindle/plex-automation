"""Weekly safety check for offloaded files, and its report.

There are no backups, so this is the safety net. For every offloaded row it:

- runs a 100% STAT of the row's NZB on both providers;
- re-scores the restore path;
- flags the row for promotion if a segment is missing or the restore count is 0.

A provider that cannot be asked (login refused, quota, link down) never flags a
row. The row is reported as "could not be checked" and the provider is not
asked again in that run, so a bad password cannot cause mass promotion or an
account lock.

Once a month (the first 7 days, or ``monthly=True``) it also reads a rotating
5% of the offloaded bytes in full through WebDAV and compares the SHA-256.
Rows are chosen by oldest ``last_ok_ts``, and ``last_ok_ts`` is set only by a
passing full read, so the rotation moves on every month.

``run_weekly`` returns the rows to promote. It does not promote anything.
"""
from __future__ import annotations

import collections
import os
import time
from datetime import datetime, timezone

from .newznab import NewznabError, nzb_segment_ids, read_archived
from .nntp_stat import NntpUnavailable
from .qualify import score_restore

_SOFT = ("auth", "quota")  # reported separately, never flagged


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


def _pick_for_full_read(rows: list[dict], frac: float) -> list[dict]:
    total = sum(int(r.get("size") or 0) for r in rows)
    budget = total * frac
    picked, used = [], 0
    for r in sorted(rows, key=lambda r: (r.get("last_ok_ts") or 0, str(r.get("id")))):
        if picked and used >= budget:
            break
        picked.append(r)
        used += int(r.get("size") or 0)
    return picked


def run_weekly(db, *, stat_primary, stat_secondary, indexers, report_path: str,
               state=None, reader=None, monthly: bool | None = None,
               sample_frac: float = 0.05, now: float | None = None) -> list[dict]:
    if state is None:
        from . import state as _state
        state = _state
    now = time.time() if now is None else now
    if monthly is None:
        monthly = datetime.fromtimestamp(now, timezone.utc).day <= 7

    rows = state.list_files(db, "offloaded")
    res: dict[str, dict] = {}
    dead: dict[str, str] = {}  # provider label -> kind, once it cannot be asked
    providers = (("primary", stat_primary), ("secondary", stat_secondary))

    for row in rows:
        r = {"row": row, "flags": [], "unchecked": [], "failed": [], "lost": False,
             "stat_done": 0}
        res[row["id"]] = r
        nzb = row.get("nzb_path")
        if not nzb or not os.path.exists(nzb):
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
                if out["missing"]:
                    r["flags"].append(f"missing_segments_{label}")

        # restore path
        upd: dict = {}
        try:
            own = None
            if nzb and os.path.exists(nzb):
                own = nzb_segment_ids(read_archived(nzb))
            sc = score_restore(row, indexers, own)
            upd["restore_score"], upd["restore_note"] = sc["count"], sc["note"]
            if sc["count"] == 0:
                r["flags"].append("no_restore_path")
                prev = row.get("restore_score")
                r["lost"] = prev is None or prev > 0
        except NewznabError:
            r["unchecked"].append("indexers_unreachable")
        except Exception as e:
            r["failed"].append(f"restore_{type(e).__name__}")
        r["upd"] = upd

    # monthly full-read compare
    full = {"files": 0, "bytes": 0, "mismatch": 0}
    if monthly and reader is not None and rows:
        for row in _pick_for_full_read(rows, sample_frac):
            r = res[row["id"]]
            try:
                got = reader.sha256_full(row["remote_path"])
            except Exception as e:
                r["failed"].append(f"full_read_{type(e).__name__}")
                continue
            full["files"] += 1
            full["bytes"] += int(row.get("size") or 0)
            if got != row.get("sha256"):
                r["flags"].append("hash_mismatch")
                full["mismatch"] += 1
            else:
                r["upd"]["last_ok_ts"] = now

    # store results, build the lists
    promote: list[dict] = []
    flagged, unchecked_rows, failed_rows = [], [], []
    ok = checked = 0
    reasons: collections.Counter = collections.Counter()
    for row in rows:
        r = res[row["id"]]
        flags = list(dict.fromkeys(r["flags"]))
        if flags:
            checked += 1
            flagged.append(r)
            promote.append({**row, "promote_reasons": flags})
            state.log_event(db, row["id"], "weekly_flag", {"reasons": flags})
            r["upd"].pop("last_ok_ts", None)
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
        state.set_state(db, row["id"], "offloaded", last_check_ts=now, **r["upd"])

    lost = sum(1 for r in res.values() if r["lost"])
    _write_report(report_path, now=now, rows=rows, checked=checked, ok=ok,
                  flagged=flagged, failed=failed_rows, unchecked=unchecked_rows,
                  reasons=reasons, dead=dead, promote=promote, lost=lost,
                  monthly=monthly, full=full, ran_full=bool(monthly and reader is not None))
    return promote


def _write_report(path, *, now, rows, checked, ok, flagged, failed, unchecked, reasons,
                  dead, promote, lost, monthly, full, ran_full):
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
    L.append("## Could not be checked")
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
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(L))
    os.replace(tmp, path)
