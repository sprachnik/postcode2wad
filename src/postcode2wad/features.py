"""Turn OSM features into map-unit polygons.

Everything here is about surviving real data. OSM footprints self-intersect,
double back on themselves, and repeat vertices; ways run off the edge of the
tile; relations arrive as rings that may or may not close. `make_valid` plus a
clip to the tile square handles nearly all of it, and anything still degenerate
after that gets dropped rather than emitted as broken geometry.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from . import UNITS_PER_METRE
from .sources.overpass import Feature
from .tiles import Tile

#: Total carriageway width in metres by OSM highway class. Deliberately
#: generous — a 6m residential street reads as far too narrow in first person.
ROAD_WIDTHS = {
    "motorway": 16.0,
    "motorway_link": 8.0,
    "trunk": 14.0,
    "trunk_link": 8.0,
    "primary": 11.0,
    "primary_link": 7.0,
    "secondary": 10.0,
    "secondary_link": 7.0,
    "tertiary": 8.5,
    "tertiary_link": 6.0,
    "residential": 7.0,
    "unclassified": 6.5,
    "living_street": 6.0,
    "service": 4.5,
    "pedestrian": 5.0,
    "track": 3.5,
    "footway": 2.0,
    "path": 1.8,
    "cycleway": 2.5,
    "steps": 2.0,
    "bridleway": 2.0,
}

#: Fallback storey heights when a building has no height tag at all.
DEFAULT_HEIGHTS_M = {
    "church": 14.0,
    "cathedral": 20.0,
    "chapel": 10.0,
    "school": 9.0,
    "hospital": 14.0,
    "industrial": 9.0,
    "warehouse": 9.0,
    "retail": 7.5,
    "commercial": 10.0,
    "apartments": 13.0,
    "hotel": 13.0,
    "garage": 2.6,
    "garages": 2.6,
    "shed": 2.4,
    "hut": 2.4,
    "greenhouse": 2.6,
    "bungalow": 4.5,
    "terrace": 7.0,
    "house": 6.5,
    "detached": 7.0,
    "semidetached_house": 6.5,
}
DEFAULT_BUILDING_HEIGHT_M = 7.0
STOREY_HEIGHT_M = 3.1


@dataclass
class Shape:
    """A polygon in map units, with the OSM tags that produced it."""

    polygon: Polygon
    tags: dict[str, str]
    height_m: float = 0.0
    #: The OSM way/relation it came from. Used to pick a stable façade material
    #: per building, so a street varies but regenerating the tile does not.
    osm_id: int = 0
    #: The line this shape was buffered out from, in map units, where it had
    #: one. A road's centreline is the axis its surface is engineered along, so
    #: it is the parameter the height should be a function of — see
    #: `build._road_surface`. Kept unclipped and unsimplified: it is a
    #: parameterisation, not geometry to be drawn.
    centre: LineString | None = None

    @property
    def name(self) -> str:
        return self.tags.get("name", "")


def parse_length(value: str | None) -> float | None:
    """OSM height/width values: '12', '12 m', '12.5m', or feet as '40\\''."""
    if not value:
        return None
    text = value.strip().lower().replace(",", ".")
    try:
        if text.endswith("'"):
            return float(text[:-1]) * 0.3048
        if text.endswith("ft"):
            return float(text[:-2].strip()) * 0.3048
        if text.endswith("m"):
            text = text[:-1].strip()
        return float(text)
    except ValueError:
        return None


def building_height(tags: dict[str, str]) -> float:
    """Best available height in metres, in descending order of trust."""
    explicit = parse_length(tags.get("height"))
    if explicit and explicit > 0:
        return explicit

    levels = tags.get("building:levels")
    if levels:
        try:
            # +1 so the roof clears the top floor's ceiling.
            return float(levels.replace(",", ".")) * STOREY_HEIGHT_M + 1.0
        except ValueError:
            pass

    kind = tags.get("building", "yes")
    if kind in DEFAULT_HEIGHTS_M:
        return DEFAULT_HEIGHTS_M[kind]
    if tags.get("amenity") == "place_of_worship":
        return DEFAULT_HEIGHTS_M["church"]
    return DEFAULT_BUILDING_HEIGHT_M


def _to_map_polygon(feature: Feature, tile: Tile) -> Polygon | None:
    points = [tile.to_map_lonlat(lon, lat) for lon, lat in feature.coords]
    if len(points) < 4:
        return None
    try:
        poly = Polygon(points)
    except (ValueError, TypeError):  # ways that don't form a ring at all
        return None
    return poly


def _clean(geom: BaseGeometry, clip: Polygon, simplify_units: float) -> list[Polygon]:
    """Make valid, clip to the tile, simplify, and split multiparts out."""
    if geom.is_empty:
        return []
    if not geom.is_valid:
        geom = geom.buffer(0)  # cheapest reliable fix for self-intersections
    if geom.is_empty:
        return []

    geom = geom.intersection(clip)
    if geom.is_empty:
        return []
    if simplify_units:
        geom = geom.simplify(simplify_units, preserve_topology=True)

    parts: list[Polygon] = []
    if isinstance(geom, Polygon):
        parts = [geom]
    elif isinstance(geom, MultiPolygon):
        parts = list(geom.geoms)
    else:  # GeometryCollection from a clip that grazed the edge
        parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]

    return [p for p in parts if not p.is_empty and p.is_valid]


def tile_clip(tile: Tile, inset_units: float = 0.0) -> Polygon:
    size = tile.size_units
    return box(inset_units, inset_units, size - inset_units, size - inset_units)


def buildings_to_shapes(
    features: list[Feature],
    tile: Tile,
    min_area_m2: float = 8.0,
    simplify_m: float = 0.4,
) -> list[Shape]:
    clip = tile_clip(tile)
    simplify_units = simplify_m * UNITS_PER_METRE
    min_area_units = min_area_m2 * UNITS_PER_METRE * UNITS_PER_METRE

    shapes: list[Shape] = []
    for feature in features:
        poly = _to_map_polygon(feature, tile)
        if poly is None:
            continue
        for part in _clean(poly, clip, simplify_units):
            if part.area < min_area_units:
                continue
            shapes.append(
                Shape(
                    polygon=part,
                    tags=feature.tags,
                    height_m=building_height(feature.tags),
                    osm_id=feature.osm_id,
                )
            )
    return shapes


def roads_to_shapes(
    features: list[Feature],
    tile: Tile,
    simplify_m: float = 0.5,
) -> list[Shape]:
    """Buffer highway centrelines out to their tagged width."""
    clip = tile_clip(tile)
    simplify_units = simplify_m * UNITS_PER_METRE

    shapes: list[Shape] = []
    for feature in features:
        kind = feature.tags.get("highway", "")
        if kind in ("proposed", "construction", "raceway"):
            continue

        width_m = parse_length(feature.tags.get("width")) or ROAD_WIDTHS.get(kind)
        if not width_m:
            continue

        points = [tile.to_map_lonlat(lon, lat) for lon, lat in feature.coords]
        # Deduplicate consecutive points; a zero-length segment kills buffer().
        deduped = [points[0]]
        for p in points[1:]:
            if p != deduped[-1]:
                deduped.append(p)
        if len(deduped) < 2:
            continue

        line = LineString(deduped)
        corridor = line.buffer(
            width_m * UNITS_PER_METRE / 2.0,
            cap_style=2,  # flat caps, so roads butt up at junctions
            join_style=2,  # mitred corners, far fewer vertices than round
        )
        for part in _clean(corridor, clip, simplify_units):
            shapes.append(Shape(polygon=part, tags=feature.tags, centre=line))
    return shapes


def _map_line(feature: Feature, tile: Tile) -> LineString | None:
    """A feature's coordinates as a map-unit LineString, or None if degenerate."""
    points = [tile.to_map_lonlat(lon, lat) for lon, lat in feature.coords]
    deduped = [points[0]] if points else []
    for point in points[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    return LineString(deduped) if len(deduped) >= 2 else None


def _leftward(line: LineString, point: Point) -> float:
    """Signed area of the turn from the line's local direction to `point`.

    Positive means the point lies to the left of the way's direction of travel.
    The tangent is sampled either side of the projected position rather than
    taken from the nearest vertex, so a point off a smooth curve gets the local
    heading instead of whichever segment happened to end closest.
    """
    along = line.project(point)
    reach = min(8.0, line.length / 2.0)
    a = line.interpolate(max(0.0, along - reach))
    b = line.interpolate(min(line.length, along + reach))
    return (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)


def coastline_lines(features: list[Feature], tile: Tile) -> list[LineString]:
    """Coastline fragments clipped to the tile, in map units."""
    clip = tile_clip(tile)
    lines: list[LineString] = []
    for feature in features:
        line = _map_line(feature, tile)
        if line is None:
            continue
        part = line.intersection(clip)
        if part.is_empty:
            continue
        for geom in getattr(part, "geoms", [part]):
            if isinstance(geom, LineString) and geom.length > 0:
                lines.append(geom)
    return lines


def beach_from_coastline(
    features: list[Feature],
    tile: Tile,
    width_m: float = 30.0,
    simplify_m: float = 1.0,
) -> list[Shape]:
    """A sand strip on the landward side of the coastline.

    OSM does tag beaches as `natural=beach` areas, and those are now fetched and
    handled like any other land cover — but coverage is patchy, and the
    Birchington seafront has none, so a coastal tile still came out as grass
    running straight into the sea.

    Generating the strip instead means every coastal tile gets a shoreline.
    It is deliberately *not* clipped to low ground here: the caller intersects
    it with the contour bands it wants, which is also what keeps the sand
    stepping with the terrain instead of ironing the foreshore flat.
    """
    clip = tile_clip(tile)
    lines = coastline_lines(features, tile)
    if not lines:
        return []

    strip = unary_union(
        [line.buffer(width_m * UNITS_PER_METRE, cap_style=2) for line in lines]
    )
    # Keep the land half only. The sea already has its own flat, and sand laid
    # over it would float on the surface.
    sea = unary_union([shape.polygon for shape in sea_from_coastline(features, tile)])
    if not sea.is_empty:
        strip = strip.difference(sea)
    if strip.is_empty:
        return []

    simplify_units = simplify_m * UNITS_PER_METRE
    return [
        Shape(polygon=part, tags={"natural": "beach"})
        for part in _clean(strip, clip, simplify_units)
    ]


def sea_from_coastline(
    features: list[Feature],
    tile: Tile,
    simplify_m: float = 1.0,
) -> list[Shape]:
    """Close `natural=coastline` against the tile edge to produce sea polygons.

    Coastline is the one piece of OSM water that is not an area. The coast of
    Great Britain is a single directed way, and the only thing marking which
    side is wet is a *winding convention*: land lies to the LEFT of the
    direction of travel, sea to the right. There is no tag to read.

    So the sea has to be constructed: node the coastline fragments against the
    tile square, polygonize, and keep the faces that fall on the seaward side.
    That is the same planar-arrangement trick `geometry.py` uses on buildings,
    for the same reason — it is the only approach that copes with a coast that
    enters and leaves the tile several times, doubles back into an inlet, or
    encloses an island.

    Known gap: a tile entirely at sea contains no coastline at all and therefore
    generates as dry land. Deciding that case needs a land polygon from outside
    the tile, which this does not fetch.
    """
    clip = tile_clip(tile)
    lines = coastline_lines(features, tile)
    if not lines:
        return []

    noded = unary_union([LineString(clip.exterior.coords), *lines])
    simplify_units = simplify_m * UNITS_PER_METRE

    shapes: list[Shape] = []
    for face in polygonize(noded):
        if face.is_empty or not face.is_valid:
            continue
        probe = face.representative_point()
        nearest = min(lines, key=lambda line: line.distance(probe))
        if _leftward(nearest, probe) >= 0:
            continue  # land side
        for part in _clean(face, clip, simplify_units):
            shapes.append(Shape(polygon=part, tags={"natural": "coastline"}))
    return shapes


#: (width, height) in metres by OSM `barrier` value.
#:
#: Anything absent is skipped on purpose. Most other barrier values are nodes
#: rather than ways (bollard, gate, stile, cattle_grid), and several are
#: *negative* features — a ditch or a sunken kerb extruded upward as a block
#: would be actively wrong.
BARRIERS = {
    "hedge": (0.9, 1.7),
    "hedge_bank": (1.2, 1.8),
    "fence": (0.15, 1.3),
    "wall": (0.35, 1.8),
    "dry_stone_wall": (0.5, 1.4),
    "retaining_wall": (0.4, 1.0),
    "city_wall": (0.8, 4.0),
    "guard_rail": (0.1, 0.8),
    "handrail": (0.08, 1.0),
}


def barriers_to_shapes(
    features: list[Feature],
    tile: Tile,
    simplify_m: float = 0.5,
) -> list[Shape]:
    """Hedges, fences and garden walls as thin extruded corridors.

    These matter out of all proportion to their size. A British street is
    legible mostly through its property boundaries — take the hedges away and a
    row of houses becomes free-standing blocks in a shared field, which is
    exactly how the first tiles read.

    Like coastline these are open ways, so they are buffered rather than filled,
    with flat caps so a hedge meeting a wall butts up instead of overshooting.
    """
    clip = tile_clip(tile)
    simplify_units = simplify_m * UNITS_PER_METRE

    shapes: list[Shape] = []
    for feature in features:
        size = BARRIERS.get(feature.tags.get("barrier", ""))
        if size is None:
            continue
        width_m, height_m = size

        line = _map_line(feature, tile)
        if line is None:
            continue

        corridor = line.buffer(
            max(width_m, 0.25) * UNITS_PER_METRE / 2.0,
            cap_style=2,
            join_style=2,
        )
        for part in _clean(corridor, clip, simplify_units):
            shapes.append(Shape(polygon=part, tags=feature.tags, height_m=height_m))
    return shapes


def areas_to_shapes(
    features: list[Feature],
    tile: Tile,
    simplify_m: float = 1.0,
    min_area_m2: float = 0.0,
) -> list[Shape]:
    """Closed ways to map polygons, tags carried through."""
    clip = tile_clip(tile)
    simplify_units = simplify_m * UNITS_PER_METRE
    min_area_units = min_area_m2 * UNITS_PER_METRE * UNITS_PER_METRE

    shapes: list[Shape] = []
    for feature in features:
        poly = _to_map_polygon(feature, tile)
        if poly is None:
            continue
        for part in _clean(poly, clip, simplify_units):
            if part.area < min_area_units:
                continue
            shapes.append(Shape(polygon=part, tags=feature.tags))
    return shapes


def water_to_shapes(features: list[Feature], tile: Tile, simplify_m: float = 1.0) -> list[Shape]:
    return areas_to_shapes(features, tile, simplify_m)


def landuse_to_shapes(
    features: list[Feature],
    tile: Tile,
    simplify_m: float = 1.5,
    min_area_m2: float = 40.0,
) -> list[Shape]:
    """Land-cover parcels: fields, parks, car parks, gardens.

    Simplified harder than buildings and with a bigger floor on area, because
    the exact outline of a field is not load-bearing and every vertex here has
    to be paid for in the planar arrangement.
    """
    return areas_to_shapes(features, tile, simplify_m, min_area_m2)
