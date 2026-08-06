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
    railways_to_shapes,
    roads_to_shapes,
    sea_from_coastline,
    tile_clip,
    water_to_shapes,
)
from .geometry import MapGeometry, SectorSpec, Thing, build_geometry
from .sources import lidar, osm, overpass
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
#:
#: 4096 units is 128m, which is enough for anywhere the generator has been
#: pointed so far and is not enough for a city. The Shard is 310m (9,920 units)
#: and would stand straight through the sky. Raised to clear the tallest
#: building in Britain, since a sky plane costs nothing but a number — the
#: sector is empty air either way.
SKY_CLEARANCE = 12288  # 384m

#: Above this, a LIDAR-measured building height is treated as an artefact.
#: Deliberately above the Shard (310m); see the note at the measurement.
MAX_MEASURED_HEIGHT_M = 350.0

#: Standard gauge, and how the rails are drawn. 1.435m is the gauge of every
#: main line in Britain; 0.14m is generous for a 7cm rail head but a hair-thin
#: sector disappears at any distance, and the point of drawing them at all is
#: that a railway should be identifiable as one.
RAIL_GAUGE_M = 1.435
RAIL_WIDTH_M = 0.14
#: Rail head above the sleeper, in map units.
#:
#: 2 units is 6cm. A real rail stands about 16cm proud, which is what this was
#: set to, and it read as too tall in Birchington — the rails looked like low
#: kerbs running down the track rather than lines on it. The honest reason is
#: that a rail is also only 7cm *wide*, and the width has to be exaggerated to
#: 14cm to survive at any distance; keeping the height at life size next to a
#: doubled width makes the section square instead of flat. Halving it restores
#: the proportion that reads as rail.
RAIL_HEIGHT_UNITS = 2

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

#: A piece of building thinner than this is an offcut, not a building.
#:
#: Footprints get clipped by things that have nothing to do with them -- a
#: contour band, a garden boundary running a few centimetres off the wall --
#: and the leftover strip becomes its own sector at roof height. With ground on
#: both sides it renders as a freestanding brick fin, tens of metres long and a
#: few centimetres thick, with no building behind it.
#:
#: 1m is chosen to sit clear of both ends: the fins measured 0.1m to 0.5m, and
#: the narrowest genuine building part in the region is a 1.6m porch.
MIN_BUILDING_WIDTH = 1.0 * UNITS_PER_METRE
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

#: Radius of the low-pass applied to the DTM before ground planes are fitted.
#: Not a cosmetic nicety: raw 1m LIDAR noise, sampled per triangle, makes the
#: surface visibly faceted. Kept well under the scale of real landform.
GROUND_SMOOTH_M = 4.0

#: Spacing of the samples taken along a road's centreline, and the half-width
#: of the straight-line fit run over them.
#:
#: 12m of fit either side is chosen against the thing being removed: the DTM is
#: smoothed at a 4m radius, so cross-slope noise survives at wavelengths above
#: that, and a road has to be smoothed over meaningfully more than the noise to
#: be flatter than it. It is also short enough to keep a real hump-backed bridge
#: or a dip at a ford, which run over 20m or more.
ROAD_PROFILE_STEP_M = 2.0
ROAD_PROFILE_SMOOTH_M = 12.0

#: How far a road surface may be pulled away from the ground beside it, in map
#: units. Doom's climb limit is 24 units, and this bounds the step at every join
#: a road makes: against the ground beside it the step is at most this, and
#: where two different ways meet each is within this of the same ground, so at
#: most twice this. 10 units (31cm) therefore leaves a 20-unit worst case with
#: room to spare, while being enough to absorb the cross-slope noise measured
#: on real pavements -- the 90th percentile of the correction is under 4 units.
ROAD_LIFT_LIMIT = 10.0

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
    railways: int = 0
    trees: int = 0
    terrain_bands: int = 0
    sectors: int = 0
    linedefs: int = 0
    elevation_range_m: tuple[float, float] = (0.0, 0.0)
    #: Percentage of the tile's ground by land cover, keyed by
    #: `SectorSpec.cover`. Measured on the arrangement, so the parts are
    #: disjoint and sum to ~100 -- see `MapGeometry.cover_area`.
    #:
    #: "terrain" is not a landscape. It is ground where OSM had nothing to say,
    #: and on a rural tile it is usually the largest share. Reporting it as its
    #: own category rather than folding it into greenspace is the difference
    #: between measuring the map and measuring OpenStreetMap's coverage of it.
    cover_pct: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        low, high = self.elevation_range_m
        return (
            f"{self.buildings} buildings, {self.roads} road pieces, "
            f"{self.water} water, {self.sea} sea, {self.beach} beach, "
            f"{self.landuse} land parcels, "
            f"{self.barriers} barriers, {self.railways} railway pieces, "
            f"{self.trees} trees, "
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
    easting: float,
    northing: float,
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
    x, y = tile.to_map(easting, northing)
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


def _straighten(values: list[float], radius: int) -> list[float]:
    """Smooth a 1-D profile by a local straight-line fit through its window.

    A box average was tried here first and is wrong at the ends. Averaging a
    truncated window over a road on a constant gradient pulls the last sample
    toward the middle of the window, so a 10% slope running off the end of a way
    finished 2.6m below its own ground -- the single largest error in the whole
    approach, and it appeared exactly where two ways meet, which is where a step
    is least forgivable. A straight-line fit reproduces a constant gradient
    exactly however much of the window it can see, so the bias is zero at the
    ends by construction. Measured on Birchington: the 90th percentile of the
    correction fell from 9.8 units to 3.5 on footways.
    """
    count = len(values)
    out: list[float] = []
    for i in range(count):
        lo, hi = max(0, i - radius), min(count, i + radius + 1)
        n = hi - lo
        if n < 2:
            out.append(values[i])
            continue
        sum_x = sum(range(lo, hi))
        sum_xx = sum(j * j for j in range(lo, hi))
        sum_y = sum(values[lo:hi])
        sum_xy = sum(j * values[j] for j in range(lo, hi))
        denominator = n * sum_xx - sum_x * sum_x
        if denominator == 0:
            out.append(sum_y / n)
            continue
        gradient = (n * sum_xy - sum_x * sum_y) / denominator
        out.append((sum_y - gradient * sum_x) / n + gradient * i)
    return out


def _road_surface(line, ground_at, limit: float = ROAD_LIFT_LIMIT, spans: bool = False):
    """A road's height as a function of distance along its own centreline.

    A carriageway is an engineered surface: level across its width, smoothly
    graded along its length. The arrangement-wide sampler is a function of
    (x, y), so fitting a road to it copies the ground's cross-slope wobble onto
    a surface that in life has none -- and the smaller the surface, the worse it
    reads, because the DTM is smoothed at a 4m radius and a pavement is 2m wide.
    Measured on the nine Birchington tiles, the angle between neighbouring
    same-material faces at the 95th percentile was 18.2 degrees on pavement
    against 5.4 on tarmac, for exactly that reason.

    So the height stops being a function of position and becomes a function of
    *distance along the way*: sample the smoothed ground along the centreline,
    fit a straight line through a moving window of it, and read that off. Across
    the width it is then constant, which is the whole point.

    The result is clamped to within `limit` of the ground it replaces. Without
    that a road crossing a bank could hang a metre over it. With it, every step
    the road makes is bounded: against the ground beside it by `limit`, and
    against a different way's surface by twice `limit`, since both are anchored
    to the same ground where they meet.

    """
    step = ROAD_PROFILE_STEP_M * UNITS_PER_METRE
    count = max(2, int(line.length / step) + 1)
    step = line.length / (count - 1)
    heights = [
        ground_at(*line.interpolate(step * i).coords[0]) for i in range(count)
    ]
    if spans:
        # A bridge does not follow the ground; that is what makes it a bridge.
        # Level, at the higher of its two ends, and the clamp turned off —
        # clamping a deck to within 31cm of the riverbed is the dip being fixed.
        #
        # Level rather than a straight line between the ends, which is what this
        # did first. The ends are sampled on the *ground*, and an endpoint
        # sitting on the embankment slope instead of the abutment top reads low
        # and tilts the whole deck. Measured on Swanley: two motorway spans of
        # 33m and 34m came out at 1 in 11, a 9% grade on a motorway. Taking the
        # higher end makes every deck flat and guarantees it clears what it
        # crosses.
        #
        # The cost is a long viaduct on a real gradient, which would come out
        # level and step at one end. Every span here is 25-76m, where level is
        # right; revisit if a genuinely graded structure turns up.
        deck = max(heights[0], heights[-1])
        heights = [deck] * count
        limit = float("inf")
    else:
        heights = _straighten(
            heights, max(1, round(ROAD_PROFILE_SMOOTH_M * UNITS_PER_METRE / step))
        )

    cache: dict[tuple[int, int], float] = {}

    def at(x: float, y: float) -> float:
        # Keyed on the rounded position because that is what the plane fitter
        # samples, and two faces sharing a corner must get identical answers
        # from it or their planes part company along the edge they share.
        key = (round(x), round(y))
        if key in cache:
            return cache[key]
        along = line.project(Point(*key))
        index = min(count - 2, int(along / step))
        fraction = min(1.0, max(0.0, (along - index * step) / step))
        height = heights[index] + fraction * (heights[index + 1] - heights[index])
        ground = ground_at(*key)
        cache[key] = min(max(height, ground - limit), ground + limit)
        return cache[key]

    return at


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
    spawn: tuple[float, float] | None = None,
    osm_source: str = "auto",
    osm_extract: str | Path | None = None,
    lidar_source: str = "auto",
    lidar_dir: str | Path | None = None,
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
        dtm = lidar.fill_holes(
            lidar.fetch_dtm(tile.bbox_osgb, cache_dir, refresh, lidar_source, lidar_dir),
            where=f"tile {tile.id}",
        )
        dsm = lidar.fetch_dsm(tile.bbox_osgb, cache_dir, refresh, lidar_source, lidar_dir)
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
    # nine tiles need one round trip rather than nine.
    if features is None:
        chosen, extract = osm.choose(tile.bbox_wgs84, osm_source, osm_extract, cache_dir)
        stats.notes.append(osm.describe(chosen, extract))
        features = osm.fetch_tile(
            tile.bbox_wgs84, cache_dir, refresh, source=chosen, extract=extract
        )

    def terrain_at(polygon: Polygon) -> float:
        if dtm is None:
            return 0.0
        return ground_height_m(dtm, tile, polygon)

    # Built here rather than at the `build_geometry` call because the roads
    # below need it too: each one derives its own height profile from it.
    ground_at = (
        height_sampler(dtm, tile, GROUND_SMOOTH_M)
        if (with_slopes and dtm is not None)
        else None
    )

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
                        cover=textures.COVER_BY_FLAT.get(flat, "terrain"),
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
                    cover="sea",
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
                        cover="beach",
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
                    cover="water",
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
        # Bridges last, so `build_geometry`'s last-wins resolution hands the
        # crossing to the deck rather than to the road underneath.
        #
        # A Doom sector has one floor height per point, so where a flyover
        # crosses a road *something* has to give. Left alone the deck was
        # dragged down into the road it spans and the result was a dip in the
        # motorway — reported from play as "bridges render as dips". Choosing
        # which way loses is the only move available, and the deck is the right
        # winner: it is the surface you are usually driving on, and the road
        # beneath ending at a dark opening reads as an underpass, while a
        # motorway sagging into a cutting reads as broken.
        #
        # This does not make the lower road passable. Nothing here can — that
        # needs GZDoom 3D floors, see TODO.md. It makes the geometry honest
        # about which way is on top.
        road_shapes = sorted(
            roads_to_shapes(features.roads, tile),
            key=lambda s: s.tags.get("bridge", "no") not in ("no", ""),
        )
        for shape in road_shapes:
            paved = shape.tags.get("highway", "") in PAVED_FOOTWAYS
            spanning = shape.tags.get("bridge", "no") not in ("no", "")
            walkable_polygons.append(shape.polygon)
            if not paved:
                road_polygons.append(shape.polygon)
            # A road's surface is level across its width, so it takes its height
            # from its own centreline rather than from the ground field. Every
            # piece of one way shares the profile, so pieces split apart by the
            # tile edge or by a junction still meet each other exactly.
            surface = (
                _road_surface(shape.centre, ground_at, spans=spanning)
                if ground_at is not None and shape.centre is not None
                else None
            )
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
                        floor_tex=textures.BRIDGE_DECK
                        if spanning
                        else (textures.PAVEMENT if paved else textures.ROAD),
                        cover="road",
                        # A bridge's sides are not kerbs, they are the face of
                        # a deck — and where one cuts a road off, that face is
                        # what the road now ends at. Dark, so it reads as the
                        # mouth of an underpass rather than as a wall someone
                        # left across the carriageway.
                        wall_tex=textures.BRIDGE_FACE if spanning else textures.KERB,
                        light=ROAD_LIGHT,
                        sloped=with_slopes,
                        height_at=surface,
                    )
                )
                stats.roads += 1

    # Railways after roads, so a level crossing shows the road surface: you
    # stand on the tarmac there, not on the ballast. Before barriers, so
    # lineside fencing still sits on top of the corridor.
    for shape in railways_to_shapes(features.railways, tile):
        for piece, elevation_m in _split_by_bands(
            shape.polygon, bands, terrain_at, with_slopes
        ):
            specs.append(
                SectorSpec(
                    polygon=piece,
                    floor=round(elevation_m * UNITS_PER_METRE) - KERB_UNITS,
                    ceiling=sky_height,
                    floor_tex=textures.BALLAST,
                    cover="rail",
                    wall_tex=textures.KERB,
                    light=ROAD_LIGHT,
                    sloped=with_slopes,
                )
            )
        # The rails themselves. Without them a railway is a grey strip and
        # reads as a gravel path — reported from the sandbox as "I don't see
        # anything I can identify as a railway line".
        #
        # These have to be geometry rather than paint: a flat tiles in both
        # axes, so rails drawn into DMRAIL would repeat across the track as
        # well as along it. That is the same reason road centre lines are
        # expensive — but a tile carries 0-4 railway ways against 90-300 roads,
        # so here it costs a handful of sectors instead of thousands.
        #
        # 1.435m apart is standard gauge, measured between the inside faces.
        # `thin` because a rail is 7cm wide and sliver absorption would
        # otherwise swallow it, exactly as it would a fence.
        if shape.centre is not None:
            for side in (-1, 1):
                rail = shape.centre.parallel_offset(
                    side * RAIL_GAUGE_M * UNITS_PER_METRE / 2.0, "left"
                )
                # An offset can come back empty where the centreline doubles
                # back on itself; the ballast still stands, it just has no rail.
                if rail.is_empty:
                    continue
                for part in getattr(rail, "geoms", [rail]):
                    if part.length <= 0:
                        continue
                    strip = part.buffer(
                        RAIL_WIDTH_M * UNITS_PER_METRE / 2.0, cap_style=2, join_style=2
                    )
                    strip = strip.intersection(shape.polygon)
                    if strip.is_empty:
                        continue
                    for piece, elevation_m in _split_by_bands(
                        strip, bands, terrain_at, with_slopes
                    ):
                        specs.append(
                            SectorSpec(
                                polygon=piece,
                                # Rail head stands proud of the sleeper, which
                                # is what catches the light and says "railway".
                                floor=round(elevation_m * UNITS_PER_METRE)
                                - KERB_UNITS
                                + RAIL_HEIGHT_UNITS,
                                ceiling=sky_height,
                                floor_tex=textures.RAIL_HEAD,
                                cover="rail",
                                wall_tex=textures.RAIL_HEAD,
                                light=ROAD_LIGHT,
                                thin=True,
                                # Sloped, exactly as the ballast beneath is.
                                # Without this the rail is one flat plane along
                                # its whole length while the ballast follows the
                                # ground, so anywhere the terrain falls the rail
                                # stands proud by the entire drop — reported as
                                # rails "tall like a fence". Birchington runs
                                # 8-24m, which is a lot of fall to stand on.
                                # The height constant was never the cause.
                                sloped=with_slopes,
                            )
                        )
        stats.railways += 1

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
                # The top of the barrier, as a function of position: the
                # ground under it plus its own height.
                #
                # A sloped sector takes its height from its fitted plane, not
                # from `floor` — so sloping a barrier against the bare ground
                # sampler pulls its top down to ground level and the fence
                # vanishes. That is what happened when `sloped` was first added
                # here: the rails survived it (they stand 2 units proud, so
                # flush still reads) and every fence in the county disappeared.
                # The offset has to be inside the sampler, not in `floor`.
                barrier_top = (
                    (lambda x, y, base=ground_at, off=shape.height_m * UNITS_PER_METRE:
                        base(x, y) + off)
                    if ground_at is not None
                    else None
                )
                for piece, elevation_m in _split_by_bands(
                    part, bands, terrain_at, with_slopes
                ):
                    specs.append(
                        SectorSpec(
                            polygon=piece,
                            floor=round((elevation_m + shape.height_m) * UNITS_PER_METRE),
                            height_at=barrier_top,
                            ceiling=sky_height,
                            floor_tex=textures.HEDGE_TOP
                            if wall == textures.HEDGE
                            else textures.WALL_CAP,
                            cover="barrier",
                            wall_tex=wall,
                            light=DAYLIGHT,
                            thin=True,   # a fence is meant to be this narrow
                            # Sloped, or the top of the barrier is one flat
                            # plane while the ground falls away under it — so a
                            # fence grows taller the further the land drops.
                            # Reported from play as fences below ground level
                            # standing up to meet it. Same defect as the rails.
                            sloped=with_slopes,
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
            #
            # The ceiling was 80m, which is under Big Ben (96m) — so every
            # building in the country taller than about 25 storeys quietly
            # ignored its own measurement and used whatever OSM said, or the
            # default. It is there to reject DSM artefacts, and the measurement
            # is a P90 inside the footprint, which a crane or a spike does not
            # move. 350m clears the tallest building in Britain (the Shard, 310)
            # while still rejecting the obviously impossible.
            if 2.0 <= measured <= MAX_MEASURED_HEIGHT_M:
                height_m = measured
        # The façade covers the wall exactly once, so scaley depends on the
        # building's measured height. scalex stays 1: the texture is authored at
        # 32 px/m horizontally, which is already map scale.
        # Footprint in m², so an untagged building too big to be a house does
        # not get sash windows and a front door. Polygon is in map units.
        footprint_m2 = shape.polygon.area / (UNITS_PER_METRE * UNITS_PER_METRE)
        wall, scale_y = textures.building_facade(
            shape.tags, shape.osm_id, height_m, footprint_m2
        )
        specs.append(
            SectorSpec(
                polygon=shape.polygon,
                floor=round((ground_m + height_m) * UNITS_PER_METRE),
                ceiling=sky_height,
                floor_tex=textures.ROOF,
                cover="building",
                wall_tex=wall,
                light=ROOF_LIGHT,
                wall_scale_y=scale_y,
                min_width=MIN_BUILDING_WIDTH,
            )
        )
        stats.buildings += 1

    # Barriers block too: spawning nose-first into a hedge is no better than
    # spawning into a wall.
    obstacles = [s.polygon for s in building_shapes] + barrier_polygons
    # An explicit spawn is exact: whoever typed coordinates meant *there*, so
    # skip the snap-to-nearest-road that the postcode default gets. The
    # clear-of-obstacles spiral still applies either way.
    spawn_e, spawn_n = spawn if spawn is not None else (place.easting, place.northing)
    things = [
        _player_start(
            spawn_e,
            spawn_n,
            tile,
            obstacles,
            tile.size_units,
            roads=None if spawn is not None else road_polygons,
        )
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

    geometry = build_geometry(specs, things=things, height_at=ground_at)

    stats.sectors = len(geometry.sectors)
    stats.linedefs = len(geometry.linedefs)

    # Against the arrangement's own total, not the tile square. The two are the
    # same to within rounding, but dividing by the square would quietly turn any
    # gap in the arrangement into a shortfall spread across every category,
    # which reads as "the numbers do not add up" rather than as the meshing bug
    # it would actually be.
    total = sum(geometry.cover_area.values())
    if total > 0:
        stats.cover_pct = {
            name: round(area / total * 100, 1)
            for name, area in sorted(
                geometry.cover_area.items(), key=lambda kv: -kv[1]
            )
        }

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
