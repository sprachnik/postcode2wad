"""Which OSM source answers a tile query.

There are two, and they answer identically: `overpass` over the network, and
`osmpbf` off a local Geofabrik extract. Everything downstream takes a
`TileFeatures`, so the choice is confined to this module.

The default is `auto`: use the extract when there is one and it covers the
query, otherwise Overpass. That ordering is the point of having a local source
at all — Overpass rate-limits at around nine tiles, so a county has to come off
disk — while the fallback is what keeps a single-tile run working on a machine
that has never downloaded a .pbf.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imports here cost a `requests` import; the CLI reads SOURCES
    from .overpass import TileFeatures

#: Where a .osm.pbf is looked for when none is named.
EXTRACT_DIR = Path("data")
EXTRACT_ENV = "POSTCODE2WAD_OSM_EXTRACT"

SOURCES = ("auto", "local", "overpass")


def find_extract(explicit: str | Path | None = None) -> Path | None:
    """The extract to use: the one named, the one in the environment, or the
    widest one sitting in `data/`.

    Widest, not first. This used to take `sorted(glob(...))[0]`, which is
    alphabetical — with both `england-latest.osm.pbf` and `kent-latest.osm.pbf`
    present it picked England because "e" precedes "k". That was the right file
    for the wrong reason, and the reason would have reversed the day someone
    added `cornwall-latest`.

    Choosing wrong here is invisible. A too-small extract still `covers()` the
    query, still returns features, and still builds a map — it just has nothing
    outside its own boundary. Measured on Sevenoaks' western edge (5 Aug), the
    Kent extract gave 2 buildings and 22 roads where England and Overpass both
    gave 7 and 40; the interior control was identical across all three. So the
    failure mode is a tile that is quietly two thirds empty along a county
    border, with no error anywhere.

    File size is a proxy for coverage, and a good one among Geofabrik extracts:
    they are the same format at the same density, so bigger means more ground.
    It is only consulted when nothing explicit was asked for, and the choice is
    reported by the caller.
    """
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    from_env = os.environ.get(EXTRACT_ENV)
    if from_env:
        path = Path(from_env)
        return path if path.exists() else None
    found = sorted(EXTRACT_DIR.glob("*.osm.pbf"), key=lambda p: p.stat().st_size)
    return found[-1] if found else None


def choose(
    bbox: tuple[float, float, float, float],
    source: str = "auto",
    extract: str | Path | None = None,
    cache_dir: Path | None = None,
) -> tuple[str, Path | None]:
    """Resolve ("overpass" | "local", extract path) for one query."""
    if source not in SOURCES:
        raise ValueError(f"unknown OSM source {source!r}; pick one of {SOURCES}")

    if source == "overpass":
        return "overpass", None

    from . import osmpbf

    path = find_extract(extract)
    if path is None:
        if source == "local":
            raise osmpbf.ExtractError(
                f"no .osm.pbf extract found (looked at {extract or EXTRACT_ENV} "
                f"and {EXTRACT_DIR}/*.osm.pbf)"
            )
        return "overpass", None

    if not osmpbf.covers(path, bbox, cache_dir):
        if source == "local":
            raise osmpbf.ExtractError(
                f"{path.name} covers "
                f"{osmpbf.extract_bbox(str(path), str(cache_dir) if cache_dir else None)}, "
                f"which does not contain {bbox}"
            )
        return "overpass", None

    return "local", path


def describe(source: str, path: Path | None) -> str:
    if source != "local":
        return "osm: Overpass API"
    # Name the losers too when there was a choice. Which extract was used
    # decides whether a border tile is complete or two thirds empty, and the
    # difference is invisible in the output, so it belongs in the build's notes
    # rather than only in this module's logic.
    others = [p.name for p in EXTRACT_DIR.glob("*.osm.pbf") if path and p.name != path.name]
    if others:
        return f"osm: local extract {path} (widest of {len(others) + 1}; also saw {', '.join(sorted(others))})"
    return f"osm: local extract {path}"


def fetch_tile(
    bbox: tuple[float, float, float, float],
    cache_dir: Path | None = None,
    refresh: bool = False,
    selectors: list[str] | None = None,
    source: str = "auto",
    extract: str | Path | None = None,
) -> TileFeatures:
    chosen, path = choose(bbox, source, extract, cache_dir)
    if chosen == "local":
        from . import osmpbf

        return osmpbf.fetch_tile(bbox, cache_dir, refresh, selectors, pbf=path)

    from . import overpass

    return overpass.fetch_tile(bbox, cache_dir, refresh, selectors)
