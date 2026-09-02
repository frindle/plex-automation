"""Adversarial behavioral test for should_hard_delete_on_upgrade + call-site wiring.

The property: a superseded torrent's DATA may be hard-deleted on upgrade ONLY
when same_group AND tracker-unregistered AND seed obligation met
(seeding_time >= SEED_DAYS*86400). Every other case must return False so the
caller soft-supersedes (keeps seeding) -> no hit-and-run. The Diplomat case is
same_group + unregistered + tiny seeding_time -> MUST be False.
"""
import ast

aw = __import__('arr-webhook')

SEED_SECS = aw.SEED_DAYS * 86400
UNREG = 'Error: Unregistered torrent'          # a real unregistered tracker_status
REG = 'Announce OK'                            # a normal registered status
BIG = SEED_SECS + 1                            # seed obligation met
SMALL = 60                                     # 1 minute in -> obligation NOT met

def info(tracker_status, seeding_time):
    return {'tracker_status': tracker_status, 'seeding_time': seeding_time}

def check(desc, got, want):
    assert got == want, f'FAIL: {desc}: got {got!r} want {want!r}'
    print(f'  ok: {desc} -> {got}')

def test_pure_helper():
    f = aw.should_hard_delete_on_upgrade
    # THE DIPLOMAT CASE: same group, unregistered, but barely seeded -> MUST NOT delete
    check('same-group unregistered but seed<obligation (Diplomat)', f(info(UNREG, SMALL), True), False)
    # safe delete: same group, unregistered, obligation met
    check('same-group unregistered, seed>=obligation', f(info(UNREG, BIG), True), True)
    # registered (still in swarm/db) -> never delete regardless of seed time
    check('same-group registered, big seed', f(info(REG, BIG), True), False)
    # different/unknown group -> never delete even if unregistered+seeded
    check('different-group unregistered, big seed', f(info(UNREG, BIG), False), False)
    # exactly at threshold -> deletable (>=)
    check('same-group unregistered, seed==obligation', f(info(UNREG, SEED_SECS), True), True)
    # one second under threshold -> not deletable
    check('same-group unregistered, seed==obligation-1', f(info(UNREG, SEED_SECS - 1), True), False)
    # missing / None seeding_time -> treat as 0 -> not deletable
    check('same-group unregistered, seeding_time missing', f({'tracker_status': UNREG}, True), False)
    check('same-group unregistered, seeding_time None', f(info(UNREG, None), True), False)
    # empty/unknown tracker status is not "unregistered" -> not deletable
    check('same-group empty tracker status, big seed', f(info('', BIG), True), False)

def test_call_site_wired():
    """The hard-delete inside handle_upgrade_import must go through the helper,
    and the old ungated condition must be gone."""
    src = open('arr-webhook.py').read()
    assert 'if same_group and torrent_is_unregistered(info):' not in src, \
        'FAIL: old ungated condition still present at the call site'
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == 'handle_upgrade_import'), None)
    assert fn is not None, 'FAIL: handle_upgrade_import not found'
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'should_hard_delete_on_upgrade']
    assert calls, 'FAIL: handle_upgrade_import does not call should_hard_delete_on_upgrade'
    # the helper itself must exist and reference the seed-time threshold
    helper = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == 'should_hard_delete_on_upgrade'), None)
    assert helper is not None, 'FAIL: should_hard_delete_on_upgrade not defined'
    assert 'SEED_DAYS * 86400' in ast.get_source_segment(src, helper), \
        'FAIL: helper does not gate on SEED_DAYS * 86400'
    print('  ok: call site wired through should_hard_delete_on_upgrade; old condition gone')

if __name__ == '__main__':
    test_pure_helper()
    test_call_site_wired()
    print('test_diplomat_seedgate: all assertions passed')
