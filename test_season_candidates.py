"""Frozen test: season_migration.candidates picks only library-seeded Sonarr
season packs that are complete and Seeding."""
import season_migration as sm


def T(name='Show.S01.1080p.WEB', label='sonarr', state='Seeding', progress=100.0,
      save_path='/data/Media/TV/Show'):
    return {'name': name, 'label': label, 'state': state, 'progress': progress, 'save_path': save_path}


def test_picks_library_season_packs_sorted():
    torrents = {'bb': T(), 'aa': T(name='Other.S02.720p'), 'cc': T(label='sonarr-upgrade')}
    assert sm.candidates(torrents) == ['aa', 'bb', 'cc']


def test_skips_episodes_movies_and_other_labels():
    torrents = {
        'ep': T(name='Show.S01E05.1080p'),
        'mv': T(label='radarr', save_path='/data/Media/Movies/X'),
        'no': T(label=''),
        'ok': T(),
    }
    assert sm.candidates(torrents) == ['ok']


def test_skips_not_seeding_or_incomplete():
    torrents = {'p': T(state='Paused'), 'e': T(state='Error'), 'd': T(state='Downloading', progress=50.0),
                'i': T(progress=99.9), 'ok': T()}
    assert sm.candidates(torrents) == ['ok']


def test_only_under_media_root():
    torrents = {
        'dl': T(save_path='/data/Downloads/Complete/tv'),
        'seed': T(save_path='/data/Downloads/Just4Seeding'),
        'look': T(save_path='/data/MediaX/TV'),
        'esc': T(save_path='/data/Media/../Downloads'),
        'root': T(save_path='/data/Media'),
        'case': T(save_path='/DATA/media/TV'),
    }
    assert sm.candidates(torrents) == ['case', 'root']


def test_unsafe_names_none_entries_and_empty():
    torrents = {'a': T(name=''), 'b': T(name='..'), 'c': T(name='.'), 'd': T(name='x/y'), 'ok': T()}
    assert sm.candidates(torrents) == ['ok']
    assert sm.candidates({}) == []
    assert sm.candidates({'x': None, 'ok': T()}) == ['ok']
