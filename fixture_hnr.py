#!/usr/bin/env python3
"""Adversarial behavioral test: seed-obligation is authoritative on EVERY
data-delete path, not just should_hard_delete_on_upgrade.

Property under test
-------------------
A torrent's DATA must never be removed while it still owes the tracker seed
time (seeding_time < SEED_DAYS*86400), REGARDLESS of whether the tracker has
unregistered it. The Diplomat lesson: a private tracker unregisters a
superseded torrent the moment the repack posts while STILL enforcing its
minimum seed time -- so "unregistered" is NOT a licence to delete data.

Two backstops still had the pre-Diplomat OR-logic and are fixed here:
  1. remove_torrent()'s internal HnR guard -- the single choke point for
     every data delete (only core.remove_torrent call in the codebase).
  2. queued_superseded_targets() -- the purge selector shared by the
     post-import sweep and /purge-unstarted-superseded.
Plus test_maintenance.py case 'e', which asserted the buggy behaviour.

The fixture drives the REAL functions with a stubbed session, capturing the
exact remove_data flag sent to Deluge's core.remove_torrent.
"""
import sys

aw = __import__("arr-webhook")

THR = aw.SEED_DAYS * 86400          # seed obligation, seconds
SMALL = 60                          # 1 min in -> obligation NOT met
BIG = THR + 1                       # obligation met
MID = 100000                        # under THR, but ABOVE the (SEED_DAYS+86400)
                                    # value a '*'->'+' mutant would compute -> kills it
UNREG = "Error: Unregistered torrent"
REG = "Announce OK"

fails = 0
def check(desc, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {'ok ' if ok else 'FAIL'}: {desc}: got {got!r} want {want!r}")


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


class _Session:
    """Stands in for the module-global requests.Session. Answers
    core.get_torrent_status with a configured info dict and records the
    remove_data flag passed to core.remove_torrent."""
    def __init__(self, info):
        self.info = info
        self.captured = None            # (hash, remove_data)
    def post(self, url, json=None, timeout=None, **kw):
        method = (json or {}).get("method")
        if method == "core.get_torrent_status":
            return _Resp({"result": dict(self.info)})
        if method == "core.remove_torrent":
            h, remove_data = json["params"]
            self.captured = (h, remove_data)
            return _Resp({"result": True})
        raise AssertionError(f"unexpected deluge method {method!r}")


def remove_data_flag(info):
    """Call the real remove_torrent(info=None) so it exercises the status
    FETCH + guard, and return the remove_data flag it actually sent."""
    fake = _Session(info)
    orig = aw.session
    aw.session = fake
    try:
        aw.remove_torrent("HASH", remove_data=True, info=None)
    finally:
        aw.session = orig
    assert fake.captured is not None, "core.remove_torrent was never called"
    return fake.captured[1]


# ── 1. remove_torrent HnR backstop ───────────────────────────────────────────
print("remove_torrent guard:")
# THE DIPLOMAT BACKSTOP: unregistered but seed obligation NOT met -> KEEP files.
check("unregistered + seed<obligation -> keep data (Diplomat backstop)",
      remove_data_flag({"tracker_status": UNREG, "seeding_time": SMALL, "progress": 100}), False)
# still registered + under obligation -> keep (already worked pre-fix)
check("registered + seed<obligation -> keep data",
      remove_data_flag({"tracker_status": REG, "seeding_time": SMALL, "progress": 100}), False)
# obligation met -> deleting data is not a hit-and-run, regardless of registration
check("unregistered + seed>=obligation -> delete data ok",
      remove_data_flag({"tracker_status": UNREG, "seeding_time": BIG, "progress": 100}), True)
check("registered + seed>=obligation -> delete data ok",
      remove_data_flag({"tracker_status": REG, "seeding_time": BIG, "progress": 100}), True)
# missing seeding_time -> treat as 0 -> not met -> keep
check("missing seeding_time -> keep data",
      remove_data_flag({"tracker_status": UNREG, "progress": 100}), False)
# boundary: exactly at THR -> obligation met -> delete (kills '<'->'<=')
check("seed==obligation -> delete data ok",
      remove_data_flag({"tracker_status": UNREG, "seeding_time": THR, "progress": 100}), True)
check("seed==obligation-1 -> keep data",
      remove_data_flag({"tracker_status": UNREG, "seeding_time": THR - 1, "progress": 100}), False)
# midpoint: under THR but above SEED_DAYS+86400 -> still keep (kills '*'->'+')
check("seed=MID (under real THR) -> keep data",
      remove_data_flag({"tracker_status": UNREG, "seeding_time": MID, "progress": 100}), False)

# ── 2. queued_superseded_targets purge selector ──────────────────────────────
print("queued_superseded_targets selector:")
L = aw.SUPERSEDED_LABEL
sel = aw.queued_superseded_targets({
    "a": {"label": L, "state": "Queued", "name": "a", "progress": 0},                                   # nothing taken -> purge
    "b": {"label": L, "state": "Seeding", "name": "b", "progress": 100, "seeding_time": BIG},            # not Queued -> skip
    "c": {"label": "radarr", "state": "Queued", "name": "c", "progress": 0},                             # wrong label -> skip
    "e": {"label": L, "state": "Queued", "name": "e", "progress": 100,                                   # unreg but seed unmet -> KEEP
          "tracker_status": UNREG},
    "f": {"label": L, "state": "Queued", "name": "f", "progress": 100, "seeding_time": BIG,              # seed met -> purge
          "tracker_status": UNREG},
    "g": {"label": L, "state": "Queued", "name": "g", "progress": 100, "seeding_time": SMALL,            # data taken, seed unmet -> KEEP
          "tracker_status": UNREG},
    "h": {"label": L, "state": "Queued", "name": "h", "progress": 100, "seeding_time": MID,              # midpoint under THR -> KEEP (kills '*'->'+')
          "tracker_status": UNREG},
    "i": {"label": L, "state": "Queued", "name": "i", "progress": 100, "seeding_time": THR,              # exactly at THR -> purge (kills '>='->'>')
          "tracker_status": UNREG},
    "j": {"label": L, "state": "Queued", "name": "j", "progress": 100, "seeding_time": THR - 1,          # one under THR -> KEEP (kills '86400'->'86399')
          "tracker_status": UNREG},
})
picked = sorted(t["hash"] for t in sel)
check("purge only zero-progress OR seed-met (e/g/h/j excluded, a/f/i kept)", picked, ["a", "f", "i"])

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
