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
from shapely.geometry import Polygon, shape

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


def contour_bands(
    dtm: Raster,
    tile: Tile,
    step_m: float = 0.5,
    simplify_m: float = 1.2,
    min_area_m2: float = 30.0,
) -> list[TerrainBand]:
    """Quantise a DTM into absolute elevation bands and polygonise each."""
    from rasterio.features import shapes
    from rasterio.transform import Affine

    if step_m > MAX_CONTOUR_STEP_M:
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

        bands.append(TerrainBand(polygon=poly, elevation_m=float(value) * step_m))

    # Lowest first, so higher ground is laid over lower and wins the overlap.
    bands.sort(key=lambda b: b.elevation_m)
    return bands


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
