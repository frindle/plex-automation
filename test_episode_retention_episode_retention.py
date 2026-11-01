"""Frozen test: episode_retention must not report a deferred removal as removed.

remove_torrent (seed floor) returns 'deferred' when it kept the torrent.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400
APPLY = {'EPISODE_RETENTION_APPLY': '1'}

_spec = importlib.util.spec_from_file_location('er_target', os.path.join(_HERE, 'episode_retention.py'))
target = importlib.util.module_from_spec(_spec)
sys.modules['er_target'] = target
_spec.loader.exec_module(target)


def _ep(name):
    return {'name': name, 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0,
            'seeding_time': 30 * DAY, 'save_path': '/data/Downloads/Complete',
            'files': [{'path': name + '/' + name + '.mkv'}]}


def _torrents():
    return {'aaa': _ep('Show.S01E01.1080p.WEB'), 'bbb': _ep('Show.S01E02.1080p.WEB')}


def _run(remove, env=None):
    e = dict(APPLY)
    e.update(env or {})
    return target.episode_retention(_torrents(), remove, lambda t: False, env=e)


def test_deferred_not_counted_as_removed():
    calls = []

    def remove(h, remove_data):
        calls.append(h)
        return 'deferred' if h == 'aaa' else True

    result = _run(remove)
    removed = [e['hash'] for e in result['removed']]
    deferred = [e['hash'] for e in result.get('deferred', [])]
    assert removed == ['bbb'], f'removed: {removed!r}'
    assert deferred == ['aaa'], f'deferred: {deferred!r}'
    assert result['errors'] == [], f"errors: {result['errors']!r}"
    assert calls == ['aaa', 'bbb'], f'calls: {calls!r}'


def test_deferred_does_not_consume_limit():
    def remove(h, remove_data):
        return 'deferred' if h == 'aaa' else True

    result = _run(remove, {'EPISODE_RETENTION_PER_RUN': '1'})
    removed = [e['hash'] for e in result['removed']]
    assert removed == ['bbb'], f'removed: {removed!r}'
    assert [e['hash'] for e in result.get('deferred', [])] == ['aaa'], f"deferred: {result.get('deferred', [])!r}"


def test_true_and_none_still_removed():
    result = _run(lambda h, remove_data: None)
    removed = [e['hash'] for e in result['removed']]
    assert removed == ['aaa', 'bbb'], f'removed: {removed!r}'
    assert result.get('deferred', []) == [], f"deferred: {result.get('deferred', [])!r}"


def test_dry_run_unchanged():
    result = target.episode_retention(_torrents(), lambda h, d: 'deferred', lambda t: False, env={})
    assert result['dry_run'] is True, result
    assert [e['hash'] for e in result['planned']] == ['aaa', 'bbb'], f"planned: {result['planned']!r}"
    assert result['removed'] == [], f"removed: {result['removed']!r}"


TESTS = [test_deferred_not_counted_as_removed, test_deferred_does_not_consume_limit,
         test_true_and_none_still_removed, test_dry_run_unchanged]

if __name__ == '__main__':
    order = TESTS[::-1] if os.environ.get('TEST_ORDER') == 'reverse' else TESTS
    failed = 0
    for t in order:
        try:
            t()
            print(f'RESULT {t.__name__} PASS')
        except Exception as e:
            failed += 1
            print(f'RESULT {t.__name__} FAIL {type(e).__name__}: {e}')
    sys.exit(1 if failed else 0)
