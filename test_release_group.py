"""Self-check for extract_release_group — the same-group-vs-different-group
gate that decides whether a proper/repack deletes the old torrent outright
or only supersedes it. No framework. Run: python test_release_group.py"""


def run():
    aw = __import__('arr-webhook')

    # plain trailing group
    assert aw.extract_release_group('Show.S01E01.1080p.WEB.H264-GROUP') == 'group'

    # tracker/site tag in brackets after the group is stripped first
    assert aw.extract_release_group('Show.S01E01.1080p.WEB.H264-GROUP[TGx]') == 'group'
    assert aw.extract_release_group('Show.S01E01.1080p.WEB.H264-GROUP[eztv][rartv]') == 'group'

    # no hyphenated group present -> unknown
    assert aw.extract_release_group('Show.S01E01.1080p.WEB.H264') is None
    assert aw.extract_release_group('') is None
    assert aw.extract_release_group(None) is None

    # comparison is case-insensitive since callers lowercase before compare
    assert aw.extract_release_group('Movie.2022.1080p-GrOuP') == 'group'

    print('test_release_group: all assertions passed')


if __name__ == '__main__':
    run()
