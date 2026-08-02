from shapely.geometry import box

from postcode2wad.geometry import LINE_HORIZON, SectorSpec, Thing, build_geometry
from postcode2wad.udmf import emit_textmap


def ground(size=1000, floor=0):
    return SectorSpec(polygon=box(0, 0, size, size), floor=floor, ceiling=4096, floor_tex="GRASS1")


def test_single_square_is_four_walls():
    geo = build_geometry([ground()])
    assert len(geo.sectors) == 1
    assert len(geo.vertices) == 4
    assert len(geo.linedefs) == 4
    # A lone square has no neighbours, so every line is one-sided.
    assert all("sideback" not in line for line in geo.linedefs)


def test_perimeter_gets_horizon_special():
    geo = build_geometry([ground()])
    assert all(line["special"] == LINE_HORIZON for line in geo.linedefs)

    plain = build_geometry([ground()], horizon_border=False)
    assert all("special" not in line for line in plain.linedefs)


def test_overlaid_building_splits_ground_and_shares_edges():
    specs = [
        ground(),
        SectorSpec(polygon=box(200, 200, 400, 400), floor=256, ceiling=4096, floor_tex="CEIL5_2"),
    ]
    geo = build_geometry(specs)

    # Ground plus building interior.
    assert len(geo.sectors) == 2
    floors = sorted(s["heightfloor"] for s in geo.sectors)
    assert floors == [0, 256]

    # The building's four edges are shared, so they must be two-sided and
    # emitted once — not duplicated as back-to-back one-sided walls.
    twosided = [line for line in geo.linedefs if line.get("twosided")]
    assert len(twosided) == 4
    for line in twosided:
        assert "sideback" in line
        assert "special" not in line  # horizon is perimeter-only


def test_building_wins_the_overlap():
    """Later specs take priority, so a building overrides the terrain beneath it."""
    specs = [
        ground(floor=0),
        SectorSpec(polygon=box(100, 100, 300, 300), floor=999, ceiling=4096, floor_tex="CEIL5_2"),
    ]
    geo = build_geometry(specs)
    assert any(s["heightfloor"] == 999 for s in geo.sectors)


def test_hole_becomes_its_own_sector():
    from shapely.geometry import Polygon

    ring = Polygon(
        shell=box(200, 200, 600, 600).exterior.coords,
        holes=[box(300, 300, 500, 500).exterior.coords],
    )
    specs = [ground(), SectorSpec(polygon=ring, floor=320, ceiling=4096, floor_tex="CEIL5_2")]
    geo = build_geometry(specs)

    # ground, the ring itself, and the courtyard inside it
    assert len(geo.sectors) == 3


def test_textmap_round_trip_is_wellformed():
    geo = build_geometry([ground()], things=[Thing(x=500, y=500, type=1, angle=90)])
    text = emit_textmap(geo)

    assert text.startswith('namespace = "zdoom";')
    assert text.count("vertex {") == 4
    assert text.count("linedef {") == 4
    assert text.count("sector {") == 1
    assert "type = 1;" in text
    # Every block must be closed and every assignment terminated.
    assert text.count("{") == text.count("}")
