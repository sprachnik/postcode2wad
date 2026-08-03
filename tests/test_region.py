"""Region packs: tile layout, map naming, and the generated transition script."""

import pytest

from postcode2wad.hud import build_zscript
from postcode2wad.pk3 import build_region_mapinfo
from postcode2wad.region import MAX_MAPS, map_name
from postcode2wad.tiles import Tile


def grid(radius: int, size_m: int = 400) -> list[Tile]:
    centre = Tile(1574, 423, size_m)
    return [
        centre.neighbour(dx, dy)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
    ]


def test_map_names_are_sequential_from_one():
    assert map_name(0) == "MAP01"
    assert map_name(8) == "MAP09"
    assert map_name(11) == "MAP12"


def test_zscript_rejects_a_mismatched_map_order():
    """The script derives map names from the array index, so the order matters.

    Nothing downstream would notice a shuffle: the pack would load, and walking
    east would simply put you in the wrong place.
    """
    tiles = grid(1)
    names = [map_name(i) for i in range(len(tiles))]
    names[3], names[4] = names[4], names[3]
    with pytest.raises(ValueError, match="expected"):
        build_zscript(tiles, names)


def test_zscript_tile_table_matches_the_grid():
    tiles = grid(1)
    names = [map_name(i) for i in range(len(tiles))]
    script = build_zscript(tiles, names)

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


def test_region_mapinfo_declares_every_map_once():
    tiles = grid(1)
    names = [map_name(i) for i in range(len(tiles))]
    mapinfo = build_region_mapinfo(
        [(n, f"Tile {n}") for n in names], event_handler="PostcodeHUD"
    )
    for name in names:
        assert f'map {name} "Tile {name}"' in mapinfo
    assert mapinfo.count("AddEventHandlers") == 1


def test_radius_beyond_the_mapxx_ceiling_is_refused():
    """MAPxx runs out at 99, and a silently truncated world is worse than an error."""
    from postcode2wad.region import build_region
    from postcode2wad.sources.postcodes import Place

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
        build_region(place, radius=5)
