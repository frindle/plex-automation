"""move_list: auto picks, 'Move' playlists, de-dup by file, per-user tokens. Synthetic data only; Plex is faked."""
import pytest

import move_list as ML

NOW = 1_800_000_000.0
GB = 10 ** 9


def ep(rk, s, e, show='Show A', size=GB, watched=0, offset=0, file=None):
    return {'ratingKey': rk, 'type': 'episode', 'grandparentTitle': show, 'parentIndex': s, 'index': e,
            'viewCount': watched, 'viewOffset': offset,
            'Media': [{'Part': [{'file': file or f'/tv/{show}/S{s:02d}E{e:02d}.mkv', 'size': size}]}]}


def mov(rk, title, size=2 * GB, offset=0, file=None):
    return {'ratingKey': rk, 'type': 'movie', 'title': title, 'year': 2020, 'viewOffset': offset,
            'Media': [{'Part': [{'file': file or f'/mov/{title}.mkv', 'size': size}]}]}


class FakePlex:
    """Per-token world: {token: {'history': [...], 'leaves': {rk: [...]}, 'continue': [...], 'playlists': {name: [...]}}}."""
    def __init__(self, world, shared_xml=''):
        self.world, self.xml, self.calls = world, shared_xml, []

    def get(self, path, params, token):
        self.calls.append((path, token))
        w = self.world[token]
        if path == '/identity':
            return {'MediaContainer': {'machineIdentifier': 'machine1'}}
        if path == '/status/sessions/history/all':
            assert 'viewedAt>' in (params or {}), 'history must be limited to the last N days'
            return {'MediaContainer': {'Metadata': w.get('history', [])}}
        if path == '/hubs/continueWatching':
            return {'MediaContainer': {'Hub': [{'Metadata': w.get('continue', [])}]}}
        if path == '/playlists':
            return {'MediaContainer': {'Metadata': [{'ratingKey': n, 'title': n} for n in w.get('playlists', {})]}}
        if path.startswith('/playlists/'):
            return {'MediaContainer': {'Metadata': w['playlists'][path.split('/')[2]]}}
        if path.startswith('/library/metadata/'):
            rk = path.split('/')[3]
            return {'MediaContainer': {'Metadata': w['leaves'].get(rk, [])}}
        raise AssertionError(path)

    def tv(self, url, token):
        self.calls.append((url, token))
        assert 'machine1' in url
        return self.xml


def hist(show_rk):
    return {'type': 'episode', 'grandparentRatingKey': show_rk}


def xml_for(*pairs):
    inner = ''.join(f'<SharedServer id="{i}" userID="{i}" username="{n}" accessToken="{t}"/>' for i, (n, t) in enumerate(pairs, 1))
    return f'<MediaContainer>{inner}</MediaContainer>'


def run(fp):
    return ML.build(fp.get, fp.tv, 'OWNER', NOW)


def test_next_three_unwatched_after_the_last_watched_episode():
    eps = [ep(i, 1, i, watched=1 if i <= 2 else 0) for i in range(1, 8)]
    eps.insert(0, ep(99, 0, 1))  # an unwatched special before everything must not count
    got = ML.next_unwatched(eps)
    assert [e['index'] for e in got] == [3, 4, 5]


def test_unwatched_show_starts_at_the_first_episode_and_in_progress_counts():
    eps = [ep(1, 1, 1, offset=5000), ep(2, 1, 2), ep(3, 1, 3), ep(4, 1, 4)]
    assert [e['index'] for e in ML.next_unwatched(eps)] == [1, 2, 3]


def test_auto_picks_recent_shows_and_movies_in_progress_and_totals_size():
    fp = FakePlex({'OWNER': {'history': [hist('10'), hist('10')], 'leaves': {'10': [ep(i, 1, i, watched=1 if i == 1 else 0) for i in range(1, 8)]},
                             'continue': [mov(50, 'Film One', size=3 * GB, offset=90), mov(51, 'Unstarted', offset=0)]}})
    d = run(fp)
    assert d['count'] == 4 and d['total_gb'] == 6.0 and d['users'] == 1 and d['errors'] == 0, d
    titles = [i['title'] for i in d['items']]
    assert 'Film One (2020)' in titles and 'Unstarted (2020)' not in titles
    assert 'Show A S01E02' in titles and 'Show A S01E04' in titles and 'Show A S01E05' not in titles


def test_history_rows_with_only_grandparentKey_still_find_the_show():
    # Plex play history rows carry grandparentKey ("/library/metadata/<id>"); the move list showed no TV without this.
    real = [{'type': 'episode', 'grandparentKey': '/library/metadata/10'}, {'type': 'movie', 'grandparentKey': '/library/metadata/99'}]
    fp = FakePlex({'OWNER': {'history': real, 'leaves': {'10': [ep(i, 1, i, watched=1 if i == 1 else 0) for i in range(1, 8)]}}})
    titles = [i['title'] for i in run(fp)['items']]
    assert titles == ['Show A S01E02', 'Show A S01E03', 'Show A S01E04'], titles


def test_show_id_prefers_rating_key_and_ignores_odd_keys():
    assert ML.show_id({'grandparentRatingKey': 7, 'grandparentKey': '/library/metadata/8'}) == '7'
    assert ML.show_id({'grandparentKey': '/library/metadata/8/'}) == '8'
    assert ML.show_id({'grandparentKey': '/playlists/8'}) is None
    assert ML.show_id({}) is None


def test_playlist_named_move_takes_episodes_movies_seasons_and_whole_shows_case_insensitive():
    pl = [ep(1, 1, 1), mov(2, 'Pick'), {'ratingKey': 'sh', 'type': 'show'}, {'ratingKey': 'se', 'type': 'season'}]
    world = {'OWNER': {'playlists': {'move': pl, 'Other': [mov(3, 'Nope')]},
                       'leaves': {'sh': [ep(10, 1, 1, show='Whole'), ep(11, 1, 2, show='Whole')], 'se': [ep(20, 2, 1, show='Seas')]}}}
    d = run(FakePlex(world))
    titles = sorted(i['title'] for i in d['items'])
    assert titles == ['Pick (2020)', 'Seas S02E01', 'Show A S01E01', 'Whole S01E01', 'Whole S01E02'], titles
    assert all(i['reasons'] == ['playlist'] for i in d['items'])


def test_same_file_wanted_by_two_users_counts_once_with_both_reasons_and_users():
    shared = '/tv/Show A/S01E02.mkv'
    world = {'OWNER': {'history': [hist('10')], 'leaves': {'10': [ep(1, 1, 1, watched=1), ep(2, 1, 2, file=shared)]}},
             'TOK2': {'playlists': {'Move': [ep(2, 1, 2, file=shared)]}}}
    d = run(FakePlex(world, xml_for(('guest', 'TOK2'))))
    same = [i for i in d['items'] if i['file'] == shared]
    assert len(same) == 1 and d['count'] == 1 and d['users'] == 2, d
    assert same[0]['reasons'] == ['auto', 'playlist'] and same[0]['users'] == ['guest', 'owner'], same


def test_each_shared_user_is_read_with_their_own_token_and_the_owner_token_is_not_reused():
    world = {'OWNER': {}, 'TOK2': {'history': [hist('10')], 'leaves': {'10': [ep(5, 1, 1, show='Kids')]}}}
    fp = FakePlex(world, xml_for(('child', 'TOK2')))
    d = run(fp)
    assert d['count'] == 1
    assert ('/hubs/continueWatching', 'TOK2') in fp.calls and ('/hubs/continueWatching', 'OWNER') in fp.calls
    assert not any(t == 'OWNER' and p == '/library/metadata/10/allLeaves' for p, t in fp.calls)


def test_a_user_without_a_token_is_skipped_and_bad_xml_leaves_the_owner_list():
    xml = '<MediaContainer><SharedServer userID="9" username="x"/></MediaContainer>'
    assert ML.users_from_shared_servers(xml) == []
    assert ML.users_from_shared_servers('not xml') == []
    d = run(FakePlex({'OWNER': {'playlists': {'Move': [mov(1, 'Solo')]}}}, 'not xml'))
    assert d['count'] == 1 and d['users'] == 1


def test_one_failing_user_is_counted_not_fatal():
    world = {'OWNER': {'playlists': {'Move': [mov(1, 'Ok')]}}}  # TOK2 missing from the world -> KeyError
    d = run(FakePlex(world, xml_for(('broken', 'TOK2'))))
    assert d['count'] == 1 and d['errors'] == 2


def test_tokens_never_appear_in_the_output():
    world = {'OWNER': {'playlists': {'Move': [mov(1, 'A')]}}, 'TOKSECRET': {}}
    d = run(FakePlex(world, xml_for(('u', 'TOKSECRET'))))
    assert 'TOKSECRET' not in repr(d) and 'OWNER' not in repr(d)


aw = __import__('arr-webhook')


@pytest.fixture
def client(monkeypatch):
    aw.app.config['PROPAGATE_EXCEPTIONS'] = True
    monkeypatch.setattr(aw, 'PLEX_TOKEN', 'OWNER')
    fp = FakePlex({'OWNER': {'playlists': {'Move': [mov(1, 'Route Film', size=5 * GB)]}}})
    monkeypatch.setattr(aw, '_plex_for_move_list', fp.get)
    monkeypatch.setattr(aw, '_plextv_for_move_list', fp.tv)
    return aw.app.test_client()


def test_route_json_and_html(client):
    j = client.get('/move-list?format=json').get_json()
    assert j['count'] == 1 and j['total_gb'] == 5.0 and j['items'][0]['title'] == 'Route Film (2020)'
    page = client.get('/move-list')
    assert page.status_code == 200 and b'Route Film' in page.data and b'5.00 GB' in page.data


def test_route_is_get_only_and_needs_a_token(client, monkeypatch):
    assert client.post('/move-list').status_code == 405
    monkeypatch.setattr(aw, 'PLEX_TOKEN', '')
    assert client.get('/move-list?format=json').status_code == 400
