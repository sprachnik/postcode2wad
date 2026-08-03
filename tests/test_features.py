"""Feature conversion, mostly the parts where real data disagrees with the code."""

from shapely.geometry import Point

from postcode2wad import UNITS_PER_METRE
from postcode2wad.features import sea_from_coastline
from postcode2wad.sources.overpass import Feature
from postcode2wad.tiles import Tile, osgb_to_lonlat

TILE = Tile(1574, 423, 400)


def coastline_way(points_osgb):
    """A coastline Feature from OSGB metres, so tests can think in metres."""
    return Feature(
        osm_id=1,
        kind="way",
        coords=[osgb_to_lonlat(e, n) for e, n in points_osgb],
        tags={"natural": "coastline"},
    )


def test_sea_is_on_the_right_of_the_way():
    """OSM's coastline convention: land to the left, sea to the right.

    Nothing in the tags says which side is wet — get the winding backwards and
    you flood the village and drain the Channel, with no error to tell you.
    """
    east, north = TILE.origin
    # A way running due north up the middle of the tile. Travelling north, west
    # is on the left (land) and east is on the right (sea).
    way = coastline_way([(east + 200, north), (east + 200, north + 400)])

    sea = sea_from_coastline([way], TILE)
    assert sea, "a coastline crossing the tile must produce sea"

    union = sea[0].polygon
    for shape in sea[1:]:
        union = union.union(shape.polygon)

    half = 200 * UNITS_PER_METRE
    quarter = 100 * UNITS_PER_METRE
    assert union.contains_properly(Point(half + quarter, half))  # east: sea
    assert not union.intersects(Point(quarter, half))  # west: land


def test_reversing_the_way_swaps_land_and_sea():
    east, north = TILE.origin
    northward = coastline_way([(east + 200, north), (east + 200, north + 400)])
    southward = coastline_way([(east + 200, north + 400), (east + 200, north)])

    north_area = sum(s.polygon.area for s in sea_from_coastline([northward], TILE))
    south_area = sum(s.polygon.area for s in sea_from_coastline([southward], TILE))

    total = float(TILE.size_units) ** 2
    assert north_area > 0 and south_area > 0
    assert abs(north_area + south_area - total) < total * 0.01


def test_no_coastline_means_no_sea():
    """An inland tile must not sprout an ocean."""
    assert sea_from_coastline([], TILE) == []


def test_hud_georeference_matches_a_real_transform():
    """The HUD's linear fit must agree with pyproj across the whole tile.

    The HUD cannot run OSTN15 — that is a 1.5 MB shift grid — so it carries a
    plane fitted to the true transform instead. If that plane is wrong the
    readout is confidently, silently wrong, which is worse than absent.
    """
    from postcode2wad import UNITS_PER_METRE
    from postcode2wad.hud import georeference
    from postcode2wad.tiles import osgb_to_lonlat

    geo = georeference(TILE)
    east, north = TILE.origin

    worst = 0.0
    for fx in (0.0, 0.25, 0.5, 0.75, 1.0):
        for fy in (0.0, 0.25, 0.5, 0.75, 1.0):
            x = fx * TILE.size_units
            y = fy * TILE.size_units
            lat = geo["lat0"] + geo["lat_per_x"] * x + geo["lat_per_y"] * y
            lon = geo["lon0"] + geo["lon_per_x"] * x + geo["lon_per_y"] * y

            true_lon, true_lat = osgb_to_lonlat(
                east + x / UNITS_PER_METRE, north + y / UNITS_PER_METRE
            )
            # Degrees to metres, near enough at this latitude.
            dy = (lat - true_lat) * 111_320.0
            dx = (lon - true_lon) * 111_320.0 * 0.62
            worst = max(worst, (dx * dx + dy * dy) ** 0.5)

    assert worst < 0.5, f"linear georeference is off by {worst:.2f}m"
