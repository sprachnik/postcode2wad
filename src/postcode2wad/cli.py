"""Command line entry point.

The argument surface is fixed here up front so the milestones can fill in behind it.
Nothing generates a real map yet — see M0 in docs/brief.md.
"""

from __future__ import annotations

import argparse
import sys

from . import DEFAULT_TILE_SIZE_M, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postcode2wad",
        description="Generate a playable GZDoom level of a real UK place.",
    )
    parser.add_argument("postcode", nargs="?", help='UK postcode, e.g. "CT7 0XX"')
    parser.add_argument(
        "--latlng",
        metavar="LAT,LNG",
        help="use coordinates instead of a postcode",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_TILE_SIZE_M,
        metavar="METRES",
        help=f"tile edge length in metres (default: {DEFAULT_TILE_SIZE_M})",
    )
    parser.add_argument(
        "--out",
        default="out.pk3",
        metavar="PATH",
        help="output PK3 path (default: out.pk3)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.postcode and not args.latlng:
        parser.error("give a postcode or --latlng")

    print("postcode2wad is at M0 (geometry proof) — generation not wired up yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
