"""LIDAR heightmap -> Doom sectors.

Doom floors are flat, so continuous terrain has to become steps. We quantise
elevation into bands and polygonise each band into a sector.

The bands are **absolute**, not per-tile: band index is `floor(elevation /
step)` against ordnance datum, never against this tile's own min and max. That
is what makes neighbouring tiles agree — two tiles generated independently put
their 21m contour in exactly the same place, so a shared edge lines up instead
of forming a cliff.

Sector count is the thing to watch. A naive polygonisation of a 400m tile at 1m
steps produces thousands of slivers, so we simplify hard and drop anything
below a minimum area. The brief's budget is under ~15-20k sectors per map.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import unary_union

from . import UNITS_PER_METRE
from .sources.lidar import Raster
from .tiles import Tile


@dataclass
class TerrainBand:
    polygon: Polygon  # map units, tile-local
    elevation_m: float  # bottom of the band, absolute (ordnance datum)

    @property
    def floor_units(self) -> int:
        return round(self.elevation_m * UNITS_PER_METRE)


def _osgb_to_map_matrix(tile: Tile) -> list[float]:
    """shapely affine_transform matrix taking OSGB metres to tile-local units."""
    origin_e, origin_n = tile.origin
    s = float(UNITS_PER_METRE)
    # [a, b, d, e, xoff, yoff] -> x' = a*x + b*y + xoff, y' = d*x + e*y + yoff
    return [s, 0.0, 0.0, s, -origin_e * s, -origin_n * s]


#: Doom refuses to step up more than 24 map units, which at 32 units/m is 0.75m.
#: A contour step at or above that height is an invisible wall: the terrain
#: renders fine and the player simply cannot climb it. Stay below the limit.
MAX_STEP_UNITS = 24
MAX_CONTOUR_STEP_M = MAX_STEP_UNITS / UNITS_PER_METRE  # 0.75


def _blur_axis(values: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """Box blur along one axis, by cumulative sums. Separable, so two passes."""
    width = 2 * radius + 1
    pad = [(radius, radius) if a == axis else (0, 0) for a in range(values.ndim)]
    padded = np.pad(values, pad, mode="edge")
    sums = np.cumsum(padded, axis=axis)
    lead = np.zeros([1 if a == axis else n for a, n in enumerate(sums.shape)])
    sums = np.concatenate([lead, sums], axis=axis)
    length = values.shape[axis]
    high = np.take(sums, range(width, width + length), axis=axis)
    low = np.take(sums, range(length), axis=axis)
    return (high - low) / width


def smooth_heights(dtm: Raster, radius_m: float) -> np.ndarray:
    """A low-pass copy of the DTM, for fitting ground planes against.

    The raw 1m LIDAR carries centimetre-scale noise -- ground clutter, vegetation
    the filter did not quite remove, the return pattern itself. Fitting a plane
    per triangle against it reproduces every wiggle, and since triangles are
    small (their size is set by road and parcel outlines, not by terrain) the
    result is a faceted surface: measured 15 degrees of dihedral at the 90th
    percentile between neighbours, up to 69 degrees along a road, which reads
    as choppy and blocky.

    Smoothing before fitting keeps the landform -- this tile spans 8m of relief
    over 400m, far coarser than the filter -- while removing the chop. Two box
    passes approximate a Gaussian closely enough and cost one pass over a
    400x400 array.
    """
    values = dtm.values.astype(float)
    finite = np.isfinite(values)
    if not finite.all():
        values = np.where(finite, values, np.nanmedian(values))
    radius = max(1, round(radius_m / dtm.pixel_m))
    for _ in range(2):
        values = _blur_axis(_blur_axis(values, radius, 0), radius, 1)
    return values


def height_sampler(dtm: Raster, tile: Tile, smooth_m: float = 0.0):
    """A fast map-units -> floor-units lookup, for per-vertex terrain heights.

    Nearest pixel, not bilinear, and deliberately so: this is called once per
    mesh vertex and the DTM is already 1m, finer than anything the geometry
    resolves. `ground_height_m` rasterises a polygon mask per call and is orders
    of magnitude too slow for this.
    """
    values = smooth_heights(dtm, smooth_m) if smooth_m > 0 else dtm.values
    rows, cols = values.shape
    origin_e, origin_n = tile.origin
    fallback = float(np.nanmedian(values))

    def at(x: float, y: float) -> float:
        e = origin_e + x / UNITS_PER_METRE
        n = origin_n + y / UNITS_PER_METRE
        col = int((e - dtm.min_e) / dtm.pixel_m)
        row = int((dtm.max_n - n) / dtm.pixel_m)
        if not (0 <= row < rows and 0 <= col < cols):
            return fallback * UNITS_PER_METRE
        value = values[row, col]
        if not np.isfinite(value):
            value = fallback
        return float(value) * UNITS_PER_METRE

    return at


def contour_bands(
    dtm: Raster,
    tile: Tile,
    step_m: float = 0.5,
    simplify_m: float = 1.2,
    min_area_m2: float = 30.0,
    allow_large_step: bool = False,
) -> list[TerrainBand]:
    """Quantise a DTM into absolute elevation bands and polygonise each.

    With sloped terrain the bands stop being the height model and become only a
    way of spreading vertices over the tile, so the step limit no longer applies
    -- hence `allow_large_step`. A coarser step then means far fewer bands for
    the same visual result, which is where sloping actually pays for itself.
    """
    from rasterio.features import shapes
    from rasterio.transform import Affine

    if step_m > MAX_CONTOUR_STEP_M and not allow_large_step:
        raise ValueError(
            f"contour step {step_m}m is {step_m * UNITS_PER_METRE:.0f} map units, above Doom's "
            f"{MAX_STEP_UNITS}-unit step limit — the player could not climb the terrain. "
            f"Use {MAX_CONTOUR_STEP_M}m or less."
        )

    values = dtm.values
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError("DTM has no valid pixels")

    # Absolute banding — the determinism guarantee lives on this line.
    banded = np.floor(np.where(finite, values, np.nanmedian(values)) / step_m).astype("int32")

    transform = Affine(dtm.pixel_m, 0.0, dtm.min_e, 0.0, -dtm.pixel_m, dtm.max_n)
    matrix = _osgb_to_map_matrix(tile)
    simplify_units = simplify_m * UNITS_PER_METRE
    min_area_units = min_area_m2 * UNITS_PER_METRE * UNITS_PER_METRE
    square = box(0, 0, tile.size_units, tile.size_units)

    bands: list[TerrainBand] = []
    for geom, value in shapes(banded, transform=transform, connectivity=4):
        poly = shape(geom)
        if poly.is_empty:
            continue
        poly = affine_transform(poly, matrix)
        if simplify_units:
            poly = poly.simplify(simplify_units, preserve_topology=True)
        if poly.is_empty or not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < min_area_units:
            continue
        if not isinstance(poly, Polygon):
            continue

        # Each band is simplified on its own, which breaks the shared edges
        # between neighbours and leaves hairline gaps. A gap falls through to
        # whatever is underneath — at the tile's lowest elevation — so a 0.1m
        # crack renders as a full-height cliff. Dilating each band past the
        # simplification tolerance makes neighbours overlap instead, and since
        # bands are laid low-to-high the higher one wins the overlap.
        if simplify_units:
            poly = poly.buffer(simplify_units, join_style=2)
            if not isinstance(poly, Polygon) or poly.is_empty:
                continue

        # Clip back to the tile. The DTM is fetched for a window at least as
        # large as the tile and the dilation above pushes bands a further 1.2m
        # out, so without this the map's outer boundary is a ragged fringe of
        # band edges instead of the tile square. Every segment of that fringe is
        # a one-sided line, and each one renders as a tear in the world with an
        # infinite flat plane behind it. Squaring the edge off removes them at
        # source — and it is required anyway for tile-edge determinism, since a
        # neighbouring tile cannot agree with a boundary that wanders.
        poly = poly.intersection(square)
        if poly.is_empty or not isinstance(poly, Polygon):
            continue

        bands.append(TerrainBand(polygon=poly, elevation_m=float(value) * step_m))

    # Lowest first, so higher ground is laid over lower and wins the overlap.
    bands.sort(key=lambda b: b.elevation_m)
    return bands


@dataclass(frozen=True)
class Tree:
    """One billboard: position in map units, canopy height in metres."""

    x: float
    y: float
    height_m: float


def vegetation(
    dsm: Raster,
    dtm: Raster,
    tile: Tile,
    exclude: list[Polygon] | None = None,
    min_height_m: float = 3.0,
    max_height_m: float = 28.0,
    min_area_m2: float = 6.0,
    per_tree_m2: float = 45.0,
    max_trees: int = 900,
) -> list[Tree]:
    """Find tree canopy in the LIDAR and scatter billboards through it.

    DSM minus DTM is height above bare earth. Anything tall that is *not* a
    mapped building is, in this country and at this resolution, overwhelmingly a
    tree — which makes the canopy essentially free, since both rasters are
    already fetched to measure building heights.

    Known false positives: LIDAR cannot tell a tree from an unmapped building,
    a marquee or a lorry, and OSM building coverage is not complete. Footprints
    are dilated before being subtracted because DSM edges bleed a pixel or two
    past a wall, which would otherwise ring every house with saplings.

    Woodland arrives as one large blob, so blobs are seeded with a tree per
    `per_tree_m2` on a jittered grid rather than getting a single sprite at the
    centroid.
    """
    from rasterio.features import shapes
    from rasterio.transform import Affine

    if dsm.values.shape != dtm.values.shape:
        return []

    delta = dsm.values - dtm.values
    canopy = np.isfinite(delta) & (delta >= min_height_m) & (delta <= max_height_m)
    if not canopy.any():
        return []

    transform = Affine(dsm.pixel_m, 0.0, dsm.min_e, 0.0, -dsm.pixel_m, dsm.max_n)
    matrix = _osgb_to_map_matrix(tile)
    square = box(0, 0, tile.size_units, tile.size_units)

    blocked = unary_union(
        [p.buffer(1.5 * UNITS_PER_METRE) for p in (exclude or [])]
    ) if exclude else None

    min_area_units = min_area_m2 * UNITS_PER_METRE * UNITS_PER_METRE
    per_tree_units = per_tree_m2 * UNITS_PER_METRE * UNITS_PER_METRE

    # Seeded on the tile, so the same tile always grows the same wood.
    rng = np.random.default_rng((tile.ix * 73856093) ^ (tile.iy * 19349663))

    trees: list[Tree] = []
    for geom, value in shapes(canopy.astype("uint8"), mask=canopy, transform=transform):
        if not value:
            continue
        poly = affine_transform(shape(geom), matrix)
        if not poly.is_valid:
            poly = poly.buffer(0)
        poly = poly.intersection(square)
        if blocked is not None:
            poly = poly.difference(blocked)
        if poly.is_empty:
            continue

        for part in getattr(poly, "geoms", [poly]):
            if not isinstance(part, Polygon) or part.area < min_area_units:
                continue
            height = object_height_m(dsm, dtm, tile, part)
            if not (min_height_m <= height <= max_height_m):
                continue
            for x, y in _scatter(part, per_tree_units, rng):
                trees.append(Tree(x=x, y=y, height_m=height))
                if len(trees) >= max_trees:
                    return trees
    return trees


def _scatter(polygon: Polygon, per_tree_units: float, rng) -> list[tuple[float, float]]:
    """Points inside a polygon on a jittered grid, at least one per polygon."""
    wanted = max(1, int(polygon.area // per_tree_units))
    if wanted == 1:
        point = polygon.representative_point()
        return [(point.x, point.y)]

    min_x, min_y, max_x, max_y = polygon.bounds
    step = max(1.0, (polygon.area / wanted) ** 0.5)

    points: list[tuple[float, float]] = []
    for gx in np.arange(min_x, max_x, step):
        for gy in np.arange(min_y, max_y, step):
            jx = gx + rng.uniform(0.15, 0.85) * step
            jy = gy + rng.uniform(0.15, 0.85) * step
            if polygon.contains(Point(jx, jy)):
                points.append((float(jx), float(jy)))
    if not points:
        point = polygon.representative_point()
        return [(point.x, point.y)]
    return points


def ground_height_m(dtm: Raster, tile: Tile, polygon: Polygon) -> float:
    """Median bare-earth elevation under a footprint, in metres."""
    samples = _samples_under(dtm, tile, polygon)
    if samples.size == 0:
        return float(np.nanmedian(dtm.values))
    return float(np.nanmedian(samples))


def object_height_m(dsm: Raster, dtm: Raster, tile: Tile, polygon: Polygon) -> float:
    """P90 of DSM-DTM inside a footprint — the building's height above ground.

    P90 rather than the median because a pitched roof only reaches full height
    along the ridge; the median reads the eaves and makes every house squat.
    """
    surface = _samples_under(dsm, tile, polygon)
    ground = _samples_under(dtm, tile, polygon)
    if surface.size == 0 or ground.size == 0 or surface.shape != ground.shape:
        return 0.0
    delta = surface - ground
    delta = delta[np.isfinite(delta)]
    if delta.size == 0:
        return 0.0
    return float(np.percentile(delta, 90))


def _samples_under(raster: Raster, tile: Tile, polygon: Polygon) -> np.ndarray:
    """Raster values inside a tile-local polygon, masked to its true shape."""
    from rasterio.features import geometry_mask
    from rasterio.transform import Affine

    origin_e, origin_n = tile.origin
    s = float(UNITS_PER_METRE)
    # Inverse of _osgb_to_map_matrix.
    inverse = [1 / s, 0.0, 0.0, 1 / s, origin_e, origin_n]
    osgb_poly = affine_transform(polygon, inverse)

    transform = Affine(raster.pixel_m, 0.0, raster.min_e, 0.0, -raster.pixel_m, raster.max_n)
    try:
        mask = geometry_mask(
            [osgb_poly],
            out_shape=raster.values.shape,
            transform=transform,
            invert=True,  # True *inside* the polygon
            all_touched=False,
        )
    except ValueError:
        return np.empty(0, dtype="float32")

    return raster.values[mask]
