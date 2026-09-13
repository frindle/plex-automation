"""Adversarial behavioural fixture for: plex-hnr-guard

Property under test: NO remove_data=True path may delete a torrent's DATA while
the tracker still knows it (NOT torrent_is_unregistered) AND its seeding_time is
under SEED_DAYS*86400. Such a removal must be downgraded to remove_data=False
(entry removed, files kept). Unregistered OR seed-obligation-met => data delete
allowed. The two direct-RPC bypasses (purge_non_radarr,
purge_stalled_upgrade_torrents) must be routed through the wrapper.

We drive the REAL target.remove_torrent with a fake Deluge session that records
the actual remove RPC's remove_data flag -- the guard DECISION, not a proxy.
"""
import sys
import ast
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

# Don't touch the real activity log on disk during the test.
target.record_activity = lambda *a, **k: None

SEED_SECS = target.SEED_DAYS * 86400
REG = 'Announce OK'                       # tracker still knows it (registered)
UNREG = 'Error: Unregistered torrent'     # tracker has dropped it
SMALL = 60                                # 1 min in -> seed obligation NOT met
BIG = SEED_SECS + 1                       # obligation met


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Intercepts every Deluge RPC. Returns the configured status for
    core.get_torrent_status; records (hash, remove_data) for every remove."""
    def __init__(self, status_result=None, raise_on_status=False):
        self.status_result = status_result
        self.raise_on_status = raise_on_status
        self.removes = []  # list of (hash_or_'BATCH', remove_data_bool)

    def post(self, url, json=None, timeout=None):
        method = (json or {}).get('method')
        params = (json or {}).get('params') or []
        if method == 'core.get_torrent_status':
            if self.raise_on_status:
                raise RuntimeError('deluge unreachable')
            return _Resp({'result': self.status_result})
        if method == 'core.remove_torrent':
            self.removes.append((params[0], bool(params[1])))
            return _Resp({'result': True})
        if method == 'core.remove_torrents':
            self.removes.append(('BATCH', bool(params[1])))
            return _Resp({'result': True})
        return _Resp({'result': None})


def deleted_data(status_result=None, raise_on_status=False, **kwargs):
    """Call the real wrapper with a fake session; return whether the actual
    core.remove_torrent RPC that fired asked Deluge to DELETE the data."""
    fake = _FakeSession(status_result=status_result, raise_on_status=raise_on_status)
    orig = target.session
    target.session = fake
    try:
        target.remove_torrent('abc123hash', **kwargs)
    finally:
        target.session = orig
    # the guarded wrapper always fires exactly one core.remove_torrent
    single = [rd for (h, rd) in fake.removes if h != 'BATCH']
    assert single, 'wrapper fired no core.remove_torrent'
    return single[-1]


def info(tracker_status, seeding_time):
    return {'tracker_status': tracker_status, 'seeding_time': seeding_time}


# --- AST helpers: prove the direct-RPC bypasses are routed through wrapper ---
_SRC = open('arr-webhook.py').read()
_TREE = ast.parse(_SRC)


def _fn_src(name):
    for n in ast.walk(_TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(_SRC, n) or ''
    return None


def bypass_routed(fn_name, forbidden_rpc):
    src = _fn_src(fn_name)
    if src is None:
        return 'MISSING_FN'
    # must no longer issue the raw remove RPC, and must call the wrapper
    if forbidden_rpc in src:
        return 'STILL_DIRECT_RPC'
    if 'remove_torrent(' not in src:
        return 'NO_WRAPPER_CALL'
    return 'ROUTED'


CASES = [
    # (a) STILL REGISTERED + under seed window -> data deletion MUST be refused
    ("registered + seeded<SEED_DAYS -> data delete REFUSED (the 9-HnR case)",
     lambda: deleted_data(status_result=info(REG, SMALL)), False),
    # (b) tracker dropped it -> deleting data is free -> allowed
    ("unregistered -> data delete allowed",
     lambda: deleted_data(status_result=info(UNREG, SMALL)), True),
    # (c) registered but seed obligation met -> allowed
    ("registered + seeded>=SEED_DAYS -> data delete allowed",
     lambda: deleted_data(status_result=info(REG, BIG)), True),
    # (d) status fetch fails -> must fail SAFE (assume registered, keep files)
    ("status fetch fails -> data delete REFUSED (fail-safe)",
     lambda: deleted_data(raise_on_status=True), False),
    # (e) explicit remove_data=False must stay False regardless (unseed path)
    ("explicit remove_data=False stays entry-only (files kept)",
     lambda: deleted_data(status_result=info(REG, BIG), remove_data=False), False),
    # (f) boundary: exactly at threshold counts as obligation met (>=)
    ("registered + seeded==SEED_DAYS threshold -> allowed",
     lambda: deleted_data(status_result=info(REG, SEED_SECS)), True),
    # (g) boundary: one second under threshold -> refused
    ("registered + seeded==threshold-1 -> REFUSED",
     lambda: deleted_data(status_result=info(REG, SEED_SECS - 1)), False),
    # (h) missing seeding_time on a registered torrent -> treat as 0 -> refused
    ("registered + seeding_time missing -> REFUSED",
     lambda: deleted_data(status_result={'tracker_status': REG}), False),
    # (i) empty/unknown tracker status is NOT 'unregistered' -> refused if unmet
    ("empty tracker status + low seed -> REFUSED (unknown != unregistered)",
     lambda: deleted_data(status_result=info('', SMALL)), False),
    # (j) bypass routing: purge_non_radarr no longer hits core.remove_torrent raw
    ("purge_non_radarr routed through wrapper",
     lambda: bypass_routed('purge_non_radarr', 'core.remove_torrent'), 'ROUTED'),
    # (k) bypass routing: purge_stalled_upgrade_torrents no longer hits raw RPC
    ("purge_stalled_upgrade_torrents routed through wrapper",
     lambda: bypass_routed('purge_stalled_upgrade_torrents', 'core.remove_torrents'), 'ROUTED'),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} case(s), need >= 3.".format(len(CASES)))
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
        else:
            print("  ok: {} -> {!r}".format(desc, got))
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
