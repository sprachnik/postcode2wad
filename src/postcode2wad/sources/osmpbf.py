"""OpenStreetMap vector data from a local .osm.pbf extract.

Answers exactly the question `overpass.fetch_tile` answers — "every way of
these kinds touching this bbox" — off the disk instead of over the network.

This is not an optimisation. Overpass is donated infrastructure and starts
returning 429 after roughly nine tiles; Kent at 800m is about 5,800 tiles. The
network path cannot generate a county at all, so the county has to come from a
file.

The cost model is the whole design. A .osm.pbf is a stream, not a database:
answering one tile by scanning it means reading 776,021 ways to keep a few
hundred, which at 39 seconds a pass is *worse* than the 131 seconds Overpass
takes for an 800m tile once you have paid it 5,800 times. So the scan happens
once, into an index under `cache/`, and every tile after that is a seek:

  records.bin   each way as (id, coordinates, tags), written once
  bbox.bin      every way's bounding box, int32, loaded whole and tested with
                numpy — this is what avoids decoding records we don't want
  cells.bin     a 0.01-degree grid, cell -> the ways whose bbox touches it
  meta.json     what was indexed, and from which file

A way is kept when its *linework* meets the query rectangle, which is Overpass's
own rule for a bbox query. Containment deliberately does not count: a farm whose
polygon swallows the tile without a single node or segment inside it is not
returned by Overpass, and returning it here would carpet the tile in one land
parcel that the Overpass build does not have.

Known gap, shared with the Overpass path on purpose: multipolygon *relations*
contribute nothing. `out geom;` gives a relation per-member geometry and no
top-level geometry, so `overpass.fetch_tile` has always skipped them, and this
skips them too. Fixing it belongs in one place, for both sources, not here.
"""

from __future__ import annotations

import array
import json
import pickle
import re
import shutil
import struct
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, box

from . import cache
from .overpass import TILE_SELECTORS, Feature, TileFeatures, sort_features

#: Bump this when the on-disk layout changes or when a different set of ways
#: gets indexed. It is part of the cache key, so a bump simply orphans the old
#: index rather than reading it as if it were the new one.
INDEX_VERSION = 1

#: Grid cell for the spatial index, in degrees — about 1.1 km north-south, 700 m
#: east-west at this latitude. Chosen against the tile: an 800 m tile spans at
#: most 3x2 cells, so a query reads a handful of candidate lists, while a cell
#: still holds few enough ways that the bbox pass over them is trivial.
CELL_DEG = 0.01

#: OSM stores coordinates as signed 1e-7 degrees and pyosmium hands them over as
#: exactly that integer, so nothing is rounded anywhere in this module. The
#: doubles that come out the far end are bit-identical to the ones Overpass's
#: 7-decimal JSON parses to.
SCALE = 10_000_000

#: id, point count, tag-block length. Coordinates and tags follow.
_RECORD = struct.Struct("<qII")

_SELECTOR_RE = re.compile(r'^(node|way|relation)\["([^"]+)"(?:="([^"]+)")?\]$')

#: Indexes this process has already rebuilt for `refresh=True`.
_REFRESHED: set[str] = set()


class ExtractError(RuntimeError):
    """The extract cannot answer this query."""


@dataclass(frozen=True)
class Selector:
    """One Overpass selector, e.g. `way["waterway"="riverbank"]`.

    Parsed rather than reimplemented so `overpass.TILE_SELECTORS` stays the one
    definition of what a tile needs. If the two sources are asked different
    questions they will give different answers, and the difference will show up
    as a missing hedge in a map nobody diffed.
    """

    kind: str
    key: str
    value: str | None = None

    def matches(self, kind: str, tags: dict[str, str]) -> bool:
        if kind != self.kind:
            return False
        if self.value is None:
            return self.key in tags
        return tags.get(self.key) == self.value

    def text(self) -> str:
        if self.value is None:
            return f'{self.kind}["{self.key}"]'
        return f'{self.kind}["{self.key}"="{self.value}"]'


def parse_selectors(selectors: list[str]) -> list[Selector]:
    parsed = []
    for text in selectors:
        match = _SELECTOR_RE.match(text.strip())
        if not match:
            raise ExtractError(
                f"cannot read the selector {text!r} against a local extract; "
                "only way/relation tag selectors are supported"
            )
        kind, key, value = match.groups()
        parsed.append(Selector(kind=kind, key=key, value=value))
    return parsed


@lru_cache(maxsize=8)
def extract_bbox(pbf: str, cache_dir: str | None = None) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of the extract, or None if it will not say.

    The .pbf header box first, because it is free and Geofabrik always sets it.
    A header box is optional in the format, though, and `osmium extract` output
    can arrive without one — so an index that has already been built is asked
    for the extent of the data it actually found. Only when neither knows does
    this give up, and the caller then has to assume the file is relevant.
    """
    import osmium

    header_box = osmium.io.Reader(pbf).header().box()
    if header_box.valid():
        low, high = header_box.bottom_left, header_box.top_right
        return low.lat, low.lon, high.lat, high.lon

    meta = Path(index_dir(Path(pbf), Path(cache_dir) if cache_dir else None)) / "meta.json"
    if meta.exists():
        data = json.loads(meta.read_text(encoding="utf-8")).get("data_bbox")
        if data:
            return tuple(data)
    return None


def covers(
    pbf: Path, bbox: tuple[float, float, float, float], cache_dir: Path | None = None
) -> bool:
    """Is this query wholly inside the extract's own bounding box?

    Only a bounding box, so this is necessary and not sufficient: Geofabrik cuts
    to a boundary polygon, and a tile inside the box but outside the polygon
    comes back empty rather than short. It catches the case that actually
    happens — asking a Kent file about Cardiff — and the alternative, an empty
    map with no complaint, is the worst possible failure here.
    """
    extent = extract_bbox(str(pbf), str(cache_dir) if cache_dir else None)
    if extent is None:
        return True  # nothing says otherwise; better than silently not using it
    south, west, north, east = bbox
    e_south, e_west, e_north, e_east = extent
    return e_south <= south and e_west <= west and north <= e_north and east <= e_east


def index_dir(pbf: Path, cache_dir: Path | None = None) -> Path:
    """Where this extract's index lives.

    Keyed on size and mtime as well as path, so replacing the .pbf with a newer
    Geofabrik download builds a new index instead of quietly serving last
    month's roads out of the old one.
    """
    stat = pbf.stat()
    key = f"{pbf.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|v{INDEX_VERSION}"
    return cache.cache_path("osmpbf", key, cache_dir, suffix="")


def build_index(
    pbf: Path,
    cache_dir: Path | None = None,
    selectors: list[str] | None = None,
    progress=None,
) -> Path:
    """Scan the extract once and write the index. Minutes, then never again."""
    import osmium

    wanted = parse_selectors(selectors or TILE_SELECTORS)
    # KeyFilter is a cheap C-side reject on the whole file; `wanted` then makes
    # the exact decision, so `waterway=stream` does not get indexed just because
    # `waterway=riverbank` is asked for.
    keys = sorted({selector.key for selector in wanted})

    target = index_dir(pbf, cache_dir)
    staging = target.with_name(target.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    started = time.time()
    offsets = array.array("Q", [0])
    bboxes = array.array("i")
    cells: dict[tuple[int, int], array.array] = {}
    incomplete = 0
    position = 0

    with open(staging / "records.bin", "wb") as records:
        processor = (
            osmium.FileProcessor(str(pbf))
            .with_locations("flex_mem")
            .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
            .with_filter(osmium.filter.KeyFilter(*keys))
        )
        for way in processor:
            tags = dict(way.tags)
            if not any(selector.matches("way", tags) for selector in wanted):
                continue

            coords = array.array("i")
            broken = False
            for node in way.nodes:
                location = node.location
                if not location.valid():
                    # A way whose nodes are not all in the extract would come
                    # out with a straight line across the missing part, which
                    # renders as a real wall in a real map. Drop it and count
                    # it; Geofabrik ships complete ways, so this should be 0.
                    broken = True
                    break
                coords.append(location.x)
                coords.append(location.y)
            if broken:
                incomplete += 1
                continue
            if len(coords) < 4:  # fewer than two points is not a line
                continue

            xs, ys = coords[0::2], coords[1::2]
            index = len(offsets) - 1
            bboxes.extend((min(xs), min(ys), max(xs), max(ys)))

            blob = json.dumps(tags, separators=(",", ":")).encode("utf-8")
            records.write(_RECORD.pack(way.id, len(coords) // 2, len(blob)))
            records.write(coords.tobytes())
            records.write(blob)
            position += _RECORD.size + len(coords) * 4 + len(blob)
            offsets.append(position)

            step = int(CELL_DEG * SCALE)
            for cx in range(min(xs) // step, max(xs) // step + 1):
                for cy in range(min(ys) // step, max(ys) // step + 1):
                    cells.setdefault((cx, cy), array.array("i")).append(index)

            if progress and index % 100_000 == 0 and index:
                progress(index)

    (staging / "bbox.bin").write_bytes(bboxes.tobytes())
    (staging / "offsets.bin").write_bytes(offsets.tobytes())
    (staging / "cells.bin").write_bytes(pickle.dumps(cells, protocol=5))
    (staging / "meta.json").write_text(
        json.dumps(
            {
                "version": INDEX_VERSION,
                "pbf": str(pbf.resolve()),
                "size": pbf.stat().st_size,
                "mtime": int(pbf.stat().st_mtime),
                "cell_deg": CELL_DEG,
                "ways": len(offsets) - 1,
                "cells": len(cells),
                "data_bbox": _data_bbox(bboxes),
                "incomplete_ways": incomplete,
                "selectors": [selector.text() for selector in wanted],
                "seconds": round(time.time() - started, 1),
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    if target.exists():
        shutil.rmtree(target)
    staging.replace(target)
    return target


def _data_bbox(bboxes: array.array) -> list[float] | None:
    """(south, west, north, east) actually spanned by the indexed ways."""
    if not bboxes:
        return None
    grid = np.frombuffer(bboxes, dtype=np.int32).reshape(-1, 4)
    return [
        float(grid[:, 1].min()) / SCALE,
        float(grid[:, 0].min()) / SCALE,
        float(grid[:, 3].max()) / SCALE,
        float(grid[:, 2].max()) / SCALE,
    ]


@dataclass
class _Index:
    """An index held open for the life of the process."""

    directory: Path
    meta: dict
    bboxes: np.ndarray  # (n, 4) int32: min x, min y, max x, max y
    offsets: np.ndarray  # (n + 1,) uint64
    cells: dict[tuple[int, int], array.array]
    records: bytes

    def candidates(self, west: int, south: int, east: int, north: int) -> np.ndarray:
        """Ways whose bounding box meets the query, cheapest test first."""
        step = int(CELL_DEG * SCALE)
        found: list[array.array] = []
        for cx in range(west // step, east // step + 1):
            for cy in range(south // step, north // step + 1):
                bucket = self.cells.get((cx, cy))
                if bucket is not None:
                    found.append(bucket)
        if not found:
            return np.empty(0, dtype=np.int64)

        # unique() deduplicates: a way long enough to span two cells is listed
        # in both, by index rather than by copy.
        ids = np.unique(np.concatenate([np.frombuffer(b, dtype=np.int32) for b in found]))
        boxes = self.bboxes[ids]
        keep = (
            (boxes[:, 0] <= east)
            & (boxes[:, 2] >= west)
            & (boxes[:, 1] <= north)
            & (boxes[:, 3] >= south)
        )
        return ids[keep]

    def record(self, index: int) -> tuple[int, list[tuple[float, float]], dict[str, str]]:
        start = int(self.offsets[index])
        osm_id, count, taglen = _RECORD.unpack_from(self.records, start)
        start += _RECORD.size
        raw = np.frombuffer(self.records, dtype=np.int32, count=count * 2, offset=start)
        start += count * 8
        tags = json.loads(self.records[start : start + taglen])
        coords = (raw.astype(np.float64) / SCALE).reshape(count, 2)
        return osm_id, [(float(x), float(y)) for x, y in coords], tags


@lru_cache(maxsize=4)
def _load(directory: str) -> _Index:
    path = Path(directory)
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    bboxes = np.fromfile(path / "bbox.bin", dtype=np.int32).reshape(-1, 4)
    offsets = np.fromfile(path / "offsets.bin", dtype=np.uint64)
    cells = pickle.loads((path / "cells.bin").read_bytes())
    # Read whole rather than mmapped: 120 MB for Kent, and a county build hits
    # it 5,800 times. Held by lru_cache so a bulk run pays for it once.
    records = (path / "records.bin").read_bytes()
    return _Index(path, meta, bboxes, offsets, cells, records)


def open_index(
    pbf: Path,
    cache_dir: Path | None = None,
    refresh: bool = False,
    selectors: list[str] | None = None,
) -> _Index:
    directory = index_dir(pbf, cache_dir)
    # `refresh` means "rebuild the index", not "rebuild it per tile". A bulk run
    # asks thousands of times and the scan is a minute a go.
    if refresh and str(directory) in _REFRESHED:
        refresh = False
    if refresh or not (directory / "meta.json").exists():
        _REFRESHED.add(str(directory))
        _load.cache_clear()
        print(f"indexing {pbf} (one pass, a few minutes; cached under {directory.parent})")
        build_index(pbf, cache_dir, selectors)
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        print(f"indexed {meta['ways']} ways in {meta['seconds']}s")

    index = _load(str(directory))
    wanted = {s.text() for s in parse_selectors(selectors or TILE_SELECTORS)}
    have = set(index.meta.get("selectors", []))
    if not wanted <= have:
        raise ExtractError(
            f"the index for {pbf.name} was built for {sorted(have)}, which does "
            f"not cover {sorted(wanted - have)} — rebuild it with refresh=True"
        )
    return index


def fetch_tile(
    bbox: tuple[float, float, float, float],
    cache_dir: Path | None = None,
    refresh: bool = False,
    selectors: list[str] | None = None,
    pbf: Path | None = None,
) -> TileFeatures:
    """Same signature, same buckets, same order as `overpass.fetch_tile`."""
    if pbf is None:
        raise ExtractError("no .osm.pbf extract given")
    index = open_index(Path(pbf), cache_dir, refresh, selectors)
    wanted = parse_selectors(selectors or TILE_SELECTORS)

    # Six decimals, because that is the bbox Overpass is actually asked about:
    # `build_query` formats it with `%.6f`. Keeping full precision here is not
    # more accurate, it is merely *different* — it picked up one extra footpath
    # on the Birchington 400m tile, a way whose only node inside the tile sits
    # 2 cm past the rounded edge. 11 cm of tile boundary is not worth a source
    # of disagreement between the two readers.
    south, west, north, east = (float(f"{value:.6f}") for value in bbox)
    rectangle = box(west, south, east, north)
    lo_x, lo_y = round(west * SCALE), round(south * SCALE)
    hi_x, hi_y = round(east * SCALE), round(north * SCALE)

    features: list[Feature] = []
    for candidate in index.candidates(lo_x, lo_y, hi_x, hi_y):
        osm_id, coords, tags = index.record(int(candidate))
        if not any(selector.matches("way", tags) for selector in wanted):
            continue
        # The bbox pass above is a filter, not the answer: a diagonal road's
        # bbox can straddle a tile the road itself misses entirely.
        if not LineString(coords).intersects(rectangle):
            continue
        features.append(
            Feature(
                osm_id=osm_id,
                kind="way",
                coords=coords,
                tags=tags,
                closed=coords[0] == coords[-1],
            )
        )

    # Ascending OSM id, because that is the order Overpass answers in and the
    # order is load-bearing: `geometry.build_geometry` resolves overlaps
    # last-wins. Sorted explicitly rather than relying on the .pbf being id
    # ordered, which is a Geofabrik convention and not a guarantee of the format.
    features.sort(key=lambda feature: feature.osm_id)
    return sort_features(features)
