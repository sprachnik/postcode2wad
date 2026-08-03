"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import DEFAULT_TILE_SIZE_M, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postcode2wad",
        description="Generate a playable GZDoom level of a real UK place.",
        epilog=(
            "Data: OpenStreetMap (ODbL) and Environment Agency LIDAR (OGL v3). "
            "Play with: gzdoom -iwad freedoom2.wad -file OUT.pk3 +map MAP01"
        ),
    )
    parser.add_argument("postcode", nargs="?", help='UK postcode, e.g. "CT1 2EH"')
    parser.add_argument("--latlng", metavar="LAT,LNG", help="use coordinates instead of a postcode")
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_TILE_SIZE_M,
        metavar="METRES",
        help=f"tile edge length in metres (default: {DEFAULT_TILE_SIZE_M})",
    )
    parser.add_argument("--out", default="out.pk3", metavar="PATH", help="output PK3 path")
    parser.add_argument(
        "--contour-step",
        type=float,
        default=0.5,
        metavar="METRES",
        help=(
            "terrain quantisation step; larger means fewer sectors but must stay "
            "at or below 0.75 or the player cannot climb it (default: 0.5)"
        ),
    )
    parser.add_argument(
        "--no-terrain", action="store_true", help="skip LIDAR, generate flat ground"
    )
    parser.add_argument("--no-roads", action="store_true", help="skip roads and paths")
    parser.add_argument("--no-water", action="store_true", help="skip water bodies")
    parser.add_argument("--no-trees", action="store_true", help="skip tree sprites")
    parser.add_argument(
        "--no-barriers", action="store_true", help="skip hedges, fences and garden walls"
    )
    parser.add_argument(
        "--no-landuse", action="store_true", help="skip land cover; leave all ground as grass"
    )
    parser.add_argument("--refresh", action="store_true", help="bypass the cache and refetch")
    parser.add_argument(
        "--cache-dir", default="cache", metavar="DIR", help="where to cache API responses"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="generate the M0 synthetic scene instead of a real place",
    )
    parser.add_argument(
        "--preview", metavar="PNG", help="also write a top-down plan view of the map"
    )
    parser.add_argument(
        "--fog",
        type=int,
        default=None,
        metavar="DENSITY",
        help="distance haze strength; 0 disables it (default: tuned for a 400m tile)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _run_demo(args) -> int:
    from .demo import build_m0_geometry
    from .pk3 import write_pk3
    from .udmf import emit_textmap

    geo = build_m0_geometry(size_m=float(args.size))
    textmap = emit_textmap(geo, comment="postcode2wad M0 synthetic proof scene")
    out = write_pk3(args.out, textmap, title="M0 Geometry Proof")
    print(geo.stats())
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.demo:
        return _run_demo(args)

    if not args.postcode and not args.latlng:
        parser.error("give a postcode or --latlng")

    from .build import build_tile
    from .pk3 import write_pk3
    from .sources import postcodes
    from .udmf import emit_textmap

    cache_dir = Path(args.cache_dir)

    try:
        if args.latlng:
            lat, lon = (float(v) for v in args.latlng.split(","))
            place = postcodes.from_latlng(lat, lon)
        else:
            place = postcodes.lookup(args.postcode, cache_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"{place.label}  ({place.lat:.5f}, {place.lon:.5f})  OSGB {place.easting:.0f}E {place.northing:.0f}N"
    )

    try:
        built = build_tile(
            place,
            size_m=args.size,
            cache_dir=cache_dir,
            refresh=args.refresh,
            contour_step_m=args.contour_step,
            with_terrain=not args.no_terrain,
            with_roads=not args.no_roads,
            with_water=not args.no_water,
            with_landuse=not args.no_landuse,
            with_barriers=not args.no_barriers,
            with_trees=not args.no_trees,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print(f"tile {built.tile.id}  ({args.size}m square)")
    print(built.stats.summary())

    textmap = emit_textmap(
        built.geometry,
        comment=(
            f"postcode2wad {__version__}\n"
            f"tile {built.tile.id}  origin OSGB {built.tile.origin[0]}E {built.tile.origin[1]}N\n"
            f"{UNITS_NOTE}\n"
            "Contains OS data (c) Crown copyright; OpenStreetMap contributors (ODbL);\n"
            "(c) Environment Agency copyright and/or database right."
        ),
    )
    from .pk3 import FOG_DENSITY

    out = write_pk3(
        args.out,
        textmap,
        title=built.title,
        fog_density=FOG_DENSITY if args.fog is None else args.fog,
    )
    print(f"wrote {out}")

    if args.preview:
        from .preview import render

        preview = render(built.geometry, args.preview, tile_units=built.tile.size_units)
        print(f"wrote {preview}")

    return 0


UNITS_NOTE = "32 map units = 1 metre; floor heights are absolute metres above ordnance datum"


if __name__ == "__main__":
    raise SystemExit(main())
