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
    only one sitting in `data/`."""
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    from_env = os.environ.get(EXTRACT_ENV)
    if from_env:
        path = Path(from_env)
        return path if path.exists() else None
    found = sorted(EXTRACT_DIR.glob("*.osm.pbf"))
    return found[0] if found else None


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
    if source == "local":
        return f"osm: local extract {path}"
    return "osm: Overpass API"


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
