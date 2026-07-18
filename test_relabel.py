"""Self-check for relabel_download_to_base — the tag flip-back decision that
went silently wrong (a finished upgrade kept its '-upgrade' tag). No framework.
Run: python test_relabel.py"""


class _Resp:
    def __init__(self, result):
        self._result = result

    def json(self):
        return {'result': self._result}


def _stub(aw, result):
    """Point the module's Deluge calls at a canned status and record label writes."""
    aw.session = type('S', (), {'post': staticmethod(lambda *a, **k: _Resp(result))})()
    writes = []
    aw.set_torrent_label = lambda h, label: writes.append((h, label))
    return writes


def run():
    aw = __import__('arr-webhook')

    # torrent missing from Deluge -> not_found, no label write
    writes = _stub(aw, None)
    assert aw.relabel_download_to_base('abc', 'Sonarr') == ('not_found', None)
    assert writes == []

    # currently upgrade-labeled -> flipped to base, exactly one label write
    writes = _stub(aw, {'label': 'sonarr-upgrade', 'name': 'Show S01E01'})
    assert aw.relabel_download_to_base('abc', 'Sonarr') == ('flipped', 'Show S01E01')
    assert writes == [('abc', 'sonarr')], writes

    # already base/other label -> no-op, no write
    writes = _stub(aw, {'label': 'sonarr', 'name': 'Show'})
    assert aw.relabel_download_to_base('abc', 'Sonarr') == ('already', 'Show')
    assert writes == []

    # Radarr side uses the radarr base label
    writes = _stub(aw, {'label': 'radarr-upgrade', 'name': 'Movie 2022'})
    assert aw.relabel_download_to_base('def', 'Radarr') == ('flipped', 'Movie 2022')
    assert writes == [('def', 'radarr')], writes

    print('test_relabel: all assertions passed')


if __name__ == '__main__':
    run()
