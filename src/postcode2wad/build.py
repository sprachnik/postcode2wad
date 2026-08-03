"""M1 — assemble a real place into a playable map.

Order matters here and is the whole trick: `build_geometry` resolves overlaps
last-wins, so the spec list runs terrain -> water -> roads -> buildings. A
building laid over a road overrides it; a road laid over terrain overrides that.

Heights are absolute against ordnance datum rather than relative to the tile, so
a 21m contour lands at the same map height in every tile that contains one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, Polygon

from . import UNITS_PER_METRE, textures
from .features import buildings_to_shapes, roads_to_shapes, tile_clip, water_to_shapes
from .geometry import MapGeometry, SectorSpec, Thing, build_geometry
from .sources import lidar, overpass
from .sources.postcodes import Place
from .terrain import contour_bands, ground_height_m, object_height_m
from .tiles import Tile

#: Headroom above the highest ground before the sky plane.
SKY_CLEARANCE = 4096

#: Roads sit a kerb's depth below the surrounding ground.
KERB_UNITS = 8
WATER_DEPTH_UNITS = 20

#: Sector light levels. The old default of 192 everywhere is interior gloom —
#: correct for a bunker, wrong for a Tuesday afternoon in Kent. Depth now comes
#: from MAPINFO fog (see pk3.py) rather than from darkening, so these can sit
#: high without flattening the scene. The small spread between them is not
#: physical, it just stops every surface in frame having identical value.
DAYLIGHT = 208
ROOF_LIGHT = 216  # unshaded, facing straight up
ROAD_LIGHT = 200  # in the lee of buildings and kerbs more often than not
WATER_LIGHT = 216

PLAYER_START = 1

PAVED_FOOTWAYS = {"footway", "path", "pedestrian", "steps", "cycleway", "bridleway"}


@dataclass
class BuildStats:
    buildings: int = 0
    roads: int = 0
    water: int = 0
    terrain_bands: int = 0
    sectors: int = 0
    linedefs: int = 0
    elevation_range_m: tuple[float, float] = (0.0, 0.0)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        low, high = self.elevation_range_m
        return (
            f"{self.buildings} buildings, {self.roads} road pieces, {self.water} water, "
            f"{self.terrain_bands} terrain bands, terrain {low:.1f}-{high:.1f}m\n"
            f"{self.sectors} sectors, {self.linedefs} linedefs"
        )


@dataclass
class BuiltMap:
    geometry: MapGeometry
    tile: Tile
    place: Place
    title: str
    stats: BuildStats


#: The eight Doom facings, as (degrees, dx, dy). Doom angles run anticlockwise
#: from east, matching atan2 on a y-up grid.
_FACINGS = [
    (0, 1.0, 0.0),
    (45, 0.7071, 0.7071),
    (90, 0.0, 1.0),
    (135, -0.7071, 0.7071),
    (180, -1.0, 0.0),
    (225, -0.7071, -0.7071),
    (270, 0.0, -1.0),
    (315, 0.7071, -0.7071),
]


def _best_facing(x: float, y: float, blocked: list[Polygon], size_units: int) -> int:
    """Face whichever direction has the most open ground.

    Spawning at a postcode centroid frequently puts you in a yard or an alley,
    where the default facing is a wall a metre from your nose. Probing outward
    costs nothing and makes the first second of the map read as a place rather
    than a texture.
    """
    reach = 40 * UNITS_PER_METRE
    probe = 2 * UNITS_PER_METRE

    best_angle, best_distance = 90, -1.0
    for angle, dx, dy in _FACINGS:
        distance = 0.0
        while distance < reach:
            distance += probe
            px, py = x + dx * distance, y + dy * distance
            if not (0 <= px <= size_units and 0 <= py <= size_units):
                break
            if any(b.contains(Point(px, py)) for b in blocked):
                break
        if distance > best_distance:
            best_angle, best_distance = angle, distance
    return best_angle


def _player_start(place: Place, tile: Tile, blocked: list[Polygon], size_units: int) -> Thing:
    """Spawn at the postcode centroid, nudged clear of any building."""
    x, y = tile.to_map(place.easting, place.northing)
    x = min(max(x, 96), size_units - 96)
    y = min(max(y, 96), size_units - 96)

    def clear(px: float, py: float) -> bool:
        p = Point(px, py)
        return not any(b.contains(p) for b in blocked)

    if not clear(x, y):
        # Spiral outward rather than spawning the player inside a wall.
        step = 3 * UNITS_PER_METRE
        found = False
        for ring in range(1, 30):
            for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
                px, py = x + dx * ring * step, y + dy * ring * step
                if 96 <= px <= size_units - 96 and 96 <= py <= size_units - 96 and clear(px, py):
                    x, y, found = px, py, True
                    break
            if found:
                break

    return Thing(
        x=round(x),
        y=round(y),
        type=PLAYER_START,
        angle=_best_facing(x, y, blocked, size_units),
    )


def build_tile(
    place: Place,
    size_m: int = 400,
    cache_dir: Path | None = None,
    refresh: bool = False,
    contour_step_m: float = 0.5,
    with_terrain: bool = True,
    with_roads: bool = True,
    with_water: bool = True,
) -> BuiltMap:
    tile = Tile.containing(place.easting, place.northing, size_m)
    stats = BuildStats()
    clip = tile_clip(tile)

    dtm = dsm = None
    bands = []
    base_elevation_m = 0.0

    if with_terrain:
        dtm = lidar.fill_holes(lidar.fetch_dtm(tile.bbox_osgb, cache_dir, refresh))
        dsm = lidar.fetch_dsm(tile.bbox_osgb, cache_dir, refresh)
        bands = contour_bands(dtm, tile, step_m=contour_step_m)
        stats.terrain_bands = len(bands)
        if bands:
            stats.elevation_range_m = (bands[0].elevation_m, bands[-1].elevation_m)
            # The base plane only shows through where the bands failed to cover.
            # Put it at the median rather than the minimum, so any hole is a
            # shallow dip instead of a drop to the bottom of the whole tile.
            base_elevation_m = bands[len(bands) // 2].elevation_m

    max_ground_units = round(stats.elevation_range_m[1] * UNITS_PER_METRE)
    sky_height = max_ground_units + SKY_CLEARANCE

    # A flat base under everything, so any gap the contours leave is still floor
    # rather than a hole in the world.
    specs: list[SectorSpec] = [
        SectorSpec(
            polygon=clip,
            floor=round(base_elevation_m * UNITS_PER_METRE),
            ceiling=sky_height,
            floor_tex=textures.GRASS,
            wall_tex=textures.TERRAIN_SIDE,
            light=DAYLIGHT,
        )
    ]

    for band in bands:
        specs.append(
            SectorSpec(
                polygon=band.polygon,
                floor=band.floor_units,
                ceiling=sky_height,
                floor_tex=textures.GRASS,
                wall_tex=textures.TERRAIN_SIDE,
                light=DAYLIGHT,
            )
        )

    features = overpass.fetch_tile(tile.bbox_wgs84, cache_dir, refresh)

    def terrain_at(polygon: Polygon) -> float:
        if dtm is None:
            return 0.0
        return ground_height_m(dtm, tile, polygon)

    if with_water:
        for shape in water_to_shapes(features.water, tile):
            level = round(terrain_at(shape.polygon) * UNITS_PER_METRE)
            specs.append(
                SectorSpec(
                    polygon=shape.polygon,
                    floor=level - WATER_DEPTH_UNITS,
                    ceiling=sky_height,
                    floor_tex=textures.WATER,
                    wall_tex=textures.BANK,
                    light=WATER_LIGHT,
                )
            )
            stats.water += 1

    if with_roads:
        for shape in roads_to_shapes(features.roads, tile):
            paved = shape.tags.get("highway", "") in PAVED_FOOTWAYS
            # Clip the corridor against each contour band so a road climbing a
            # hill steps with the ground instead of flattening across it.
            for piece, elevation_m in _split_by_bands(shape.polygon, bands, terrain_at):
                floor = round(elevation_m * UNITS_PER_METRE)
                specs.append(
                    SectorSpec(
                        polygon=piece,
                        floor=floor if paved else floor - KERB_UNITS,
                        ceiling=sky_height,
                        floor_tex=textures.PAVEMENT if paved else textures.ROAD,
                        wall_tex=textures.KERB,
                        light=ROAD_LIGHT,
                    )
                )
                stats.roads += 1

    building_shapes = buildings_to_shapes(features.buildings, tile)
    for shape in building_shapes:
        ground_m = terrain_at(shape.polygon)
        height_m = shape.height_m
        if dsm is not None and dtm is not None:
            measured = object_height_m(dsm, dtm, tile, shape.polygon)
            # Trust LIDAR when it gives a sane answer; fall back to OSM tags.
            if 2.0 <= measured <= 80.0:
                height_m = measured
        wall = textures.building_wall(shape.tags)
        specs.append(
            SectorSpec(
                polygon=shape.polygon,
                floor=round((ground_m + height_m) * UNITS_PER_METRE),
                ceiling=sky_height,
                floor_tex=textures.ROOF,
                wall_tex=wall,
                light=ROOF_LIGHT,
                wall_scale_x=textures.wall_scale(wall),
                wall_scale_y=textures.wall_scale(wall),
            )
        )
        stats.buildings += 1

    start = _player_start(place, tile, [s.polygon for s in building_shapes], tile.size_units)
    geometry = build_geometry(specs, things=[start])

    stats.sectors = len(geometry.sectors)
    stats.linedefs = len(geometry.linedefs)

    title = place.name or place.postcode or place.query
    return BuiltMap(geometry=geometry, tile=tile, place=place, title=title, stats=stats)


def _split_by_bands(polygon: Polygon, bands, fallback) -> list[tuple[Polygon, float]]:
    """Cut a polygon into per-contour pieces, each with its own elevation."""
    if not bands:
        return [(polygon, fallback(polygon))]

    pieces: list[tuple[Polygon, float]] = []
    for band in bands:
        if not polygon.intersects(band.polygon):
            continue
        part = polygon.intersection(band.polygon)
        if part.is_empty:
            continue
        for geom in getattr(part, "geoms", [part]):
            if isinstance(geom, Polygon) and geom.area > 16 * UNITS_PER_METRE:
                pieces.append((geom, band.elevation_m))

    return pieces or [(polygon, fallback(polygon))]
