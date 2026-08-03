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


def test_interior_orphan_lines_are_walls_not_horizons():
    """A one-sided line away from the tile edge must be solid, not a horizon.

    `min_face_area` discards sliver faces, orphaning the edges they backed onto.
    Those edges are one-sided but they are *not* the map boundary, and giving
    them Line_Horizon renders each as a window onto an infinite flat plane —
    tears in the scenery, dotted all over a real tile.
    """
    from shapely.geometry import Polygon

    from postcode2wad.geometry import LINE_HORIZON, SectorSpec, build_geometry

    ground = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    # A spike thin enough that the arrangement face under it falls below
    # min_face_area and gets dropped, orphaning the edges around it.
    spike = Polygon([(500, 500), (501, 500), (500.5, 501)])

    geo = build_geometry(
        [
            SectorSpec(polygon=ground, floor=0, ceiling=4096, floor_tex="GRASS1"),
            SectorSpec(polygon=spike, floor=64, ceiling=4096, floor_tex="GRASS1"),
        ],
        min_face_area=64.0,
    )

    for line in geo.linedefs:
        if "sideback" in line and line["sideback"] is not None:
            continue
        if line.get("special") != LINE_HORIZON:
            continue
        v1 = geo.vertices[line["v1"]]
        v2 = geo.vertices[line["v2"]]
        on_edge = (
            (v1[0] == v2[0] == 0)
            or (v1[0] == v2[0] == 1000)
            or (v1[1] == v2[1] == 0)
            or (v1[1] == v2[1] == 1000)
        )
        assert on_edge, f"horizon special on an interior line {v1}->{v2}"
