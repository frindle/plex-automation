"""Hot-set rules: which Plex files must be whole (fully local) right now.

Pure functions, no I/O. Everything is passed in as arguments.
See tiering/OFFLOAD_DESIGN.md, "Rules for what stays whole".

Inputs are plain dicts shaped like Plex's JSON. Extra keys used here:
  items[]: "section" (library section title), "videoResolution" and
           "bitrate" (kbps, optional).
  plays[]: "ratingKey", "viewedAt". In-progress items can be passed as
           plays with their lastViewedAt as viewedAt.
  sessions[], on_deck[]: "ratingKey" (activity is taken at `now`).
  synced_rks: a set of ratingKeys (activity at `now`) or a dict
              {ratingKey: ts} (activity at ts).
"""
from __future__ import annotations

from dataclasses import dataclass

DAY = 86400.0
FOREVER = float("inf")

# Tie-break when two rules give the same expiry: earlier wins.
_PRECEDENCE = ("section", "4k-line", "bitrate", "session", "on-deck", "synced",
               "played", "season", "next-season", "new")
_RANK = {r: i for i, r in enumerate(_PRECEDENCE)}


@dataclass(frozen=True)
class Cfg:
    new_days: int = 30
    idle_days: int = 60
    near_end_eps: int = 3
    keep_4k: bool = False
    max_stream_kbps: int = 0
    never_sections: tuple = ("Adult", "Asian TV")


@dataclass(frozen=True)
class Action:
    kind: str  # "promote" | "demote"
    rating_key: str
    reason: str
    priority: int  # 0 = now (live session)


def _rk(v):
    return None if v is None else str(v)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_4k(item) -> bool:
    return str(item.get("videoResolution") or "").strip().lower() in ("4k", "2160")


def desired_whole(items, plays, sessions, on_deck, synced_rks, now, cfg=Cfg()):
    by_rk = {}
    seasons = {}  # (show, season_no) -> [episode items]
    for it in items:
        rk = _rk(it.get("ratingKey"))
        if rk is None:
            continue
        by_rk[rk] = it
        if it.get("type") == "episode":
            show, sn = _rk(it.get("grandparentRatingKey")), _num(it.get("parentIndex"))
            if show is not None and sn is not None:
                seasons.setdefault((show, int(sn)), []).append(it)

    cands: dict[str, list] = {}

    def add(rk, reason, until):
        cands.setdefault(rk, []).append((until, reason))

    never = {s.strip().lower() for s in cfg.never_sections}
    idle = cfg.idle_days * DAY

    for rk, it in by_rk.items():
        sec = str(it.get("section") or it.get("librarySectionTitle") or "")
        if sec.strip().lower() in never:
            add(rk, "section", FOREVER)
        if cfg.keep_4k and _is_4k(it):
            add(rk, "4k-line", FOREVER)
        br = _num(it.get("bitrate"))
        if cfg.max_stream_kbps > 0 and br is not None and br > cfg.max_stream_kbps:
            add(rk, "bitrate", FOREVER)
        added = _num(it.get("addedAt"))
        if added is not None:
            add(rk, "new", added + cfg.new_days * DAY)

    # activity: (rk, ts, reason)
    activity = []
    for p in plays:
        ts = _num(p.get("viewedAt"))
        if ts is not None:
            activity.append((_rk(p.get("ratingKey")), ts, "played"))
    for s in sessions:
        activity.append((_rk(s.get("ratingKey")), now, "session"))
    for d in on_deck:
        activity.append((_rk(d.get("ratingKey")), now, "on-deck"))
    if isinstance(synced_rks, dict):
        for rk, ts in synced_rks.items():
            ts = _num(ts)
            activity.append((_rk(rk), now if ts is None else ts, "synced"))
    else:
        for rk in synced_rks:
            activity.append((_rk(rk), now, "synced"))

    for rk, ts, reason in activity:
        it = by_rk.get(rk)
        if it is None:
            continue
        until = ts + idle
        add(rk, reason, until)
        if it.get("type") != "episode":
            continue
        show, sn = _rk(it.get("grandparentRatingKey")), _num(it.get("parentIndex"))
        if show is None or sn is None:
            continue
        sn = int(sn)
        for ep in seasons.get((show, sn), ()):
            add(_rk(ep["ratingKey"]), "season", until)
        idx = _num(it.get("index"))
        if sn == 0 or idx is None:
            continue  # specials never pull in a next season
        top = max((i for i in (_num(e.get("index")) for e in seasons[(show, sn)])
                   if i is not None), default=None)
        if top is not None and idx > top - cfg.near_end_eps:
            for ep in seasons.get((show, sn + 1), ()):
                add(_rk(ep["ratingKey"]), "next-season", until)

    out = {}
    for rk, cs in cands.items():
        until, reason = max(cs, key=lambda c: (c[0], -_RANK[c[1]]))
        if until > now:
            out[rk] = (reason, until)
    return out


def plan_actions(desired, offloaded_rks, whole_rks, now):
    """Promote offloaded items that are desired; demote eligible whole items
    that are not. `whole_rks` holds only offload-eligible whole items."""
    acts = []
    for rk in {_rk(r) for r in offloaded_rks}:
        if rk in desired:
            reason = desired[rk][0]
            acts.append(Action("promote", rk, reason, 0 if reason == "session" else 1))
    for rk in {_rk(r) for r in whole_rks}:
        if rk not in desired:
            acts.append(Action("demote", rk, "idle", 5))
    acts.sort(key=lambda a: (a.priority, a.rating_key))
    return acts
