"""Upgrade imported from a REMOTE download client (the seedbox Deluge in the soft
cutover): the new torrent is not in home Deluge, so handle_upgrade_import used to
abort ("cannot identify new torrent") and the old home copy was never labeled
superseded. Now every matching home torrent is the old copy and gets superseded,
and an unfinished one is labeled but never resumed (nothing downloads at home).

Run: python3 -m pytest -q test_supersede_remote_import.py
"""
import importlib.util

spec = importlib.util.spec_from_file_location('aw_remote', 'arr-webhook.py')
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)

NEW = 'a' * 40          # the seedbox torrent (downloadId)
OLD = 'b' * 40          # the old home copy
OTHER = 'c' * 40        # a different movie


def _radarr_payload(client='seedbox-deluge', download_id=NEW):
    return {
        'eventType': 'Download', 'isUpgrade': True, 'downloadId': download_id.upper(),
        'downloadClient': client,
        'movie': {'id': 7, 'title': 'Dune Part Two', 'year': 2024},
        'movieFile': {'path': '/movies/Dune Part Two (2024)/Dune.Part.Two.2024.2160p.UHD.BluRay-GRP.mkv',
                      'releaseGroup': 'GRP'},
        'release': {'releaseTitle': 'Dune.Part.Two.2024.2160p.UHD.BluRay.TrueHD.7.1-GRP'},
    }


def _sonarr_pack_payload(client='seedbox-deluge'):
    return {
        'eventType': 'Download', 'isUpgrade': True, 'downloadId': NEW.upper(), 'downloadClient': client,
        'series': {'id': 3, 'title': 'Slow Horses'},
        'episodes': [{'seasonNumber': 2, 'episodeNumber': 1}, {'seasonNumber': 2, 'episodeNumber': 2}],
        'episodeFile': {'path': '/tv/Slow Horses/Season 02/Slow.Horses.S02.2160p.WEB-NEW.mkv', 'releaseGroup': 'NEW'},
    }


def _wire(monkeypatch, torrents):
    calls = {'superseded': [], 'pushover': [], 'flip': []}
    monkeypatch.setattr(aw, 'deluge_login', lambda *a, **k: None)
    monkeypatch.setattr(aw, 'ensure_label_exists', lambda *a, **k: None)
    monkeypatch.setattr(aw, 'get_all_torrents', lambda force=False: dict(torrents))

    def flip(h, source):
        calls['flip'].append(h)
        return ('not_found', None) if h not in torrents else ('already', torrents[h]['name'])
    monkeypatch.setattr(aw, 'relabel_download_to_base', flip)
    monkeypatch.setattr(aw, 'get_radarr_movie_titles', lambda mid: {'dune part two'})
    monkeypatch.setattr(aw, 'get_sonarr_series_titles', lambda sid: {'slow horses'})
    monkeypatch.setattr(aw, 'supersede_torrent', lambda h, resume=True: calls['superseded'].append((h, resume)))
    monkeypatch.setattr(aw, 'remove_torrent', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no deletes')))
    monkeypatch.setattr(aw, 'send_pushover', lambda **k: calls['pushover'].append(k))
    monkeypatch.setattr(aw, '_stuck_upgrade_tag_ask', lambda *a, **k: None)
    monkeypatch.setattr(aw, 'record_activity', lambda *a, **k: None)
    monkeypatch.setattr(aw, 'queued_superseded_targets', lambda t: [])
    aw._recent_upgrade_download_ids.clear()
    return calls


HOME = {
    OLD: {'name': 'Dune.Part.Two.2024.1080p.BluRay.x265-OLDGRP', 'label': 'radarr', 'state': 'Seeding', 'progress': 100.0},
    OTHER: {'name': 'Dune.1984.1080p.BluRay.x264-X', 'label': 'radarr', 'state': 'Seeding', 'progress': 100.0},
}


def test_remote_import_supersedes_old_home_copy_without_resume(monkeypatch):
    calls = _wire(monkeypatch, HOME)
    aw.handle_upgrade_import(_radarr_payload(), 'Radarr')
    assert calls['superseded'] == [(OLD, False)]
    assert calls['pushover'] == []                  # no "upgrade tag may be stuck" alert
    assert calls['flip'] == []                      # the seedbox torrent is not in home Deluge: never asked for


def test_remote_client_name_is_case_insensitive(monkeypatch):
    calls = _wire(monkeypatch, HOME)
    aw.handle_upgrade_import(_radarr_payload(client='Seedbox-Deluge'), 'Radarr')
    assert calls['superseded'] == [(OLD, False)]


def test_home_client_still_aborts_when_new_torrent_unknown(monkeypatch):
    calls = _wire(monkeypatch, HOME)
    aw.handle_upgrade_import(_radarr_payload(client='Deluge'), 'Radarr')
    assert calls['superseded'] == []                # unchanged: never guess on a home import
    assert len(calls['pushover']) == 1


def test_home_import_found_by_download_id_still_resumes(monkeypatch):
    torrents = dict(HOME)
    torrents[NEW] = {'name': 'Dune.Part.Two.2024.2160p.UHD.BluRay.TrueHD.7.1-GRP', 'label': 'radarr-upgrade',
                     'state': 'Seeding', 'progress': 100.0}
    calls = _wire(monkeypatch, torrents)
    aw.handle_upgrade_import(_radarr_payload(client='Deluge'), 'Radarr')
    assert calls['superseded'] == [(OLD, True)]


def test_remote_season_pack_does_not_spare_the_old_home_pack(monkeypatch):
    old_pack = 'd' * 40
    torrents = {old_pack: {'name': 'Slow.Horses.S02.1080p.WEB-OLD', 'label': 'sonarr', 'state': 'Seeding', 'progress': 100.0}}
    calls = _wire(monkeypatch, torrents)
    aw.handle_upgrade_import(_sonarr_pack_payload(), 'Sonarr')
    assert calls['superseded'] == [(old_pack, False)]


def test_gap_fill_import_from_seedbox_does_not_ask_home_deluge(monkeypatch):
    calls = _wire(monkeypatch, HOME)
    aw.handle_import_relabel({'downloadId': NEW.upper(), 'downloadClient': 'seedbox-deluge'}, 'Radarr')
    assert calls['flip'] == []
    aw.handle_import_relabel({'downloadId': NEW.upper(), 'downloadClient': 'Deluge'}, 'Radarr')
    assert calls['flip'] == [NEW]


def test_release_inflight_without_resume_never_resumes(monkeypatch):
    sent = []

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(aw.session, 'post', lambda url, json=None, timeout=None: sent.append(json['method']) or R())
    monkeypatch.setattr(aw, 'record_activity', lambda *a, **k: None)
    aw.release_inflight_superseded(OLD, 'Paused', 40.0, resume=False)
    assert sent == ['core.set_torrent_options']
    sent.clear()
    aw.release_inflight_superseded(OLD, 'Paused', 40.0)
    assert sent == ['core.set_torrent_options', 'core.resume_torrent']
