"""Environment Agency LIDAR composite, via WCS.

The Defra survey portal is an interactive SPA and the old `downloadapi` is gone,
but the spatial data platform exposes a plain OGC WCS 2.0.1 that will hand you a
float32 GeoTIFF for any British National Grid bbox with no key and no auth.
That is the whole ballgame for terrain.

Two coverages matter:

    DTM  bare earth      -> the ground you walk on
    DSM  last return     -> ground plus whatever is standing on it

DSM minus DTM inside a building footprint is that building's height. Outside
footprints the same difference is trees, which is why the brief only trusts it
within a footprint.

Licence: Open Government Licence v3. Attribution is required — see ATTRIBUTION.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from .cache import cache_path

ATTRIBUTION = "© Environment Agency copyright and/or database right 2022. All rights reserved."

USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 180

DTM = (
    "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs",
    "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m",
)
# Note the asymmetric slug — it is "...-last-return-dsm-1m", not "...-dsm-1m",
# which 404s.
DSM_URL = (
    "https://environment.data.gov.uk/spatialdata/"
    "lidar-composite-digital-surface-model-last-return-dsm-1m/wcs"
)
DSM = (DSM_URL, "9ba4d5ac-d596-445a-9056-dae3ddec0178__Lidar_Composite_Elevation_LZ_DSM_1m")

#: The GeoTIFF's declared NODATA. Anything at or below this is a hole.
NODATA_THRESHOLD = -1e30


@dataclass
class Raster:
    """A north-up grid of elevations in metres, georeferenced in OSGB."""

    values: np.ndarray  # (rows, cols), float32, NaN where no data
    min_e: float
    min_n: float
    max_n: float
    pixel_m: float = 1.0

    @property
    def coverage(self) -> float:
        """Fraction of pixels that carry a real elevation."""
        if self.values.size == 0:
            return 0.0
        return float(np.count_nonzero(np.isfinite(self.values)) / self.values.size)

    def sample(self, easting: float, northing: float) -> float:
        """Nearest-pixel elevation, or NaN outside the raster."""
        col = int((easting - self.min_e) / self.pixel_m)
        row = int((self.max_n - northing) / self.pixel_m)
        if not (0 <= row < self.values.shape[0] and 0 <= col < self.values.shape[1]):
            return float("nan")
        return float(self.values[row, col])

    def window(self, min_e: float, min_n: float, max_e: float, max_n: float) -> np.ndarray:
        """Values inside an OSGB bbox, for statistics over a footprint."""
        c0 = max(0, int((min_e - self.min_e) / self.pixel_m))
        c1 = min(self.values.shape[1], int(np.ceil((max_e - self.min_e) / self.pixel_m)))
        r0 = max(0, int((self.max_n - max_n) / self.pixel_m))
        r1 = min(self.values.shape[0], int(np.ceil((self.max_n - min_n) / self.pixel_m)))
        if r0 >= r1 or c0 >= c1:
            return np.empty(0, dtype="float32")
        return self.values[r0:r1, c0:c1]


def _fetch(coverage: tuple[str, str], bbox_osgb, cache_dir: Path | None, refresh: bool) -> bytes:
    url, coverage_id = coverage
    min_e, min_n, max_e, max_n = bbox_osgb
    key = f"{coverage_id}|{min_e},{min_n},{max_e},{max_n}"
    path = cache_path("lidar", key, cache_dir, suffix=".tif")

    if path.exists() and not refresh:
        return path.read_bytes()

    params = {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "coverageId": coverage_id,
        # Two subset params with the same name — requests handles the repeat
        # when the value is a list.
        "subset": [f"E({min_e},{max_e})", f"N({min_n},{max_n})"],
        "format": "image/tiff",
    }
    response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "tiff" not in content_type:
        raise RuntimeError(f"WCS returned {content_type!r}, not a GeoTIFF: {response.text[:300]}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


def _to_raster(data: bytes) -> Raster:

    from rasterio.io import MemoryFile

    with MemoryFile(data) as memfile, memfile.open() as src:
        values = src.read(1).astype("float32")
        transform = src.transform

    values[values <= NODATA_THRESHOLD] = np.nan
    pixel_m = abs(transform.a)
    min_e = transform.c
    max_n = transform.f
    min_n = max_n - values.shape[0] * pixel_m
    return Raster(values=values, min_e=min_e, min_n=min_n, max_n=max_n, pixel_m=pixel_m)


def fetch_dtm(bbox_osgb, cache_dir: Path | None = None, refresh: bool = False) -> Raster:
    return _to_raster(_fetch(DTM, bbox_osgb, cache_dir, refresh))


def fetch_dsm(bbox_osgb, cache_dir: Path | None = None, refresh: bool = False) -> Raster:
    return _to_raster(_fetch(DSM, bbox_osgb, cache_dir, refresh))


def fill_holes(raster: Raster) -> Raster:
    """Replace NODATA with a plausible height so quantisation doesn't tear.

    LIDAR drops out over water and dark absorbing surfaces. A hole left as NaN
    becomes a missing sector, which reads as a pit in the floor. Filling with
    the surrounding median is crude but visually stable.
    """
    values = raster.values
    holes = ~np.isfinite(values)
    if not holes.any():
        return raster
    if holes.all():
        raise ValueError("raster is entirely NODATA — no LIDAR coverage here")

    filled = values.copy()
    filled[holes] = float(np.nanmedian(values))
    return Raster(
        values=filled,
        min_e=raster.min_e,
        min_n=raster.min_n,
        max_n=raster.max_n,
        pixel_m=raster.pixel_m,
    )
