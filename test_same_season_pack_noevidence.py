"""Same-season pack-vs-pack: with no import evidence (blank downloadIds), never pick a
winner by codec/hash alone -- the winner could be a pack Sonarr never imported."""
aw = __import__('arr-webhook')

HONE = '095b54b29d22c256784cc7f7ab7185f9a86f0b46'
ELITE = 'e9e6fedb0000000000000000000000000000aaaa'
NAMES = {HONE: 'Slow Horses (2022) S05 (1080p ATVP WEB-DL H265 SDR DDP Atmos 5.1 English - HONE)',
         ELITE: 'Slow.Horses.S05.1080p.x265-ELiTE'}


def test_no_evidence_supersedes_nothing():
    assert aw.select_same_season_pack_losers(129, NAMES, set(), set()) == []


def test_imported_pack_still_wins():
    assert aw.select_same_season_pack_losers(129, NAMES, {HONE}, {HONE}) == [ELITE]
    assert aw.select_same_season_pack_losers(129, NAMES, set(), {ELITE}) == [HONE]
