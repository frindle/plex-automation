#!/usr/bin/env python3
"""Adversarial behavioural test: /orphan-scan mode=all must NOT os.remove an
'untracked' file that Deluge is still seeding (that is a hit-and-run), the same
way it already protects 'dupe' files. We stand up a real temp movie library
with two untracked video files — one that Deluge reports as seeding, one not —
stub Radarr (nothing tracked) and the Deluge file list, then drive the real
route with ?delete=1&mode=all and assert the seeding file survives on disk.
"""
import os, sys, tempfile
aw = __import__("arr-webhook")

fails = 0
def check(desc, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {'ok ' if ok else 'FAIL'}: {desc}: got {got!r} want {want!r}")

# temp movie library with two untracked video files
base = os.path.realpath(tempfile.mkdtemp())
lib = os.path.join(base, "movies"); os.makedirs(lib)
seeding_file = os.path.join(lib, "StillSeeding.2021.1080p.mkv")
orphan_file = os.path.join(lib, "DeadOrphan.2019.1080p.mkv")
open(seeding_file, "w").write("x")
open(orphan_file, "w").write("x")

aw.MOVIES_LIBRARY = lib
aw.RADARR_API_KEY = "x"

# Radarr: nothing tracked -> both files classify as 'untracked'
class _R:
    def raise_for_status(self): pass
    def json(self): return []
aw.requests.get = lambda *a, **k: _R()

# Deluge: report StillSeeding.* as an actively-seeding torrent's file
aw.deluge_login = lambda *a, **k: None
class _Resp:
    def raise_for_status(self): pass
    def json(self): return {"result": {"h1": {"name": "StillSeeding.2021.1080p.mkv",
                                              "files": [{"path": "StillSeeding.2021.1080p.mkv"}]}}}
class _Sess:
    def post(self, *a, **k): return _Resp()
aw.session = _Sess()

aw.app.testing = False
c = aw.app.test_client()
r = c.get("/orphan-scan?delete=1&mode=all")
print(f"  route status={r.status_code}")

print("orphan-scan mode=all seeding protection:")
check("actively-seeding untracked file is NOT deleted", os.path.exists(seeding_file), True)
check("non-seeding orphan IS deleted", os.path.exists(orphan_file), False)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
