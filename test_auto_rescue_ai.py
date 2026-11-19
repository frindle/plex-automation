"""auto-rescue: after the scans run out, an LLM-checked ManualImport instead of 'needs manual import'."""

import json
import os
import tempfile

import pytest

os.environ.setdefault('ACTIVITY_LOG', os.path.join(tempfile.mkdtemp(), 'activity.jsonl'))
aw = __import__('arr-webhook')

H = 'abc123'
SAVE = '/data/Downloads/Complete/sonarr'
NAME = 'Show.S01E02.1080p'
MAX = aw.AUTO_RESCUE_MAX_ATTEMPTS


class Resp:
    ok = True

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def cand(svc='sonarr', target=7, rejections=(), path='/x/a.mkv'):
    c = {'path': path, 'quality': {}, 'languages': [], 'releaseGroup': 'G', 'rejections': [{'reason': r, 'type': 'permanent'} for r in rejections]}
    if svc == 'radarr':
        c['movie'] = {'id': target, 'title': 'Film', 'year': 2020}
    else:
        c['series'] = {'id': target, 'title': 'Show', 'year': 2020}
        c['episodes'] = [{'id': 11}]
    return c


class World:
    def __init__(self, monkeypatch, tmp_path, svc='sonarr', by_id=(), by_folder=(), advice='match', pending=False):
        self.svc, self.gets, self.cmds = svc, [], []
        self.by_id, self.by_folder, self.imported = list(by_id), list(by_folder), False
        label = 'radarr' if svc == 'radarr' else 'sonarr'
        torrents = {H: {'name': NAME, 'label': label, 'save_path': SAVE, 'progress': 100.0}}
        grab = {'eventType': 'grabbed', 'date': '2026-10-01T00:00:00Z', 'sourceTitle': NAME}
        grab.update({'movieId': 7} if svc == 'radarr' else {'seriesId': 7, 'episodeId': 11})
        self.grab = grab

        def get(url, params=None, **kw):
            params = dict(params or {})
            self.gets.append((url.rsplit('/', 1)[-1], params))
            if url.endswith('/manualimport'):
                return Resp(self.by_id if 'downloadId' in params else self.by_folder)
            if url.endswith('/history'):
                recs = [self.grab] if params.get('downloadId') else []
                if self.imported:
                    recs.append({'eventType': 'downloadFolderImported', 'date': '2026-10-02T00:00:00Z'})
                return Resp({'records': recs})
            raise AssertionError(url)

        def post(url, json=None, **kw):
            self.cmds.append(json)
            return Resp({'id': len(self.cmds)})

        def advise(kind, facts, rules_action, info=None):
            assert kind == 'import_identity'
            if pending and info is not None:
                info['pending'] = True
                return 'unsure'  # a pending decision never answers match
            return advice

        monkeypatch.setattr(aw.requests, 'get', get)
        monkeypatch.setattr(aw.requests, 'post', post)
        monkeypatch.setattr(aw.session, 'post', lambda url, json=None, **kw: Resp({'result': torrents}))
        monkeypatch.setattr(aw, 'deluge_login', lambda: None)
        monkeypatch.setattr(aw, '_llm_advise', advise)
        monkeypatch.setattr(aw, 'ACTIVITY_LOG', str(tmp_path / 'act.jsonl'))
        monkeypatch.setattr(aw, 'RESCUE_STATE_PATH', str(tmp_path / 'rescue.json'))
        monkeypatch.setattr(aw, '_rescue_attempts', {H: MAX})  # the scans already ran out
        monkeypatch.setattr(aw, '_rescue_gave_up', set())
        monkeypatch.setattr(aw, 'SONARR_API_KEY', 'k')
        monkeypatch.setattr(aw, 'RADARR_API_KEY', 'k')

    def run(self):
        return aw._auto_rescue(self.svc)

    def acts(self):
        p = aw.ACTIVITY_LOG
        if not os.path.exists(p):
            return []
        return [json.loads(line) for line in open(p) if line.strip()]

    def manual_imports(self):
        return [c for c in self.cmds if c.get('name') == 'ManualImport']


def test_untracked_download_is_imported_by_folder_after_llm_ok(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()])
    w.run()
    folder_gets = [p for name, p in w.gets if name == 'manualimport' and 'folder' in p]
    assert folder_gets == [{'folder': f'{SAVE}/{NAME}', 'filterExistingFiles': 'true'}]
    mi = w.manual_imports()
    assert len(mi) == 1 and mi[0]['importMode'] == 'copy'
    f = mi[0]['files'][0]
    assert f['seriesId'] == 7 and f['episodeIds'] == [11]
    assert 'downloadId' not in f  # the *arr can't resolve an untracked download id
    assert [a['category'] for a in w.acts()] == ['import-rescue']
    assert aw._rescue_attempts[H] == MAX + 1 and H not in aw._rescue_gave_up
    assert not [c for c in w.cmds if c.get('name') == 'DownloadedEpisodesScan']


def test_tracked_download_keeps_download_id(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_id=[cand()])
    w.run()
    assert w.manual_imports()[0]['files'][0]['downloadId'] == H.upper()
    assert not [p for name, p in w.gets if name == 'manualimport' and 'folder' in p]


def test_llm_pending_waits_quietly(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()], pending=True)
    w.run()
    assert w.manual_imports() == [] and w.acts() == []
    assert aw._rescue_attempts[H] == MAX and H not in aw._rescue_gave_up


def test_llm_mismatch_asks_a_person(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()], advice='mismatch')
    w.run()
    assert w.manual_imports() == []
    acts = w.acts()
    assert [a['category'] for a in acts] == ['rescue-gave-up']
    assert 'did not confirm' in acts[0]['summary'] and 'needs manual import' in acts[0]['summary']
    assert H in aw._rescue_gave_up


def test_library_already_has_it_closes_quietly(monkeypatch, tmp_path):
    files = [cand(rejections=['Not an upgrade for existing episode file(s)']),
             cand(rejections=['Sample'], path='/x/sample.mkv')]
    w = World(monkeypatch, tmp_path, by_folder=files)
    w.run()
    assert w.manual_imports() == []
    assert [a['category'] for a in w.acts()] == ['rescue-not-needed']
    assert H in aw._rescue_gave_up
    w.run()  # a later pass stays quiet
    assert [a['category'] for a in w.acts()] == ['rescue-not-needed']


def test_other_rejection_still_asks_a_person(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand(rejections=['Invalid season or episode'])])
    w.run()
    assert [a['category'] for a in w.acts()] == ['rescue-gave-up']
    assert 'no clean candidate' in w.acts()[0]['summary']


def test_queued_but_never_recorded_gives_up_after_the_ai_tries(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()])
    for _ in range(aw.AUTO_RESCUE_AI_TRIES + 2):
        w.run()
    assert len(w.manual_imports()) == aw.AUTO_RESCUE_AI_TRIES
    cats = [a['category'] for a in w.acts()]
    assert cats == ['import-rescue'] * aw.AUTO_RESCUE_AI_TRIES + ['rescue-gave-up']
    assert 'no import was recorded' in w.acts()[-1]['summary']


def test_import_lands_clears_state(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()])
    w.run()
    w.imported = True
    r = w.run()
    assert r['skipped']['already_imported'] == 1
    assert H not in aw._rescue_attempts and H not in aw._rescue_gave_up


def test_radarr_uses_movie_id(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, svc='radarr', by_folder=[cand('radarr')])
    w.run()
    f = w.manual_imports()[0]['files'][0]
    assert f['movieId'] == 7 and 'downloadId' not in f


def test_dry_run_never_imports(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()])
    aw._auto_rescue('sonarr', dry_run=True)
    assert w.cmds == [] and w.acts() == []


@pytest.mark.parametrize('reason', ['Not an upgrade for existing episode file(s)',
                                    'Not a Custom Format upgrade for existing episode file(s)',
                                    'Episode file already imported at 10/2/2026',
                                    'Existing file on disk is of equal or higher preference'])
def test_not_needed_reasons(reason):
    assert aw._not_needed_detail([cand(rejections=[reason])], 'series', 7).startswith(aw.IMPORT_NOT_NEEDED)


def test_not_needed_needs_every_file_refused_for_that_reason():
    mixed = [cand(rejections=['Not an upgrade for existing episode file(s)']), cand(rejections=['Unknown Series'])]
    assert aw._not_needed_detail(mixed, 'series', 7) is None
    assert aw._not_needed_detail([cand(rejections=['Sample'])], 'series', 7) is None
    assert aw._not_needed_detail([cand(target=8, rejections=['Not an upgrade'])], 'series', 7) is None


def test_retry_gave_up_goes_straight_to_the_llm_step(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path, by_folder=[cand()])
    aw._rescue_attempts[H] = MAX
    aw._rescue_gave_up.add(H)
    with aw.app.test_request_context('/auto-rescue?service=sonarr&retry_gave_up=1'):
        aw.auto_rescue_endpoint()
    assert len(w.manual_imports()) == 1
    assert not [c for c in w.cmds if c.get('name') == 'DownloadedEpisodesScan']
    assert aw._rescue_attempts[H] == MAX + 1 and H not in aw._rescue_gave_up
