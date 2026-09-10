"""Regression: the '-upgrade' throttle label must be reserved for REAL upgrades.

Bug: is_upgrade_radarr() used to return 'old_gap' for a first-time grab of an
old catalog movie (no file on disk, release year older than a rolling cutoff),
which the Grab handler then labeled 'radarr-upgrade'. So brand-new grabs of old
films (e.g. "10 Things I Hate About You" 1999, "Save the Last Dance" 2001) were
mislabeled as upgrades. Penn: "upgrade should only go to upgrade."

Fix: is_upgrade_radarr() returns 'upgrade' iff the movie already has a file,
else None. No-file grabs -- of ANY release year -- are new downloads and keep
the plain 'radarr' label.

Discriminating case (fails at baseline, passes on the fix): an OLD, no-file
grab returns None, not 'old_gap'.
"""
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'arr-webhook.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_movie(monkeypatch, movie):
    monkeypatch.setattr(target.requests, 'get', lambda *a, **k: _Resp(movie))


DATA = {'movie': {'id': 42}}


def test_has_file_is_upgrade(monkeypatch):
    _patch_movie(monkeypatch, {'hasFile': True, 'year': 2020})
    assert target.is_upgrade_radarr(DATA) == 'upgrade'


def test_old_no_file_is_not_upgrade(monkeypatch):
    # The regression: 1999, no file. Previously -> 'old_gap' -> 'radarr-upgrade'.
    _patch_movie(monkeypatch, {'hasFile': False, 'year': 1999})
    assert target.is_upgrade_radarr(DATA) is None


def test_recent_no_file_is_not_upgrade(monkeypatch):
    _patch_movie(monkeypatch, {'hasFile': False, 'year': 2024})
    assert target.is_upgrade_radarr(DATA) is None


def test_no_movie_id_is_none(monkeypatch):
    # Should not even hit the API; return None.
    called = {'n': 0}

    def _boom(*a, **k):
        called['n'] += 1
        return _Resp({})
    monkeypatch.setattr(target.requests, 'get', _boom)
    assert target.is_upgrade_radarr({'movie': {}}) is None
    assert called['n'] == 0
