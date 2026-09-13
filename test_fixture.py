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
    def __init__(self, payload, raise_error=False):
        self._payload = payload
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise RuntimeError('HTTP 500 from Deluge')
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Intercepts every Deluge RPC. Returns the configured status for
    core.get_torrent_status (and records how it was called); records
    (hash, remove_data) for every remove RPC."""
    def __init__(self, status_result=None, raise_on_status=False,
                 status_http_error=False, status_json_result=None):
        self.status_result = status_result            # info dict .json() returns
        self.raise_on_status = raise_on_status         # the fetch POST itself raises
        self.status_http_error = status_http_error     # response.raise_for_status() raises
        self.status_json_result = status_json_result   # info seen ONLY if raise_for_status is skipped
        self.removes = []   # list of (hash_or_'BATCH', remove_data_bool)
        self.fetch = None   # (rpc_id, timeout, sorted(fields)) of the status fetch

    def post(self, url, json=None, timeout=None):
        method = (json or {}).get('method')
        params = (json or {}).get('params') or []
        if method == 'core.get_torrent_status':
            if self.raise_on_status:
                raise RuntimeError('deluge unreachable')
            fields = params[1] if len(params) > 1 else []
            self.fetch = ((json or {}).get('id'), timeout, sorted(fields))
            payload = {'result': self.status_json_result if self.status_http_error
                       else self.status_result}
            return _Resp(payload, raise_error=self.status_http_error)
        if method == 'core.remove_torrent':
            self.removes.append((params[0], bool(params[1])))
            return _Resp({'result': True})
        if method == 'core.remove_torrents':
            self.removes.append(('BATCH', bool(params[1])))
            return _Resp({'result': True})
        return _Resp({'result': None})


def _run(status_result=None, raise_on_status=False, status_http_error=False,
         status_json_result=None, **kwargs):
    fake = _FakeSession(status_result=status_result, raise_on_status=raise_on_status,
                        status_http_error=status_http_error,
                        status_json_result=status_json_result)
    orig = target.session
    target.session = fake
    try:
        target.remove_torrent('abc123hash', **kwargs)
    finally:
        target.session = orig
    return fake


def deleted_data(**kw):
    """Return whether the actual core.remove_torrent RPC that fired asked Deluge
    to DELETE the data -- the guard DECISION, not a proxy."""
    fake = _run(**kw)
    single = [rd for (h, rd) in fake.removes if h != 'BATCH']
    assert single, 'wrapper fired no core.remove_torrent'
    return single[-1]


def fetch_meta():
    """The (rpc_id, timeout, sorted fields) the guard used to fetch status when
    the caller passed no info. Pins id/timeout/fields so a mutant of any of them
    is caught, and proves the fetch actually requests the fields the guard reads."""
    fake = _run(status_result=info(REG, SMALL))
    return fake.fetch


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


def bypass_routed(fn_name, forbidden_rpc, must_contain=None):
    src = _fn_src(fn_name)
    if src is None:
        return 'MISSING_FN'
    # must no longer issue the raw remove RPC, and must call the wrapper
    if forbidden_rpc in src:
        return 'STILL_DIRECT_RPC'
    if 'remove_torrent(' not in src:
        return 'NO_WRAPPER_CALL'
    # for the unseed bypass: the wrapper must be called keeping files, so a
    # mutant that flips remove_data=False -> True (re-arming a data delete on
    # the stalled-upgrade sweep) is caught here.
    if must_contain is not None and must_contain not in src:
        return 'WRONG_REMOVE_DATA'
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
    # (d2) response.raise_for_status() must be honoured: an HTTP-error status
    # response must NOT be parsed as valid info. If the guard skips
    # raise_for_status it would read UNREG+BIG and wrongly ALLOW deletion; with
    # it, the error is caught -> info={} -> fail-safe REFUSE.
    ("http-error status response -> raise_for_status honoured -> REFUSED",
     lambda: deleted_data(status_http_error=True, status_json_result=info(UNREG, BIG)), False),
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
    #     AND still keeps files (remove_data=False) -- catches a False->True flip
    ("purge_stalled_upgrade_torrents routed through wrapper, keeps files",
     lambda: bypass_routed('purge_stalled_upgrade_torrents', 'core.remove_torrents',
                           must_contain='remove_data=False'), 'ROUTED'),
    # (l) the guard fetches status with the exact id/timeout/fields the spec pins
    ("guard fetches status with id 8, timeout 10, the two read fields",
     lambda: fetch_meta(), (8, 10, ['seeding_time', 'tracker_status'])),
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
