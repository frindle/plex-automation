"""Adversarial fixture for: arr-webhook-recent-upgrade-priority-s3-sonarr-recent-fasttrack

Drives the real relabel_sonarr_upgrades() through its actual code path with
the Deluge/Sonarr endpoints stubbed at the boundary (requests.get / session.post),
and asserts on the OBSERVABLE side effects: which label was set, whether the
torrent went to core.queue_top or core.queue_bottom, and the returned count.

The four cases are exactly the adversarial matrix from TASK.md:
  * this-year airDateUtc        -> priority label + queue_top, never bottomed, count 1
  * airDateUtc ABSENT, recent   -> falls back to airDate, still fast-tracks
    airDate
  * NO air date at all          -> throttled lane (sonarr-upgrade + queue_bottom), no crash
  * decade-old airDateUtc       -> sonarr-upgrade + queue_bottom
plus a regression half: hasFile=False must NOT be relabeled at all.
"""
import datetime
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC (see scaffold note): dataclasses on 3.14 resolves string
# annotations through sys.modules[cls.__module__].
sys.modules["target"] = target
spec.loader.exec_module(target)

NOW_YEAR = datetime.datetime.now(datetime.timezone.utc).year


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _run_case(episode_payload):
    """Run relabel_sonarr_upgrades() with one 'sonarr'-labeled queued torrent.

    Returns (count, labels_set, top_hashes, bottom_hashes) where labels_set is
    the list of (hash, label) pairs passed to set_torrent_label and the hash
    lists are what was sent to core.queue_top / core.queue_bottom.
    """
    HASH = 'a' * 40
    calls = {'labels': [], 'top': None, 'bottom': None}

    def fake_get(url, **kw):
        if '/api/v3/queue' in url:
            return _Resp({'records': [{'downloadId': HASH.upper(), 'episodeId': 7}]})
        if '/api/v3/episode/' in url:
            return _Resp(episode_payload)
        raise AssertionError('unexpected GET {}'.format(url))

    def fake_post(url, **kw):
        body = kw.get('json') or {}
        method = body.get('method')
        params = (body.get('params') or [None])[0]
        if method == 'core.queue_top':
            calls['top'] = list(params)
        elif method == 'core.queue_bottom':
            calls['bottom'] = list(params)
        return _Resp({'result': True})

    orig_get, orig_post = target.requests.get, target.session.post
    try:
        target.requests.get = fake_get
        target.session.post = fake_post
        target.deluge_login = lambda: None
        target.get_all_torrents = lambda: {HASH: {'label': 'sonarr', 'name': 'ep'}}
        target.set_torrent_label = lambda h, l: calls['labels'].append((h, l))
        target.ensure_label_exists_named = lambda label: None
        count = target.relabel_sonarr_upgrades()
    finally:
        target.requests.get = orig_get
        target.session.post = orig_post

    return (count, tuple(calls['labels']), calls['top'], calls['bottom'])


PRIORITY_LABEL = 'sonarr-upgrade-recent'
THROTTLED_LABEL = 'sonarr-upgrade'
HASH = 'a' * 40

CASES = [
    ("this-year airDateUtc -> priority label + queue_top, never bottomed, count 1",
     lambda: _run_case({'hasFile': True, 'airDateUtc': '{}-03-14'.format(NOW_YEAR)}),
     (1, ((HASH, PRIORITY_LABEL),), [HASH], None)),

    ("airDateUtc ABSENT but recent airDate -> falls back and still fast-tracks",
     lambda: _run_case({'hasFile': True, 'airDate': '{}-01-05'.format(NOW_YEAR)}),
     (1, ((HASH, PRIORITY_LABEL),), [HASH], None)),

    ("NO air date at all -> throttled lane (sonarr-upgrade + queue_bottom), no crash",
     lambda: _run_case({'hasFile': True}),
     (1, ((HASH, THROTTLED_LABEL),), None, [HASH])),

    ("decade-old airDateUtc -> sonarr-upgrade label + core.queue_bottom",
     lambda: _run_case({'hasFile': True, 'airDateUtc': '2015-06-01'}),
     (1, ((HASH, THROTTLED_LABEL),), None, [HASH])),

    ("regression: hasFile=False is NOT an upgrade -- no relabel, count 0",
     lambda: _run_case({'hasFile': False, 'airDateUtc': '{}-03-14'.format(NOW_YEAR)}),
     (0, (), None, None)),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
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
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
