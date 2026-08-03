"""Region packs: tile layout, map naming, and the generated transition script."""

import pytest

from postcode2wad.hud import build_zscript
from postcode2wad.pk3 import build_region_mapinfo
from postcode2wad.region import MAX_MAPS, map_name
from postcode2wad.tiles import Tile


def names(count: int) -> list[str]:
    return [map_name(i, count) for i in range(count)]


def grid(radius: int, size_m: int = 400) -> list[Tile]:
    centre = Tile(1574, 423, size_m)
    return [
        centre.neighbour(dx, dy)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
    ]


def test_small_packs_keep_the_conventional_mapxx_names():
    """`+map MAP01` is the documented command and what the new-game menu loads."""
    assert map_name(0, 9) == "MAP01"
    assert map_name(8, 9) == "MAP09"
    assert map_name(98, 99) == "MAP99"


def test_packs_past_the_mapxx_limit_switch_scheme():
    """Two digits run out at 99; the wider scheme keeps going."""
    assert map_name(0, 121) == "M0001"
    assert map_name(120, 121) == "M0121"


def test_zscript_rejects_a_mismatched_map_order():
    """The script derives map names from the array index, so the order matters.

    Nothing downstream would notice a shuffle: the pack would load, and walking
    east would simply put you in the wrong place.
    """
    tiles = grid(1)
    shuffled = names(len(tiles))
    shuffled[3], shuffled[4] = shuffled[4], shuffled[3]
    with pytest.raises(ValueError, match="expected"):
        build_zscript(tiles, shuffled)


def test_zscript_tile_table_matches_the_grid():
    tiles = grid(1)
    script = build_zscript(tiles, names(len(tiles)))

    assert "const TILE_COUNT  = 9;" in script
    # Every tile's grid index must appear, or the neighbour lookup cannot
    # resolve and the edges silently become dead ends.
    assert f"{{ {', '.join(str(t.ix) for t in tiles)} }}" in script
    assert f"{{ {', '.join(str(t.iy) for t in tiles)} }}" in script


def test_every_interior_tile_has_four_neighbours_in_the_table():
    """A 3x3 block must leave exactly one tile with neighbours on all sides."""
    tiles = grid(1)
    coords = {(t.ix, t.iy) for t in tiles}
    fully_surrounded = [
        t
        for t in tiles
        if all(
            (t.ix + dx, t.iy + dy) in coords for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
    ]
    assert len(fully_surrounded) == 1


def test_single_tile_is_just_a_one_entry_region():
    """The single-tile CLI path uses the same generator, so it must not special-case."""
    script = build_zscript([Tile(1574, 423, 400)], ["MAP01"])
    assert "const TILE_COUNT  = 1;" in script


def test_zscript_parses_whichever_naming_scheme_the_pack_uses():
    """The script slices the map name by fixed offsets, so they must match."""
    small = build_zscript(grid(1), names(9))
    assert 'mapname.Mid(3, 2)' in small
    assert 'String.Format("MAP%02d"' in small

    tiles = grid(5)                      # 11x11 = 121 maps, past the MAPxx limit
    big = build_zscript(tiles, names(len(tiles)))
    assert 'mapname.Mid(1, 4)' in big
    assert 'String.Format("M%04d"' in big


def test_region_mapinfo_declares_every_map_once():
    tiles = grid(1)
    pack = names(len(tiles))
    mapinfo = build_region_mapinfo(
        [(n, f"Tile {n}") for n in pack], event_handler="PostcodeHUD"
    )
    for name in pack:
        assert f'map {name} "Tile {name}"' in mapinfo
    assert mapinfo.count("AddEventHandlers") == 1


def test_radius_beyond_the_naming_ceiling_is_refused():
    """A silently truncated world is worse than an error.

    The radius here has to actually exceed MAX_MAPS. When that ceiling was
    raised from 99 to 9,999 this test kept its old radius of 5, which no longer
    trips the guard — so instead of raising, it fell through and started
    generating 121 real tiles over the network from inside the test suite.
    """
    from postcode2wad.region import build_region
    from postcode2wad.sources.postcodes import Place

    side = int(MAX_MAPS**0.5) + 2
    radius = (side - 1) // 2 + 1
    assert (radius * 2 + 1) ** 2 > MAX_MAPS, "radius must exceed the ceiling to test it"

    place = Place(
        postcode="CT1 2EH",
        easting=627500,
        northing=167500,
        lat=51.36099,
        lon=1.26650,
        name="Birchington",
        query="CT1 2EH",
    )
    with pytest.raises(ValueError, match=str(MAX_MAPS)):
        build_region(place, radius=radius)
