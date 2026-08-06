"""Mirror Environment Agency LIDAR 5km grid tiles into `data/lidar/`.

Why this exists: the WCS is one round trip per map tile, and Kent at 800m is
5,843 of them per coverage. The EA also publishes the same composite as one zip
per 5km National Grid square, and those are *bit-identical* to what the WCS
returns (verified in docs/lidar-bulk.md: max abs diff 0.0). So the county can be
pulled once as ~200 files and every build after that is a windowed read off
disk.

The API is the survey portal's own, reverse-engineered from its JavaScript and
not published on Defra's API portal — undocumented, unversioned, and gated on a
subscription key hardcoded in the SPA. That is an argument for mirroring once
rather than depending on it at build time; `sources/lidar.py` keeps the
documented OGC WCS as its fallback.

    GET /tiles/collections/survey/{product}/{year}/{res}/{tile}?subscription-key=dspui

Responses are `Transfer-Encoding: chunked`, so there is no Content-Length, no
Range and `HEAD` returns 405: you cannot learn a file's size without fetching
it, and there is no manifest to diff against.

**The zip is not always a raster.** `lidar_composite_last_return_dsm` at 1m
returns a metadata-only zip (or an intermittent 404) for every TR tile — an EA
packaging defect, not missing survey data, since the WCS serves the same pixels
fine. So every download is checked for a `.tif` member and a zip without one is
reported as a failure rather than left as a hole someone finds later.

    python scripts/fetch-lidar.py --district Thanet
    python scripts/fetch-lidar.py --tiles TR2565 TR3065
    python scripts/fetch-lidar.py --bbox 628800,168000,631200,170400
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from postcode2wad.sources.lidar import (
    DEFAULT_LIDAR_DIR,
    GRID_TILE_M,
    grid_tile_id,
    grid_tiles_for_bbox,
    local_tile_path,
)

BASE = "https://environment.data.gov.uk/tiles/collections/survey"

#: The web UI's own key, hardcoded in its JS. Without any key the API answers
#: 401; `public` works for search but not for download.
SUBSCRIPTION_KEY = "dspui"

USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 600

def district_tiles(name: str) -> list[str]:
    """Every 5km grid tile a named local authority district touches.

    The boundary itself comes from `sources.boundaries`, shared with
    `build-district.py` so the two agree on what "Thanet" means — a mirror that
    covered a different set of squares than the build wants is a build that
    silently falls back to the network.
    """
    from shapely.geometry import box

    from postcode2wad.sources.boundaries import district_polygon

    try:
        polygon = district_polygon(name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    min_e, min_n, max_e, max_n = polygon.bounds
    tiles = []
    for east in range(int(min_e // GRID_TILE_M) * GRID_TILE_M, int(max_e) + 1, GRID_TILE_M):
        for north in range(int(min_n // GRID_TILE_M) * GRID_TILE_M, int(max_n) + 1, GRID_TILE_M):
            cell = box(east, north, east + GRID_TILE_M, north + GRID_TILE_M)
            if polygon.intersects(cell) and not polygon.intersection(cell).is_empty:
                tiles.append(grid_tile_id(east, north))
    return sorted(set(tiles))


#: Attempts per tile, and the base for exponential backoff between them.
#:
#: Two of these were real transient failures, seen while mirroring Kent:
#:
#:   - one tile arrived as a 0.6 MB unreadable zip and retried clean at 41.6 MB;
#:   - one run died outright on a ChunkedEncodingError mid-download, losing
#:     every remaining tile rather than one.
#:
#: Reported as BAD ZIP and a traceback, those produce a half-mirror that reads
#: as a coverage report, and every build timed against it is then silently
#: network-bound — the exact failure this project keeps paying for.
#:
#: A third was believed to be throttling and was not: a run of 404s that looked
#: like rate limiting turned out to be a carriage return in the tile list, so
#: the ids never matched anything. 404 is retried anyway, because the cost is a
#: few seconds and the alternative is recording a square as missing when it is
#: not — but do not read the retry as evidence the API throttles.
ATTEMPTS = 4
BACKOFF_S = 5.0


def fetch_tile(tile: str, product: str, year: str, res: str, directory: Path) -> tuple[str, str]:
    """Download one grid tile, retrying transient failures. Returns (tile, outcome)."""
    target = local_tile_path(directory, product, year, res, tile)
    if target.exists():
        return tile, f"have {target.stat().st_size / 1e6:.1f} MB"

    url = f"{BASE}/{product}/{year}/{res}/{tile}"
    started = time.time()
    last = "no attempt made"

    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_S * (2 ** (attempt - 1)))
        try:
            response = requests.get(
                url,
                params={"subscription-key": SUBSCRIPTION_KEY},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            # 404 is not proof of absence here — see ATTEMPTS. Retry it like any
            # other transient, and only call it missing once the budget is out.
            if response.status_code in (404, 429, 500, 502, 503, 504):
                last = f"HTTP {response.status_code}"
                continue
            response.raise_for_status()
            payload = response.content
        except requests.RequestException as exc:
            # Covers ChunkedEncodingError/IncompleteRead, which killed a whole
            # run rather than one tile.
            last = f"{type(exc).__name__}"
            continue

        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile:
            last = f"truncated ({len(payload)} bytes)"
            continue

        rasters = [n for n in archive.namelist() if n.lower().endswith(".tif")]
        if not rasters:
            # This one really is terminal: a well-formed zip with no raster is
            # the EA's packaging defect, not a network problem. 72 of Kent's 191
            # squares do this for the 1m last-return DSM. Do not burn retries.
            return tile, f"METADATA-ONLY, no .tif in {archive.namelist()}"

        target.parent.mkdir(parents=True, exist_ok=True)
        data = archive.read(rasters[0])
        target.write_bytes(data)
        elapsed = time.time() - started
        note = f" after {attempt + 1} attempts" if attempt else ""
        return tile, f"{len(data) / 1e6:.1f} MB in {elapsed:.0f}s ({rasters[0]}){note}"

    return tile, f"FAILED after {ATTEMPTS} attempts, last: {last}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tiles", nargs="+", metavar="TR2565", help="explicit grid tile ids")
    parser.add_argument("--district", metavar="NAME", help="every tile an ONS district touches")
    parser.add_argument("--bbox", metavar="minE,minN,maxE,maxN", help="every tile an OSGB box touches")
    parser.add_argument("--product", default="lidar_composite_dtm")
    parser.add_argument("--year", default="2022")
    parser.add_argument("--res", default="1", help="resolution in metres")
    parser.add_argument("--dir", default=str(DEFAULT_LIDAR_DIR), metavar="DIR")
    # 1 is cautious rather than measured, and the story behind it is a warning.
    #
    # A day was spent concluding the API throttles on sustained rate: 4 workers
    # got 0 of 30, 3 got 28 of 30, 1 got the rest. That looks like a textbook
    # limiter and it was entirely coincidence. The real fault was a carriage
    # return in the tile-list file feeding the runs, so every id arrived with a
    # trailing CR: it matches nothing on disk, and the request goes to
    # .../TQ4035%0D, which the API correctly 404s. The runs that appeared to
    # prove low concurrency worked were the ones typed by hand.
    #
    # With that fixed, 195 DTM and 195 DSM squares came down at 1 worker with
    # zero failures, ~10s and ~30s each. Whether 3 or 4 also works is untested.
    #
    # The retries below are still earned: a truncated zip and a mid-download
    # ChunkedEncodingError were both real, and both were reported as permanent.
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.tiles:
        tiles = sorted(set(args.tiles))
    elif args.district:
        tiles = district_tiles(args.district)
    elif args.bbox:
        tiles = grid_tiles_for_bbox(tuple(float(v) for v in args.bbox.split(",")))
    else:
        parser.error("give --tiles, --district or --bbox")

    directory = Path(args.dir)
    print(f"{len(tiles)} tiles: {' '.join(tiles)}")
    print(f"{args.product}/{args.year}/{args.res} -> {directory}")
    if args.dry_run:
        return 0

    started = time.time()
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(fetch_tile, t, args.product, args.year, args.res, directory) for t in tiles
        ]
        for future in futures:
            tile, outcome = future.result()
            print(f"  {tile}  {outcome}", flush=True)
            if outcome.startswith(("MISSING", "METADATA-ONLY", "BAD ZIP")):
                failures.append((tile, outcome))

    print(f"{len(tiles) - len(failures)}/{len(tiles)} tiles in {time.time() - started:.0f}s")
    if failures:
        print(f"\n{len(failures)} tiles have no raster:", file=sys.stderr)
        for tile, outcome in failures:
            print(f"  {tile}  {outcome}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
