"""Environment Agency LIDAR composite, from a local mirror or over WCS.

Two coverages matter:

    DTM  bare earth      -> the ground you walk on
    DSM  last return     -> ground plus whatever is standing on it

DSM minus DTM inside a building footprint is that building's height. Outside
footprints the same difference is trees, which is why the brief only trusts it
within a footprint.

There are two ways to get either one, and they answer identically.

**Local** is the EA's own bulk publication: one LZW-compressed, internally tiled
GeoTIFF per 5km National Grid square under `data/lidar/`, put there by
`scripts/fetch-lidar.py`. A map tile is a windowed read out of it, so an 800m
square costs four 800x800 reads at worst and never decompresses the other 24
million pixels.

**WCS** is the documented OGC service, one HTTP round trip per map tile, cached
under `cache/lidar/`. It is the fallback, not the primary, because Kent at 800m
is 5,843 tiles x 2 coverages = 11,686 round trips.

*The two are bit-identical.* docs/lidar-bulk.md pulled the same 5km window both
ways and measured `max abs diff 0.0` — the WCS emits the same pixels
uncompressed. So falling back cannot introduce a seam or change a generated map,
and that is a measured claim rather than an assumption.

Three sharp edges, all of them load-bearing:

1. **5000 is not a multiple of 800.** A map tile straddles up to four 5km
   squares, so the local path mosaics rather than picking a file.
2. **All-NODATA means sea.** Over Kent's *land* the composite is gapless
   (measured: valid fraction 1.000 on ten inland 5km tiles), so a tile with no
   data at all is offshore. That is `NoCoverageError`, to be skipped
   deliberately — not filled with flat ground, not a crash.
3. **`lidar_composite_last_return_dsm` at 1m has no bulk raster for TR.** All 72
   TR tiles return a metadata-only zip. It is an EA packaging defect, not
   missing survey: the WCS serves the same pixels fine. Since Thanet, Canterbury
   and Dover are all TR, a naive mirror would quietly produce tree-less maps for
   the eastern half of Kent — so `Coverage.broken_squares` names it and the
   fallback says so out loud.

Licence: Open Government Licence v3. Attribution is required — see ATTRIBUTION.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests

from .cache import cache_path

ATTRIBUTION = "© Environment Agency copyright and/or database right 2022. All rights reserved."

USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 180

#: The EA's bulk tiling: 5km squares on the OS National Grid.
GRID_TILE_M = 5000

#: Where the mirror lives. `data/` already holds the OSM extract.
DEFAULT_LIDAR_DIR = Path("data/lidar")
LIDAR_DIR_ENV = "POSTCODE2WAD_LIDAR_DIR"

SOURCES = ("auto", "local", "wcs")


@dataclass(frozen=True)
class Coverage:
    """One EA elevation product, addressable both ways."""

    label: str
    wcs_url: str
    coverage_id: str
    #: The bulk API's product/year/resolution triple, which is also the layout
    #: of the local mirror.
    product: str
    year: str
    resolution: str
    #: 100km squares for which the bulk publication has no raster. Not a
    #: coverage gap — a packaging defect; the WCS has the data.
    broken_squares: frozenset[str] = field(default_factory=frozenset)


DTM = Coverage(
    label="DTM",
    wcs_url=(
        "https://environment.data.gov.uk/spatialdata/"
        "lidar-composite-digital-terrain-model-dtm-1m/wcs"
    ),
    coverage_id="13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m",
    product="lidar_composite_dtm",
    year="2022",
    resolution="1",
)

# Note the asymmetric slug — it is "...-last-return-dsm-1m", not "...-dsm-1m",
# which 404s.
DSM = Coverage(
    label="DSM",
    wcs_url=(
        "https://environment.data.gov.uk/spatialdata/"
        "lidar-composite-digital-surface-model-last-return-dsm-1m/wcs"
    ),
    coverage_id="9ba4d5ac-d596-445a-9056-dae3ddec0178__Lidar_Composite_Elevation_LZ_DSM_1m",
    product="lidar_composite_last_return_dsm",
    year="2022",
    resolution="1",
    # Measured, twice: docs/lidar-bulk.md probed all 191 Kent tiles and found
    # every one of the 72 TR tiles metadata-only, and this was re-probed over
    # Thanet's 12 tiles before the mirror was built. Every TQ tile is fine.
    broken_squares=frozenset({"TR"}),
)

#: The *first* return surface, and the way out of the TR packaging defect.
#:
#: Probed 5 Aug: the bulk publication has real rasters for TR where the
#: last-return product has none — TR3065 41.9 MB, TR3570 19.6 MB, TR2565
#: 41.6 MB, all with a .tif, against metadata-only zips for the same three
#: squares under `lidar_composite_last_return_dsm`. So eastern Kent can be
#: mirrored with this and does not have to go one WCS round trip per tile.
#:
#: It is a different surface, not a different packaging of the same one: first
#: return is the top of whatever the pulse hit first, so it sits on tree canopy
#: rather than through it. For building heights (P90 of DSM-DTM inside a
#: footprint) that is arguably the more correct surface; for the tree finder,
#: which thresholds DSM-DTM, it reads canopy more aggressively. Do not switch
#: the default without the A/B — see docs/lidar-bulk.md.
FIRST_RETURN_DSM = Coverage(
    label="DSM(first)",
    wcs_url=(
        "https://environment.data.gov.uk/spatialdata/"
        "lidar-composite-digital-surface-model-first-return-dsm-1m/wcs"
    ),
    coverage_id="df4e3ec3-315e-48aa-aaaf-b5ae74d7b2bb__Lidar_Composite_Elevation_FZ_DSM_1m",
    product="lidar_composite_first_return_dsm",
    year="2022",
    resolution="1",
)

#: The GeoTIFF's declared NODATA. Anything at or below this is a hole.
NODATA_THRESHOLD = -1e30

#: 'I' is not used in the National Grid letter scheme.
_GRID_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


class NoCoverageError(ValueError):
    """No LIDAR anywhere in this tile — which over England means sea.

    Deliberately its own type so a bulk run can skip the tile and carry on,
    while a single-tile run can say "that postcode is in the sea" instead of
    dying inside numpy.
    """


def grid_square(easting: float, northing: float) -> str:
    """The two-letter 100km National Grid square containing a point."""
    e100k, n100k = int(easting // 100000), int(northing // 100000)
    if not (0 <= e100k <= 6 and 0 <= n100k <= 12):
        raise ValueError(f"{easting},{northing} is outside the British National Grid")
    first = 19 - n100k - (19 - n100k) % 5 + (e100k + 10) // 5
    second = (19 - n100k) * 5 % 25 + e100k % 5
    return _GRID_LETTERS[first] + _GRID_LETTERS[second]


def grid_tile_id(easting: float, northing: float) -> str:
    """The 5km bulk tile containing a point, e.g. TR2565.

    Both offsets are kilometres inside the 100km square, two digits, always a
    multiple of 5. TR2565 is therefore E 625000-630000, N 165000-170000 — the
    tile id is arithmetic on the corner, so a lookup needs no manifest.
    """
    east = math.floor(easting / GRID_TILE_M) * GRID_TILE_M
    north = math.floor(northing / GRID_TILE_M) * GRID_TILE_M
    return f"{grid_square(east, north)}{east % 100000 // 1000:02d}{north % 100000 // 1000:02d}"


def grid_cells(bbox_osgb) -> list[tuple[str, int, int]]:
    """(tile id, origin E, origin N) for every 5km tile a bbox touches.

    The bbox is half-open on its north and east edges, because that is what a
    raster window is: an 800m tile at E 625000-625800 needs one 5km square, and
    one at E 629600-630400 needs two. Getting this wrong the other way costs a
    file read per tile forever and, when the extra tile is offshore and absent,
    a spurious fallback to the network.
    """
    min_e, min_n, max_e, max_n = bbox_osgb
    east = math.floor(min_e / GRID_TILE_M) * GRID_TILE_M
    cells: list[tuple[str, int, int]] = []
    while east < max_e:
        north = math.floor(min_n / GRID_TILE_M) * GRID_TILE_M
        while north < max_n:
            cells.append((grid_tile_id(east, north), east, north))
            north += GRID_TILE_M
        east += GRID_TILE_M
    return cells


def grid_tiles_for_bbox(bbox_osgb) -> list[str]:
    return [tile for tile, _, _ in grid_cells(bbox_osgb)]


def lidar_dir(explicit: str | Path | None = None) -> Path:
    """Where the mirror is: the directory named, the environment's, or default."""
    if explicit:
        return Path(explicit)
    from_env = os.environ.get(LIDAR_DIR_ENV)
    return Path(from_env) if from_env else DEFAULT_LIDAR_DIR


def local_tile_path(
    directory: Path, product: str, year: str, resolution: str, tile: str
) -> Path:
    """`data/lidar/lidar_composite_dtm/2022/1m/TR/TR2565.tif`.

    Keyed by product, vintage and resolution so switching any of them is a new
    directory rather than a store full of silently mixed pixels.
    """
    return Path(directory) / product / year / f"{resolution}m" / tile[:2] / f"{tile}.tif"


def coverage_tile_path(coverage: Coverage, tile: str, directory: Path | None = None) -> Path:
    return local_tile_path(
        lidar_dir(directory), coverage.product, coverage.year, coverage.resolution, tile
    )


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


@dataclass
class Tally:
    """How each request was served, for the end of a long run.

    A 5,843-tile county build that silently answered every request over the
    network would look exactly like a successful one until it had taken a week.
    So the fallback is counted, the first one of each kind is shouted at stderr,
    and `note()` goes in the build's own summary.
    """

    local: int = 0
    wcs: int = 0
    announced: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.local = 0
        self.wcs = 0
        self.announced.clear()

    def announce(self, key: str, message: str) -> None:
        """Shout once per distinct reason, then stay quiet about it."""
        if key in self.announced:
            return
        self.announced.add(key)
        print(f"LIDAR FALLBACK: {message}", file=sys.stderr, flush=True)

    def note(self) -> str:
        total = self.local + self.wcs
        if not total:
            return "lidar: nothing fetched"
        if not self.wcs:
            return f"lidar: {self.local}/{total} local"
        return f"lidar: {self.local}/{total} local, {self.wcs} over WCS (network fallback)"


TALLY = Tally()


def _mosaic_local(coverage: Coverage, bbox_osgb, directory: Path | None) -> Raster | None:
    """Read a bbox out of the local 5km mirror, or None if it is not all there.

    None rather than an exception because "not mirrored" is an ordinary state —
    it is what a fresh checkout looks like — and the caller's answer to it is
    the WCS, not a failure. A *partial* answer is never returned: half a tile
    off disk and half over the network would be correct here (the two are
    bit-identical) but it would hide a half-finished mirror indefinitely.
    """
    cells = grid_cells(bbox_osgb)
    paths = [(tile, coverage_tile_path(coverage, tile, directory), e, n) for tile, e, n in cells]
    missing = [tile for tile, path, _, _ in paths if not path.exists()]
    if missing:
        # Once per distinct 5km square, not once per map tile: a county run
        # crossing an unmirrored square would otherwise print the same line
        # thirty-nine times, and the line that matters is *which square*.
        for tile in missing:
            square = tile[:2]
            if square in coverage.broken_squares:
                TALLY.announce(
                    f"{coverage.product}/{square}",
                    f"{coverage.label} has no bulk raster for the {square} square. The EA "
                    f"publishes {coverage.product} at {coverage.resolution}m as a "
                    f"metadata-only zip for every {square} tile — a packaging defect, not "
                    f"missing survey data, and the WCS serves the same pixels. "
                    f"Serving {coverage.label} over the network for all of {square}.",
                )
            else:
                TALLY.announce(
                    f"{coverage.product}/{tile}",
                    f"{coverage.label} square {tile} is not in {lidar_dir(directory)}, "
                    f"so every map tile touching it goes over the network. Run: "
                    f"python scripts/fetch-lidar.py --tiles {tile} "
                    f"--product {coverage.product}",
                )
        return None

    import rasterio
    from rasterio.windows import Window

    min_e, min_n, max_e, max_n = (float(v) for v in bbox_osgb)
    values: np.ndarray | None = None
    pixel_m = 1.0

    for tile, path, origin_e, origin_n in paths:
        with rasterio.open(path) as src:
            transform = src.transform
            pixel = abs(transform.a)
            src_min_e, src_max_n = transform.c, transform.f
            # A file in the wrong place would paste real elevations at the
            # wrong coordinates, which is invisible in the output and wrong
            # everywhere. Cheap to check, so check.
            if round(src_min_e) != origin_e or round(src_max_n) != origin_n + GRID_TILE_M:
                raise ValueError(
                    f"{path} says its corner is {src_min_e},{src_max_n} but {tile} "
                    f"is {origin_e},{origin_n + GRID_TILE_M}"
                )
            if values is None:
                pixel_m = pixel
                rows = round((max_n - min_n) / pixel_m)
                cols = round((max_e - min_e) / pixel_m)
                values = np.full((rows, cols), np.nan, dtype="float32")
            elif pixel != pixel_m:
                raise ValueError(f"{path} is {pixel}m but the mosaic is {pixel_m}m")

            # The overlap between what we want and what this file holds.
            over_min_e = max(min_e, origin_e)
            over_max_e = min(max_e, origin_e + GRID_TILE_M)
            over_min_n = max(min_n, origin_n)
            over_max_n = min(max_n, origin_n + GRID_TILE_M)
            width = round((over_max_e - over_min_e) / pixel_m)
            height = round((over_max_n - over_min_n) / pixel_m)
            if width <= 0 or height <= 0:
                continue

            patch = src.read(
                1,
                window=Window(
                    round((over_min_e - src_min_e) / pixel_m),
                    round((src_max_n - over_max_n) / pixel_m),
                    width,
                    height,
                ),
            )

        row = round((max_n - over_max_n) / pixel_m)
        col = round((over_min_e - min_e) / pixel_m)
        values[row : row + height, col : col + width] = patch.astype("float32")

    if values is None:
        return None
    values[values <= NODATA_THRESHOLD] = np.nan
    TALLY.local += 1
    return Raster(values=values, min_e=min_e, min_n=min_n, max_n=max_n, pixel_m=pixel_m)


def _fetch(coverage: Coverage, bbox_osgb, cache_dir: Path | None, refresh: bool) -> bytes:
    min_e, min_n, max_e, max_n = bbox_osgb
    key = f"{coverage.coverage_id}|{min_e},{min_n},{max_e},{max_n}"
    path = cache_path("lidar", key, cache_dir, suffix=".tif")

    if path.exists() and not refresh:
        return path.read_bytes()

    params = {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "coverageId": coverage.coverage_id,
        # Two subset params with the same name — requests handles the repeat
        # when the value is a list.
        "subset": [f"E({min_e},{max_e})", f"N({min_n},{max_n})"],
        "format": "image/tiff",
    }
    response = requests.get(
        coverage.wcs_url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )
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


def fetch(
    coverage: Coverage,
    bbox_osgb,
    cache_dir: Path | None = None,
    refresh: bool = False,
    source: str = "auto",
    directory: str | Path | None = None,
) -> Raster:
    """Local mirror first, WCS second. Identical pixels either way.

    `source="local"` refuses the network, which is what a county run wants:
    better to stop than to spend a week discovering the mirror was incomplete.
    `source="wcs"` refuses the mirror, which is what the equivalence test wants.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown LIDAR source {source!r}; pick one of {SOURCES}")

    if source != "wcs":
        raster = _mosaic_local(coverage, bbox_osgb, directory)
        if raster is not None:
            return raster
        if source == "local":
            raise RuntimeError(
                f"{coverage.label} for {tuple(bbox_osgb)} is not in "
                f"{lidar_dir(directory)} and --lidar-source local forbids the WCS"
            )

    TALLY.wcs += 1
    return _to_raster(_fetch(coverage, bbox_osgb, cache_dir, refresh))


def fetch_dtm(
    bbox_osgb,
    cache_dir: Path | None = None,
    refresh: bool = False,
    source: str = "auto",
    directory: str | Path | None = None,
) -> Raster:
    return fetch(DTM, bbox_osgb, cache_dir, refresh, source, directory)


def fetch_dsm(
    bbox_osgb,
    cache_dir: Path | None = None,
    refresh: bool = False,
    source: str = "auto",
    directory: str | Path | None = None,
) -> Raster:
    return fetch(DSM, bbox_osgb, cache_dir, refresh, source, directory)


def fill_holes(raster: Raster, where: str | None = None) -> Raster:
    """Replace NODATA with a plausible height so quantisation doesn't tear.

    LIDAR drops out over water and dark absorbing surfaces. A hole left as NaN
    becomes a missing sector, which reads as a pit in the floor. Filling with
    the surrounding median is crude but visually stable.

    A raster that is *entirely* NODATA is a different thing and gets a different
    answer. Over Kent's land the composite is gapless — ten inland 5km tiles all
    measured a valid fraction of exactly 1.000 — so no data at all means the
    tile is offshore. Filling it would lay a flat plate of ground on the sea,
    which is worse than not building it, so the caller is told and skips it.
    """
    values = raster.values
    holes = ~np.isfinite(values)
    if not holes.any():
        return raster
    if holes.all():
        raise NoCoverageError(
            f"no LIDAR anywhere in {where or 'this tile'} — it is sea, not ground"
        )

    filled = values.copy()
    filled[holes] = float(np.nanmedian(values))
    return Raster(
        values=filled,
        min_e=raster.min_e,
        min_n=raster.min_n,
        max_n=raster.max_n,
        pixel_m=raster.pixel_m,
    )
