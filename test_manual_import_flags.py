"""Tests for indexerFlags on ManualImport files (keeps grab-time custom formats like FreeLeech)."""

import os
import tempfile

import pytest

os.environ['ACTIVITY_LOG'] = os.path.join(tempfile.mkdtemp(), 'activity.jsonl')
aw = __import__('arr-webhook')


class Resp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


DL = 'B' * 40


def setup(monkeypatch, history, svc, cand_flags=None):
    posts = []
    gets = []
    cand = {'path': '/x/a.mkv', 'quality': {}, 'languages': [], 'releaseGroup': 'G', 'rejections': []}
    if svc == 'radarr':
        cand['movie'] = {'id': 7, 'title': 'Film', 'year': 2020}
    else:
        cand['series'] = {'id': 7, 'title': 'Show', 'year': 2020}
        cand['episodes'] = [{'id': 11}]
    if cand_flags is not None:
        cand['indexerFlags'] = cand_flags

    def fake_get(url, **kw):
        gets.append(url)
        if url.endswith('/manualimport'):
            return Resp([cand])
        if url.endswith('/history'):
            if isinstance(history, Exception):
                raise history
            return Resp({'records': history})
        raise AssertionError(url)

    def fake_post(url, **kw):
        posts.append(kw['json'])
        return Resp({})

    for name in ('RADARR_URL', 'SONARR_URL'):
        monkeypatch.setattr(aw, name, 'http://' + name.lower())
    monkeypatch.setattr(aw, 'RADARR_API_KEY', 'k')
    monkeypatch.setattr(aw, 'SONARR_API_KEY', 'k')
    monkeypatch.setattr(aw.requests, 'get', fake_get)
    monkeypatch.setattr(aw.requests, 'post', fake_post)
    monkeypatch.setattr(aw, '_llm_advise', lambda *a, **k: 'match')
    return posts, gets


def run(svc):
    if svc == 'radarr':
        return aw._try_radarr_manual_import({'downloadId': DL, 'movieId': 7, 'title': 'Film 2020'})
    return aw._try_sonarr_manual_import({'downloadId': DL, 'seriesId': 7, 'title': 'Show S01E01'})


def hist(flags):
    return [{'date': '2026-01-01T00:00:00Z', 'data': {'indexerFlags': flags}}]


@pytest.mark.parametrize('svc', ['radarr', 'sonarr'])
def test_candidate_flags_used_as_is(monkeypatch, svc):
    posts, gets = setup(monkeypatch, hist('G_Freeleech'), svc, cand_flags=33)
    ok, _ = run(svc)
    assert ok
    assert posts[0]['files'][0]['indexerFlags'] == 33
    assert not any(g.endswith('/history') for g in gets)


def test_radarr_history_single_flag(monkeypatch):
    posts, _ = setup(monkeypatch, hist('G_Freeleech'), 'radarr', cand_flags=0)
    assert run('radarr')[0]
    assert posts[0]['files'][0]['indexerFlags'] == 1


def test_radarr_history_two_flags_ored(monkeypatch):
    posts, _ = setup(monkeypatch, hist('G_Freeleech, G_Internal'), 'radarr')
    run('radarr')
    assert posts[0]['files'][0]['indexerFlags'] == 33


def test_history_numeric_string(monkeypatch):
    posts, _ = setup(monkeypatch, hist('33'), 'radarr')
    run('radarr')
    assert posts[0]['files'][0]['indexerFlags'] == 33


def test_sonarr_history_names(monkeypatch):
    posts, _ = setup(monkeypatch, hist('Freeleech, Internal'), 'sonarr')
    assert run('sonarr')[0]
    assert posts[0]['files'][0]['indexerFlags'] == 9


def test_newest_history_record_wins(monkeypatch):
    h = [{'date': '2025-01-01', 'data': {'indexerFlags': 'G_Internal'}},
         {'date': '2026-01-01', 'data': {'indexerFlags': 'G_Freeleech'}}]
    posts, _ = setup(monkeypatch, h, 'radarr')
    run('radarr')
    assert posts[0]['files'][0]['indexerFlags'] == 1


@pytest.mark.parametrize('svc', ['radarr', 'sonarr'])
def test_history_error_gives_zero_and_import_still_posts(monkeypatch, svc):
    posts, _ = setup(monkeypatch, RuntimeError('boom'), svc)
    ok, _ = run(svc)
    assert ok
    assert posts[0]['files'][0]['indexerFlags'] == 0


def test_no_history_record_gives_zero(monkeypatch):
    posts, _ = setup(monkeypatch, [], 'radarr')
    run('radarr')
    assert posts[0]['files'][0]['indexerFlags'] == 0
