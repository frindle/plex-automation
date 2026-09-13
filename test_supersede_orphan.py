"""Self-checks for the supersede-orphan fix.

The supersede workflow used to leave a pile of 0%/paused "superseded" torrents:
supersede_torrent() calls core.move_storage AFTER *arr's upgrade import has
already deleted/overwritten the old file, so move_storage relocates a fileless
torrent to Just4Seeding where it sits at 0% forever. It never accrues
seeding_time, so cleanup_superseded (gated on the seed window) can never reap
it -> it piles up (the 40 paused-0% orphans seen after a reboot recheck).

This covers both guards:
  * supersede_torrent only moves when the torrent actually has data on disk.
  * cleanup_superseded reaps dataless 0% superseded orphans with
    remove_data=False, and its mount_healthy gate refuses to do so when every
    torrent reads 0% (the shfs mount-race signature) so a mount blip can never
    trigger mass removal.

No framework, no live Deluge. Run: python test_supersede_orphan.py
"""


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _session_for(total_done_by_hash, boom=False):
    """Fake Deluge JSON-RPC: core.get_torrents_status -> {'total_done': N}."""
    class S:
        def post(self, url, json=None, timeout=None, **kw):
            if boom:
                raise RuntimeError('deluge down')
            ids = (json.get('params') or [{}])[0].get('id') or []
            result = {h: {'total_done': total_done_by_hash.get(h, 0)} for h in ids}
            return _Resp({'result': result})
    return S()


def _install(aw, torrents=None, total_done=None, boom=False):
    """Stub the live surface and record side effects. Returns the log."""
    calls = {'label': [], 'move': [], 'remove': [], 'activity': []}
    aw.session = _session_for(total_done or {}, boom=boom)
    aw.deluge_login = lambda: None
    aw.get_all_torrents = lambda: (torrents or {})
    aw.set_torrent_label = lambda h, l: calls['label'].append((h, l))
    aw.move_torrent_storage = lambda h, d: calls['move'].append((h, d))
    aw.remove_torrent = lambda h, remove_data=True: calls['remove'].append((h, remove_data))
    aw.record_activity = lambda cat, summary: calls['activity'].append(cat)
    return calls


def main():
    aw = __import__('arr-webhook')
    SEED = aw.SEED_DAYS * 86400
    SUP = aw.SUPERSEDED_LABEL

    # --- supersede_torrent: has data -> labeled + moved --------------------
    c = _install(aw, total_done={'H': 999999})
    aw.supersede_torrent('H')
    assert ('H', SUP) in c['label']
    assert ('H', aw.SEEDING_DIR) in c['move']
    assert 'supersede-no-data' not in c['activity']
    print('OK  supersede has-data -> labeled + moved')

    # --- supersede_torrent: no data -> labeled, NOT moved, orphan avoided --
    c = _install(aw, total_done={'H': 0})
    aw.supersede_torrent('H')
    assert ('H', SUP) in c['label']
    assert c['move'] == [], 'must NOT move a dataless torrent'
    assert 'supersede-no-data' in c['activity']
    print('OK  supersede no-data  -> labeled, not moved (orphan avoided)')

    # --- supersede_torrent: probe error -> fail-safe, move happens ---------
    c = _install(aw, total_done={}, boom=True)
    aw.supersede_torrent('H')
    assert ('H', aw.SEEDING_DIR) in c['move'], 'probe error must fail-safe to move'
    print('OK  supersede probe-error -> fail-safe assumes data (moves)')

    # --- cleanup_superseded: healthy mount ---------------------------------
    #   seedmet -> remove_data=True ; orphan -> remove_data=False ;
    #   active(has data) -> kept ; non-superseded -> untouched
    torrents = {
        'seedmet': {'label': SUP, 'seeding_time': SEED + 10, 'progress': 100, 'name': 'seedmet'},
        'orphan':  {'label': SUP, 'seeding_time': 0,          'progress': 0,   'name': 'orphan'},
        'active':  {'label': SUP, 'seeding_time': 1000,       'progress': 0,   'name': 'active'},
        'other':   {'label': 'radarr', 'seeding_time': 0,     'progress': 100, 'name': 'other'},
    }
    c = _install(aw, torrents=torrents, total_done={'orphan': 0, 'active': 555, 'seedmet': 1})
    aw.cleanup_superseded()
    assert ('seedmet', True) in c['remove'], 'seed-met -> remove WITH data'
    assert ('orphan', False) in c['remove'], 'orphan -> remove WITHOUT data'
    assert not any(h == 'active' for h, _ in c['remove']), '0%-but-has-data must be kept'
    assert not any(h == 'other' for h, _ in c['remove']), 'non-superseded untouched'
    print('OK  cleanup healthy -> seedmet rm+data, orphan rm no-data, active kept')

    # --- cleanup_superseded: mount down (all 0%) -> NO removals ------------
    down = {
        'a': {'label': SUP, 'seeding_time': 1000, 'progress': 0, 'name': 'a'},
        'b': {'label': SUP, 'seeding_time': 2000, 'progress': 0, 'name': 'b'},
        'c': {'label': 'radarr', 'seeding_time': 0, 'progress': 0, 'name': 'c'},
    }
    c = _install(aw, torrents=down, total_done={'a': 0, 'b': 0})
    aw.cleanup_superseded()
    assert c['remove'] == [], f'mount-race must reap nothing, got {c["remove"]}'
    print('OK  cleanup mount-down -> zero removals (footgun safe)')

    print('\nALL SUPERSEDE-ORPHAN TESTS PASS')


if __name__ == '__main__':
    main()
