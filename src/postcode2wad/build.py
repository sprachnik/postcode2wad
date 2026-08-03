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
from shapely.ops import nearest_points, unary_union

from . import UNITS_PER_METRE, art, textures
from .features import (
    barriers_to_shapes,
    beach_from_coastline,
    buildings_to_shapes,
    landuse_to_shapes,
    roads_to_shapes,
    sea_from_coastline,
    tile_clip,
    water_to_shapes,
)
from .geometry import MapGeometry, SectorSpec, Thing, build_geometry
from .sources import lidar, overpass
from .sources.postcodes import Place
from .terrain import (
    contour_bands,
    ground_height_m,
    height_sampler,
    object_height_m,
    vegetation,
)
from .tiles import Tile

#: Headroom above the highest ground before the sky plane.
SKY_CLEARANCE = 4096

#: Roads sit a kerb's depth below the surrounding ground.
KERB_UNITS = 8
WATER_DEPTH_UNITS = 20

#: Mean sea level, in metres above ordnance datum. Zero by definition — ODN *is*
#: mean sea level at Newlyn — so no measurement is needed and every coastal tile
#: in the country agrees on it for free.
SEA_LEVEL_M = 0.0

#: The foreshore. 30m of sand landward of the coastline, kept to ground below
#: BEACH_TOP_M so it stops at the foot of a cliff instead of climbing it — at
#: Birchington the coast is chalk, and sand running up the cliff face would be
#: worse than no beach at all.
BEACH_WIDTH_M = 30.0
BEACH_TOP_M = 6.0

#: How far the sea surface sits below the datum. Exactly one Doom step: a Doom
#: player can climb 24 units and no more, so at 24 you can wade off a beach and
#: back out again, while a 10m chalk cliff is still a cliff. (There is no
#: separate water plane without 3D floors — the sector floor *is* the surface.)
SEA_SURFACE_UNITS = -24

#: Sector light levels. The old default of 192 everywhere is interior gloom —
#: correct for a bunker, wrong for a Tuesday afternoon in Kent. Depth now comes
#: from MAPINFO fog (see pk3.py) rather than from darkening, so these can sit
#: high without flattening the scene. The small spread between them is not
#: physical, it just stops every surface in frame having identical value.
DAYLIGHT = 208
ROOF_LIGHT = 216  # unshaded, facing straight up
ROAD_LIGHT = 200  # in the lee of buildings and kerbs more often than not
WATER_LIGHT = 216

#: Contour step by mode. The flat value must stay at or below 0.75m (Doom's
#: 24-unit step); the sloped one only controls how finely the ground is cut into
#: sectors, since the height itself comes from a per-sector plane.
DEFAULT_FLAT_STEP_M = 0.5
#: 4m rather than 2m: the height comes from a per-triangle plane, so the bands
#: only decide how finely the ground is tessellated. Coarsening to 4m drops the
#: tile from 7,349 sectors to 5,613 with no visible change; 8m saves a further
#: 1,200 but starts fitting a single plane across a whole field.
DEFAULT_SLOPED_STEP_M = 4.0

PLAYER_START = 1

PAVED_FOOTWAYS = {"footway", "path", "pedestrian", "steps", "cycleway", "bridleway"}


@dataclass
class BuildStats:
    buildings: int = 0
    roads: int = 0
    water: int = 0
    sea: int = 0
    beach: int = 0
    landuse: int = 0
    barriers: int = 0
    trees: int = 0
    terrain_bands: int = 0
    sectors: int = 0
    linedefs: int = 0
    elevation_range_m: tuple[float, float] = (0.0, 0.0)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        low, high = self.elevation_range_m
        return (
            f"{self.buildings} buildings, {self.roads} road pieces, "
            f"{self.water} water, {self.sea} sea, {self.beach} beach, "
            f"{self.landuse} land parcels, "
            f"{self.barriers} barriers, {self.trees} trees, "
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


def _player_start(
    place: Place,
    tile: Tile,
    blocked: list[Polygon],
    size_units: int,
    roads: list[Polygon] | None = None,
) -> Thing:
    """Spawn on the street nearest the postcode, else at the postcode itself.

    Probing eight directions for open ground was not enough on a real tile: a
    postcode centroid frequently lands in a back garden or a farmyard, where
    *every* facing is a wall a few metres away and the best of eight is still a
    wall. Standing on the carriageway instead gives an open view along the road
    by construction, and "you appear on the street outside" is what someone
    typing their own postcode expects anyway.
    """
    x, y = tile.to_map(place.easting, place.northing)
    if not (0 <= x <= size_units and 0 <= y <= size_units):
        # A region pack builds tiles the postcode is nowhere near. Clamping
        # would jam every one of those starts into the corner nearest the
        # postcode; the middle of the tile is the honest default.
        x = y = size_units / 2.0
    x = min(max(x, 96), size_units - 96)
    y = min(max(y, 96), size_units - 96)

    if roads:
        here = Point(x, y)
        nearest = min(roads, key=here.distance)
        # A point *on* the road: its own interior if we are already standing in
        # it, otherwise the closest bit of tarmac to the postcode.
        target = here if nearest.contains(here) else nearest_points(nearest, here)[0]
        # Step a little way in from the edge so we are not clipping the kerb.
        inward = nearest.representative_point()
        length = max(1e-6, target.distance(inward))
        step = min(24.0, length)
        x = target.x + (inward.x - target.x) * step / length
        y = target.y + (inward.y - target.y) * step / length
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
    contour_step_m: float | None = None,
    with_terrain: bool = True,
    with_roads: bool = True,
    with_water: bool = True,
    with_landuse: bool = True,
    with_barriers: bool = True,
    with_trees: bool = True,
    with_slopes: bool = True,
    tile: Tile | None = None,
    features: overpass.TileFeatures | None = None,
) -> BuiltMap:
    # Sloped ground is not bound by Doom's 24-unit climb limit, so the contour
    # step can be four times coarser — which is where the sector saving comes
    # from. Flat ground still has to stay under the limit, or the terrain
    # renders perfectly and cannot be walked up.
    if contour_step_m is None:
        contour_step_m = DEFAULT_SLOPED_STEP_M if with_slopes else DEFAULT_FLAT_STEP_M

    # A region pack builds tiles the postcode does not sit in, so the caller can
    # name the tile directly. Without an override the postcode picks it.
    if tile is None:
        tile = Tile.containing(place.easting, place.northing, size_m)
    stats = BuildStats()
    clip = tile_clip(tile)

    dtm = dsm = None
    bands = []
    base_elevation_m = 0.0

    if with_terrain:
        dtm = lidar.fill_holes(lidar.fetch_dtm(tile.bbox_osgb, cache_dir, refresh))
        dsm = lidar.fetch_dsm(tile.bbox_osgb, cache_dir, refresh)
        # With slopes the bands stop carrying the height and become only a way
        # of spreading mesh vertices over the tile, so the 0.75m step ceiling no
        # longer applies and a much coarser step buys back the sectors that
        # triangulating costs.
        bands = contour_bands(
            dtm, tile, step_m=contour_step_m, allow_large_step=with_slopes
        )
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
            sloped=with_slopes,
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
                sloped=with_slopes,
            )
        )

    # A region pack fetches one query covering every tile and hands the result
    # to each in turn: features are clipped to the tile downstream anyway, so
    # nine tiles need one Overpass round trip rather than nine.
    if features is None:
        features = overpass.fetch_tile(tile.bbox_wgs84, cache_dir, refresh)

    def terrain_at(polygon: Polygon) -> float:
        if dtm is None:
            return 0.0
        return ground_height_m(dtm, tile, polygon)

    if with_landuse:
        # Land cover changes the *surface*, not the height, so each parcel is
        # cut against the contour bands and each piece keeps its band's floor.
        # Laying a parcel down flat instead would iron a field into a plateau.
        for shape in landuse_to_shapes(features.landuse, tile):
            flat = textures.land_cover(shape.tags)
            if flat is None:
                continue  # unrecognised: leave the terrain showing through
            for piece, elevation_m in _split_by_bands(
                shape.polygon, bands, terrain_at, with_slopes
            ):
                specs.append(
                    SectorSpec(
                        polygon=piece,
                        floor=round(elevation_m * UNITS_PER_METRE),
                        ceiling=sky_height,
                        floor_tex=flat,
                        # Risers inside a parcel wear the parcel's own cover, so
                        # a contour step in a ploughed field is ploughed too.
                        wall_tex=flat,
                        light=DAYLIGHT,
                        sloped=with_slopes,
                    )
                )
            stats.landuse += 1

    if with_water:
        # Sea first, so an inland lake or a river mouth laid over it still wins.
        for shape in sea_from_coastline(features.coastline, tile):
            specs.append(
                SectorSpec(
                    polygon=shape.polygon,
                    floor=round(SEA_LEVEL_M * UNITS_PER_METRE) + SEA_SURFACE_UNITS,
                    ceiling=sky_height,
                    floor_tex=textures.WATER,
                    wall_tex=textures.BANK,
                    light=WATER_LIGHT,
                )
            )
            stats.sea += 1

        # Beach after the sea so the sand sits on the land side of the line,
        # before roads and buildings so a seafront promenade still wins.
        for shape in beach_from_coastline(features.coastline, tile, BEACH_WIDTH_M):
            for piece, elevation_m in _split_by_bands(
                shape.polygon, bands, terrain_at, with_slopes
            ):
                if elevation_m > BEACH_TOP_M:
                    continue
                specs.append(
                    SectorSpec(
                        polygon=piece,
                        floor=round(elevation_m * UNITS_PER_METRE),
                        ceiling=sky_height,
                        floor_tex=textures.SAND,
                        wall_tex=textures.SAND,
                        light=DAYLIGHT,
                        sloped=with_slopes,
                    )
                )
            stats.beach += 1

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

    # Two lists, deliberately. The player start wants a *carriageway* to appear
    # on, so footways are kept out of it. Tree exclusion wants every kind of
    # right of way, footpaths very much included -- a tree in a 2m footway is
    # the one that traps you.
    road_polygons: list[Polygon] = []
    walkable_polygons: list[Polygon] = []
    barrier_polygons: list[Polygon] = []

    if with_roads:
        for shape in roads_to_shapes(features.roads, tile):
            paved = shape.tags.get("highway", "") in PAVED_FOOTWAYS
            walkable_polygons.append(shape.polygon)
            if not paved:
                road_polygons.append(shape.polygon)
            # Clip the corridor against each contour band so a road climbing a
            # hill steps with the ground instead of flattening across it.
            for piece, elevation_m in _split_by_bands(
                shape.polygon, bands, terrain_at, with_slopes
            ):
                floor = round(elevation_m * UNITS_PER_METRE)
                specs.append(
                    SectorSpec(
                        polygon=piece,
                        floor=floor if paved else floor - KERB_UNITS,
                        ceiling=sky_height,
                        floor_tex=textures.PAVEMENT if paved else textures.ROAD,
                        wall_tex=textures.KERB,
                        light=ROAD_LIGHT,
                        sloped=with_slopes,
                    )
                )
                stats.roads += 1

    if with_barriers:
        # After roads so a hedge along a verge survives, before buildings so a
        # house laid over one still wins — the hedge stops at the wall.
        #
        # Cut at every right of way, footpaths included. OSM draws a field
        # boundary as one unbroken way even where a path crosses it — the gate
        # or stile is a node, which this pipeline drops — so an uncut hedge
        # walls the path off dead. Where the hedge meets the path it now simply
        # stops, and the gap left behind is the gate. Enclosed back gardens
        # keep their hedges: no path crosses them, so nothing is cut.
        walkable_union = unary_union(walkable_polygons) if walkable_polygons else None
        for shape in barriers_to_shapes(features.barriers, tile):
            barrier = shape.polygon
            if walkable_union is not None:
                barrier = barrier.difference(walkable_union)
            if barrier.is_empty:
                continue
            wall = textures.barrier_wall(shape.tags)
            parts = [
                g
                for g in getattr(barrier, "geoms", [barrier])
                if isinstance(g, Polygon) and g.area > 0.25 * UNITS_PER_METRE**2
            ]
            barrier_polygons.extend(parts)
            for part in parts:
                for piece, elevation_m in _split_by_bands(
                    part, bands, terrain_at, with_slopes
                ):
                    specs.append(
                        SectorSpec(
                            polygon=piece,
                            floor=round((elevation_m + shape.height_m) * UNITS_PER_METRE),
                            ceiling=sky_height,
                            floor_tex=textures.HEDGE_TOP
                            if wall == textures.HEDGE
                            else textures.CONCRETE,
                            wall_tex=wall,
                            light=DAYLIGHT,
                            thin=True,   # a fence is meant to be this narrow
                        )
                    )
            stats.barriers += 1

    building_shapes = buildings_to_shapes(features.buildings, tile)
    for shape in building_shapes:
        ground_m = terrain_at(shape.polygon)
        height_m = shape.height_m
        if dsm is not None and dtm is not None:
            measured = object_height_m(dsm, dtm, tile, shape.polygon)
            # Trust LIDAR when it gives a sane answer; fall back to OSM tags.
            if 2.0 <= measured <= 80.0:
                height_m = measured
        # The façade covers the wall exactly once, so scaley depends on the
        # building's measured height. scalex stays 1: the texture is authored at
        # 32 px/m horizontally, which is already map scale.
        wall, scale_y = textures.building_facade(shape.tags, shape.osm_id, height_m)
        specs.append(
            SectorSpec(
                polygon=shape.polygon,
                floor=round((ground_m + height_m) * UNITS_PER_METRE),
                ceiling=sky_height,
                floor_tex=textures.ROOF,
                wall_tex=wall,
                light=ROOF_LIGHT,
                wall_scale_y=scale_y,
            )
        )
        stats.buildings += 1

    # Barriers block too: spawning nose-first into a hedge is no better than
    # spawning into a wall.
    obstacles = [s.polygon for s in building_shapes] + barrier_polygons
    things = [
        _player_start(place, tile, obstacles, tile.size_units, roads=road_polygons)
    ]

    if with_trees and dsm is not None and dtm is not None:
        # Roads and barriers exclude trees as well as buildings. A tree is a
        # solid actor, and a Doom player needs 28 units of clearance to squeeze
        # past one; a 2m footway is 64 units wide, so a tree anywhere near the
        # middle seals it completely. LIDAR happily finds canopy overhanging a
        # lane, and 21 of the 27 trees this put in a carriageway were
        # impassable -- you walk down a path and simply stop.
        keep_clear = (
            [s.polygon for s in building_shapes] + walkable_polygons + barrier_polygons
        )
        for tree in vegetation(dsm, dtm, tile, exclude=keep_clear):
            things.append(
                Thing(
                    x=round(tree.x),
                    y=round(tree.y),
                    type=art.TREE_DOOMEDNUM,
                    scale=round(tree.height_m / art.TREE_HEIGHT_M, 3),
                )
            )
            stats.trees += 1

    geometry = build_geometry(
        specs,
        things=things,
        height_at=height_sampler(dtm, tile) if (with_slopes and dtm is not None) else None,
    )

    stats.sectors = len(geometry.sectors)
    stats.linedefs = len(geometry.linedefs)

    title = place.name or place.postcode or place.query
    return BuiltMap(geometry=geometry, tile=tile, place=place, title=title, stats=stats)


def _split_by_bands(polygon: Polygon, bands, fallback, sloped: bool = False) -> list[tuple[Polygon, float]]:
    """Cut a polygon into per-contour pieces, each with its own elevation.

    Skipped entirely when the ground slopes. The split exists so a road climbing
    a hill steps with the terrain, and a sloped road simply follows it instead —
    so all the split would do is chop the road into per-band pieces that each get
    their own plane anyway. Worse than redundant: a piece that fails to
    triangulate falls back to its band's flat elevation, which at a 4m contour
    step is a 4m unclimbable wall across the carriageway. That was 204 invisible
    walls on one tile.
    """
    if sloped or not bands:
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
