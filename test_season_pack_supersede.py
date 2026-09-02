"""Unit tests for the season-pack ⇄ singles supersede decision.

Regression cover for the bug where a season pack (e.g.
"The.Agency.2024.S02.1080p...-Riyadh") has no SxxExx token, so
dedup_via_sonarr()'s per-episode grouping never grouped it with the
individual-episode singles it replaced -- every single landed alone in its own
group of length 1, `multi` was empty, and the singles the pack superseded kept
seeding forever (real case: The Agency S02, 10 ...ETHEL singles still seeding
next to the imported ...Riyadh pack).

The decision is factored into the pure helper
select_singles_superseded_by_pack(torrent_names, keeper_ids, keeper_pack_seasons)
so it can be tested without touching live Sonarr/Deluge. keeper_ids and
keeper_pack_seasons are what _sonarr_keeper_pack_info() derives from Sonarr's
import history: keeper_ids = latest-import downloadId per episode;
keeper_pack_seasons = seasons whose current keeper is a season pack. Coverage is
history-derived on purpose -- the pack torrent itself need NOT still exist in
Deluge (the real The Agency case: pack imported and gone, singles still seeding).

Run: pytest test_season_pack_supersede.py   (or: python test_season_pack_supersede.py)
"""

select_singles_superseded_by_pack = __import__(
    'arr-webhook'
).select_singles_superseded_by_pack


# ── fixtures ────────────────────────────────────────────────────────────────

PACK_HASH = 'ba5cacd0573f' + '0' * 28  # the imported S02 pack keeper
PACK_NAME = 'The.Agency.2024.S02.1080p.WEB.H.265-Riyadh'


def _ten_singles():
    """10 individual-episode singles, S02E01..S02E10, ETHEL group."""
    out = {}
    for ep in range(1, 11):
        h = f'{ep:02d}' + 'a' * 38  # 40-char-ish fake infohash, unique per ep
        out[h] = f'The.Agency.2024.S02E{ep:02d}.1080p.WEB.h264-ETHEL'
    return out


# ── (a) 10 singles + keeper pack for S02 -> all 10 singles selected ──────────
# Models the real The Agency case: the pack torrent is NOT even in the Deluge
# set (Sonarr imported it and it left the sonarr label); coverage comes purely
# from Sonarr history. The pack is never among torrent_names here.

def test_pack_keeper_supersedes_all_singles():
    torrents = _ten_singles()
    keeper_ids = {PACK_HASH.lower()}       # Sonarr's keeper for every S02 ep is the pack
    keeper_pack_seasons = {2}              # S02's current keeper is a pack

    selected = set(
        select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons)
    )

    assert selected == set(_ten_singles().keys())          # all 10 singles
    assert len(selected) == 10


# ── (b) singles only, no pack -> nothing selected ────────────────────────────

def test_singles_only_no_pack_untouched():
    torrents = _ten_singles()
    # A single is the keeper for its own episode; no season has a pack keeper.
    keeper_ids = {next(iter(torrents)).lower()}
    keeper_pack_seasons = set()            # <- no pack keeps any season

    assert select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons) == []


# ── (c) pack present but NOT the keeper -> nothing selected ──────────────────
# The pack lost: the singles are the current keepers, so Sonarr reports no
# pack-kept season. (The pack torrent may still sit in Deluge; irrelevant.)

def test_pack_not_keeper_leaves_singles_alone():
    torrents = dict(_ten_singles())
    torrents[PACK_HASH] = PACK_NAME
    keeper_ids = {h.lower() for h in _ten_singles().keys()}
    keeper_pack_seasons = set()            # pack is not the keeper of any season

    assert select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons) == []


# ── extra guards ─────────────────────────────────────────────────────────────

def test_single_that_is_itself_keeper_not_selected():
    """S02 is a pack-kept season, but E05 was RE-IMPORTED as a repack AFTER the
    pack -> E05's single is the current keeper for its episode and must survive.
    The other 9 are still superseded. (Real analogue: South Park S28E05.)"""
    torrents = _ten_singles()
    e05 = next(h for h, n in _ten_singles().items() if 'S02E05' in n)
    keeper_ids = {PACK_HASH.lower(), e05.lower()}   # pack + the repack single
    keeper_pack_seasons = {2}

    selected = set(
        select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons)
    )

    assert e05 not in selected
    assert len(selected) == 9


def test_pack_never_returned_even_if_in_torrent_set():
    """If the pack torrent is present in the Deluge set, it must never be
    selected (packs are never superseded by this pass)."""
    torrents = dict(_ten_singles())
    torrents[PACK_HASH] = PACK_NAME
    keeper_ids = {PACK_HASH.lower()}
    keeper_pack_seasons = {2}

    selected = set(
        select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons)
    )

    assert PACK_HASH not in selected
    assert selected == set(_ten_singles().keys())


def test_other_season_singles_untouched():
    """A keeper pack for S02 must not touch S01 singles (season not pack-kept)."""
    torrents = dict(_ten_singles())              # S02 singles
    s01 = 'ff' * 20
    torrents[s01] = 'The.Agency.2024.S01E03.1080p.WEB.h264-ETHEL'
    keeper_ids = {PACK_HASH.lower()}
    keeper_pack_seasons = {2}                     # only S02 is pack-kept

    selected = set(
        select_singles_superseded_by_pack(torrents, keeper_ids, keeper_pack_seasons)
    )

    assert s01 not in selected
    assert selected == set(_ten_singles().keys())


if __name__ == '__main__':
    test_pack_keeper_supersedes_all_singles()
    test_singles_only_no_pack_untouched()
    test_pack_not_keeper_leaves_singles_alone()
    test_single_that_is_itself_keeper_not_selected()
    test_pack_never_returned_even_if_in_torrent_set()
    test_other_season_singles_untouched()
    print('all season-pack supersede tests passed')
