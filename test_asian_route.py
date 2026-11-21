"""route_series_to_asian: a CJK series moves to the Asian TV root only when an Asian TV
requester's Overseerr tag is on it. Language alone never moves a series. Sonarr is faked."""
import pytest

aw = __import__('arr-webhook')

TAGS = [{'id': 1, 'label': '5 - viewer'}, {'id': 2, 'label': '1 - admin'}, {'id': 3, 'label': 'upgrade'},
        {'id': 4, 'label': 'x - 5'}]


class Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


@pytest.fixture
def sonarr(monkeypatch):
    box = {'series': {}, 'puts': [], 'gets': []}

    def get(url, headers=None, timeout=None):
        box['gets'].append(url)
        if url.endswith('/api/v3/tag'):
            return Resp(TAGS)
        return Resp(box['series'])

    def put(url, headers=None, timeout=None, json=None):
        box['puts'].append(json)
        return Resp({})

    monkeypatch.setattr(aw.requests, 'get', get)
    monkeypatch.setattr(aw.requests, 'put', put)
    monkeypatch.setattr(aw, 'ASIAN_TV_REQUESTERS', {'5'})
    return box


def series(lang, tags, root='/data/Media/TV Shows'):
    return {'id': 42, 'originalLanguage': {'name': lang}, 'tags': tags, 'rootFolderPath': root}


def test_requester_cjk_series_moves(sonarr):
    sonarr['series'] = series('Korean', [1])
    assert aw.route_series_to_asian({'series': {'id': 42}}) == aw.ASIAN_TV_ROOT
    assert sonarr['puts'] == [{'seriesIds': [42], 'rootFolderPath': aw.ASIAN_TV_ROOT, 'moveFiles': True}]


@pytest.mark.parametrize('tags', [[], [2], [3], [4], [2, 3]])
def test_cjk_without_requester_tag_stays(sonarr, tags):
    sonarr['series'] = series('Japanese', tags)
    assert aw.route_series_to_asian({'series': {'id': 42}}) is None
    assert sonarr['puts'] == []


def test_requester_english_series_stays(sonarr):
    sonarr['series'] = series('English', [1])
    assert aw.route_series_to_asian({'series': {'id': 42}}) is None
    assert sonarr['puts'] == []
    assert not any(u.endswith('/tag') for u in sonarr['gets'])


def test_already_in_asian_root_not_moved(sonarr):
    sonarr['series'] = series('Chinese', [1], root=aw.ASIAN_TV_ROOT)
    assert aw.route_series_to_asian({'series': {'id': 42}}) is None
    assert sonarr['puts'] == []


def test_empty_requester_list_turns_routing_off(sonarr, monkeypatch):
    monkeypatch.setattr(aw, 'ASIAN_TV_REQUESTERS', set())
    sonarr['series'] = series('Korean', [1])
    assert aw.route_series_to_asian({'series': {'id': 42}}) is None
    assert sonarr['gets'] == [] and sonarr['puts'] == []


def test_sonarr_error_is_swallowed(sonarr, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('sonarr down')
    monkeypatch.setattr(aw.requests, 'get', boom)
    assert aw.route_series_to_asian({'series': {'id': 42}}) is None


def test_missing_series_id(sonarr):
    assert aw.route_series_to_asian({}) is None
    assert sonarr['gets'] == []
