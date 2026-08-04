"""Build a whole local authority district into one PK3, in parallel.

    python scripts/build-district.py --district Thanet --size 800 \
        --out out/thanet.pk3 --workers 8

Why this is not just `--region N`: a district is not a square. `build_region`
takes a centre and a radius, holds every `BuiltMap` in memory at once, and
loses the lot if any single tile raises. Thanet is 200 tiles at 800m and Kent
is 5,843, so all three of those stop being acceptable.

The three properties that matter for a run measured in hours:

**Per-tile isolation.** One tile is one subprocess. Not a `ProcessPoolExecutor`
worker: shapely, rasterio and numpy are C extensions, and a hard crash in one
takes the whole pool down with `BrokenProcessPool`, losing every tile in
flight. A subprocess that dies is one tile, recorded and stepped over.
Measured cost of the isolation: interpreter start plus the 120 MB OSM index
read is ~0.3 s against a mean tile of ~39 s, so it is under 1%. Note this
defeats the `lru_cache` on `osmpbf._load`, whose docstring assumes one
long-lived process — that assumption is what the 0.3 s buys out of.

**Resumability.** Each finished tile is a self-contained artifact on disk under
`work/<district>/tiles/<tile id>.zip`, holding its TEXTMAP, its minimap and its
stats. A rerun skips anything already there. Nothing is held in memory between
tiles, so the assembly step is the only place the whole district exists at
once, and it streams straight into the zip.

**A log you can act on.** Every tile appends one JSON line to `progress.jsonl`
with its outcome and how long it took; failures also get their traceback in
`failures.jsonl`. A run that fails 3 tiles out of 200 should tell you which 3,
not die at tile 4.

Tiles come from the ONS district polygon, not a bounding box — see
`sources.boundaries`. A tile with no LIDAR at all is open sea: it is recorded
as skipped and does not become a map, which is the same rule `build_region`
already applies.

    --limit N        build only the first N tiles (for trying the machinery out)
    --assemble-only  skip building, just pack whatever artifacts already exist
    --retry-failed   clear failed tiles from the log and try them again
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from postcode2wad import __version__
from postcode2wad.tiles import Tile, osgb_to_lonlat

UNITS_NOTE = "32 map units = 1 metre; floor heights are absolute metres above ordnance datum"

#: Artifact members. Kept boring on purpose: a half-written artifact from a
#: killed run must be detectable, so the zip is written to a temporary name and
#: renamed into place only once complete.
TEXTMAP_MEMBER = "TEXTMAP"
MINIMAP_MEMBER = "minimap.png"
META_MEMBER = "meta.json"


# --------------------------------------------------------------------------
# planning


def plan_tiles(district: str, size_m: int, cache_dir: Path) -> list[Tile]:
    """Every grid tile the district polygon touches, in row-major order.

    Row-major from the south-west, matching `build_region`'s convention. The
    ZScript navigates by grid index rather than map number so the ordering is
    not load-bearing for correctness — but it keeps neighbouring map numbers
    near each other, which makes a progress log readable as the run sweeps
    north.
    """
    from shapely.geometry import box

    from postcode2wad.sources.boundaries import district_polygon

    polygon = district_polygon(district, cache_dir)
    min_e, min_n, max_e, max_n = polygon.bounds

    tiles = []
    for north in range(int(min_n // size_m) * size_m, int(max_n) + 1, size_m):
        for east in range(int(min_e // size_m) * size_m, int(max_e) + 1, size_m):
            if polygon.intersects(box(east, north, east + size_m, north + size_m)):
                tiles.append(Tile(east // size_m, north // size_m, size_m))
    return tiles


# --------------------------------------------------------------------------
# one tile, in its own process


def build_one_tile(tile: Tile, options: dict, cache_dir: Path, artifact: Path) -> dict:
    """Generate one tile and write its artifact. Runs in the worker process."""
    from postcode2wad.build import build_tile
    from postcode2wad.preview import render_minimap
    from postcode2wad.sources import lidar, postcodes
    from postcode2wad.udmf import emit_textmap

    centre_e = tile.origin[0] + tile.size_m / 2
    centre_n = tile.origin[1] + tile.size_m / 2
    lon, lat = osgb_to_lonlat(centre_e, centre_n)
    place = postcodes.nearest(lat, lon, cache_dir)

    started = time.perf_counter()
    try:
        built = build_tile(
            place, size_m=tile.size_m, cache_dir=cache_dir, tile=tile, **options
        )
    except lidar.NoCoverageError as exc:
        # No LIDAR anywhere in the square means open sea. Over Kent's land the
        # composite is gapless, so this is a positive signal rather than a
        # failure — record it and leave the tile out of the pack.
        meta = {
            "tile": tile.id,
            "ix": tile.ix,
            "iy": tile.iy,
            "size_m": tile.size_m,
            "outcome": "skipped",
            "why": str(exc),
            "seconds": round(time.perf_counter() - started, 2),
        }
        _write_artifact(artifact, meta, textmap=None, minimap=None)
        return meta

    textmap = emit_textmap(
        built.geometry,
        comment=(
            f"postcode2wad {__version__}\n"
            f"tile {tile.id}  origin OSGB {tile.origin[0]}E {tile.origin[1]}N\n"
            f"{UNITS_NOTE}\n"
            "Contains OS data (c) Crown copyright; OpenStreetMap contributors (ODbL);\n"
            "(c) Environment Agency copyright and/or database right."
        ),
    )
    minimap = render_minimap(built.geometry, tile_units=tile.size_units)

    meta = {
        "tile": tile.id,
        "ix": tile.ix,
        "iy": tile.iy,
        "size_m": tile.size_m,
        "outcome": "built",
        "title": built.title,
        "sectors": built.stats.sectors,
        "linedefs": built.stats.linedefs,
        "buildings": built.stats.buildings,
        "trees": built.stats.trees,
        "lidar_local": lidar.TALLY.local,
        "lidar_wcs": lidar.TALLY.wcs,
        "seconds": round(time.perf_counter() - started, 2),
    }
    _write_artifact(artifact, meta, textmap, minimap)
    return meta


def _write_artifact(path: Path, meta: dict, textmap: str | None, minimap: bytes | None) -> None:
    """Write to a temporary name and rename, so a killed run leaves no half-tile.

    A truncated zip that still has the right filename is indistinguishable from
    a finished one to the resume check, and would be packed into the district
    as a corrupt map.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".partial")
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(META_MEMBER, json.dumps(meta, indent=1))
        if textmap is not None:
            zf.writestr(TEXTMAP_MEMBER, textmap)
        if minimap is not None:
            zf.writestr(MINIMAP_MEMBER, minimap)
    os.replace(staging, path)


def read_meta(artifact: Path) -> dict | None:
    """The artifact's metadata, or None if it is missing or unreadable."""
    if not artifact.exists():
        return None
    try:
        with zipfile.ZipFile(artifact) as zf:
            return json.loads(zf.read(META_MEMBER))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# the parallel run


def run_worker(tile: Tile, work: Path, cache_dir: Path, options: dict) -> dict:
    """Spawn one subprocess for one tile and collect its result."""
    artifact = work / "tiles" / f"{tile.id}.zip"
    job = json.dumps(
        {
            "ix": tile.ix,
            "iy": tile.iy,
            "size_m": tile.size_m,
            "cache_dir": str(cache_dir),
            "artifact": str(artifact),
            "options": options,
        }
    )
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", job],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        # A failing tile is data, not an exception: it goes in the failure log
        # and the run carries on to the other 199.
        check=False,
    )
    elapsed = round(time.perf_counter() - started, 2)

    meta = read_meta(artifact)
    if meta is not None:
        meta["wall_seconds"] = elapsed
        return meta

    # No artifact means the child died before writing one — a crash in a C
    # extension, an out-of-memory kill, or a bug. Keep its output: this is the
    # only record of what went wrong.
    return {
        "tile": tile.id,
        "ix": tile.ix,
        "iy": tile.iy,
        "outcome": "failed",
        "returncode": proc.returncode,
        "wall_seconds": elapsed,
        "stderr": (proc.stderr or "")[-4000:],
        "stdout": (proc.stdout or "")[-2000:],
    }


def build_all(tiles: list[Tile], work: Path, cache_dir: Path, options: dict, workers: int) -> None:
    (work / "tiles").mkdir(parents=True, exist_ok=True)
    progress_log = work / "progress.jsonl"
    failure_log = work / "failures.jsonl"

    todo = [t for t in tiles if read_meta(work / "tiles" / f"{t.id}.zip") is None]
    done = len(tiles) - len(todo)
    if done:
        print(f"resuming: {done} of {len(tiles)} tiles already built")
    if not todo:
        print("every tile already built")
        return

    lock = threading.Lock()
    counter = {"n": 0, "failed": 0, "skipped": 0, "seconds": 0.0}
    started = time.perf_counter()

    def record(meta: dict) -> None:
        with lock:
            counter["n"] += 1
            counter["seconds"] += meta.get("wall_seconds", 0.0)
            outcome = meta.get("outcome")
            if outcome == "failed":
                counter["failed"] += 1
                with failure_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(meta) + "\n")
            elif outcome == "skipped":
                counter["skipped"] += 1

            with progress_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in meta.items() if k != "stderr"}) + "\n")

            n = counter["n"]
            # Rate is measured over the wall clock of the run, not the sum of
            # per-tile times, so the estimate accounts for the actual worker
            # count rather than assuming it.
            wall = time.perf_counter() - started
            left = (len(todo) - n) * wall / n
            note = {"built": "", "skipped": "  (sea)", "failed": "  FAILED"}.get(outcome, "")
            print(
                f"  [{n}/{len(todo)}] {meta['tile']:<20} "
                f"{meta.get('wall_seconds', 0):5.1f}s  "
                f"{meta.get('sectors', 0):>6} sectors{note}   "
                f"~{left / 60:.0f} min left",
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Threads here only wait on subprocesses, so the GIL is irrelevant and
        # the real parallelism is in the child processes.
        for meta in pool.map(lambda t: run_worker(t, work, cache_dir, options), todo):
            record(meta)

    wall = time.perf_counter() - started
    print(
        f"\n{counter['n']} tiles in {wall / 60:.1f} min "
        f"({counter['seconds'] / max(counter['n'], 1):.1f}s each, "
        f"{counter['seconds'] / max(wall, 1e-9):.1f}x speedup), "
        f"{counter['skipped']} sea, {counter['failed']} failed"
    )
    if counter["failed"]:
        print(f"failures logged to {failure_log}")


# --------------------------------------------------------------------------
# assembly


def assemble(tiles: list[Tile], work: Path, out: Path, fog: int | None) -> int:
    """Pack every built artifact into one district PK3."""
    from postcode2wad.hud import HANDLER, ZSCRIPT_LUMP, build_zscript, minimap_lump
    from postcode2wad.pk3 import FOG_DENSITY, write_region_pk3
    from postcode2wad.region import map_name

    built: list[tuple[Tile, dict, Path]] = []
    missing = 0
    for tile in tiles:
        artifact = work / "tiles" / f"{tile.id}.zip"
        meta = read_meta(artifact)
        if meta is None:
            missing += 1
            continue
        if meta.get("outcome") != "built":
            continue
        built.append((tile, meta, artifact))

    if not built:
        print("error: no tiles were built", file=sys.stderr)
        return 3
    if missing:
        print(f"warning: {missing} tiles have no artifact and are not in the pack")

    count = len(built)
    names = [map_name(i, count) for i in range(count)]
    digits = len(names[0]) - len("".join(c for c in names[0] if not c.isdigit()))

    maps: list[tuple[str, str, str]] = []
    extra: dict[str, bytes] = {}

    for index, ((tile, meta, artifact), name) in enumerate(zip(built, names)):
        with zipfile.ZipFile(artifact) as zf:
            maps.append((name, meta.get("title") or tile.id, zf.read(TEXTMAP_MEMBER).decode()))
            extra[f"graphics/{minimap_lump(index, digits)}.png"] = zf.read(MINIMAP_MEMBER)

    extra[ZSCRIPT_LUMP] = build_zscript([t for t, _, _ in built], names).encode()

    write_region_pk3(
        out,
        maps,
        fog_density=FOG_DENSITY if fog is None else fog,
        extra_files=extra,
        event_handler=HANDLER,
        tile_id=built[0][0].id,
    )

    sectors = sum(m.get("sectors", 0) for _, m, _ in built)
    lines = sum(m.get("linedefs", 0) for _, m, _ in built)
    size_mb = out.stat().st_size / 1e6
    print(
        f"\n{count} maps ({names[0]}..{names[-1]}), "
        f"{sectors:,} sectors, {lines:,} linedefs, {size_mb:.0f} MB"
    )
    print(f"wrote {out}")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build-district.py",
        description="Build a whole local authority district into one PK3, in parallel.",
    )
    parser.add_argument("--district", help="ONS district name, e.g. Thanet")
    parser.add_argument("--size", type=int, default=800, metavar="METRES")
    parser.add_argument("--out", default=None, metavar="PATH")
    parser.add_argument("--work", default=None, metavar="DIR", help="artifacts and logs")
    parser.add_argument("--cache-dir", default="cache", metavar="DIR")
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) - 1),
        help="parallel tile processes (default: cores - 1)",
    )
    parser.add_argument("--limit", type=int, default=None, help="build only the first N tiles")
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument("--plan-only", action="store_true", help="list the tiles and stop")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--fog", type=int, default=None)
    parser.add_argument("--no-slopes", action="store_true")
    parser.add_argument("--contour-step", type=float, default=None, metavar="METRES")
    parser.add_argument("--worker", metavar="JSON", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        job = json.loads(args.worker)
        tile = Tile(job["ix"], job["iy"], job["size_m"])
        build_one_tile(tile, job["options"], Path(job["cache_dir"]), Path(job["artifact"]))
        return 0

    if not args.district:
        parser.error("give --district")

    cache_dir = Path(args.cache_dir)
    slug = args.district.lower().replace(" ", "-")
    work = Path(args.work or f"work/{slug}-{args.size}m")
    out = Path(args.out or f"out/{slug}.pk3")

    tiles = plan_tiles(args.district, args.size, cache_dir)
    if args.limit:
        tiles = tiles[: args.limit]

    area = len(tiles) * (args.size / 1000) ** 2
    print(
        f"{args.district}: {len(tiles)} tiles at {args.size}m "
        f"({area:.0f} km2 of grid), work dir {work}"
    )
    if args.plan_only:
        for tile in tiles:
            print(f"  {tile.id}  OSGB {tile.origin[0]}E {tile.origin[1]}N")
        return 0

    if args.retry_failed:
        removed = 0
        for tile in tiles:
            meta = read_meta(work / "tiles" / f"{tile.id}.zip")
            if meta is not None and meta.get("outcome") == "failed":
                (work / "tiles" / f"{tile.id}.zip").unlink()
                removed += 1
        print(f"cleared {removed} failed tiles for a retry")

    options = {
        "contour_step_m": args.contour_step,
        "with_slopes": not args.no_slopes,
        "osm_source": "local",
        "lidar_source": "auto",
    }

    if not args.assemble_only:
        work.mkdir(parents=True, exist_ok=True)
        (work / "plan.json").write_text(
            json.dumps(
                {
                    "district": args.district,
                    "size_m": args.size,
                    "tiles": [t.id for t in tiles],
                    "options": options,
                    "version": __version__,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        build_all(tiles, work, cache_dir, options, args.workers)

    return assemble(tiles, work, out, args.fog)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Artifacts already on disk survive; a rerun picks up where this stopped.
        print("\ninterrupted — rerun the same command to resume", file=sys.stderr)
        raise SystemExit(130) from None
