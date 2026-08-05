"""OpenStreetMap vector data via the Overpass API.

Two things matter here.

`out geom;` inlines each way's coordinates in the response, so we never have to
resolve node references ourselves.

And everything a tile needs is fetched in a *single* query. Overpass is donated
infrastructure — three separate round trips per tile is both slower and ruder
than one, and in practice the second and third are what get you a 504. The
response is cached to disk keyed on the query text, so regenerating a tile
costs nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import cache

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    # overpass.osm.jp was here and has been dropped: it serves a certificate
    # that is not valid for its own hostname, so every request to it fails
    # verification. Because it was last in the list its SSL error was the one
    # reported when the other two rate-limited, which made a throttling problem
    # look like a certificate problem.
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 180
RETRIES = 3
BACKOFF_SECONDS = 5

#: Everything a tile needs, in one query.
TILE_SELECTORS = [
    'way["building"]',
    'relation["building"]',
    'way["highway"]',
    # Whole `natural` key, not just water and coastline. Eight land covers --
    # beach, sand, wood, scrub, heath, grassland, shingle, bare_rock -- were
    # sitting in the land-cover table with no way of ever being fetched, which
    # is why a coastal tile had no beach on it.
    'way["natural"]',
    'relation["natural"]',
    'way["waterway"="riverbank"]',
    'way["barrier"]',
    # Railways. Birchington has a station and had no track: nothing read the
    # key at all, so the line through the middle of the village was open grass.
    'way["railway"]',
    'way["landuse"]',
    'way["leisure"]',
]


@dataclass
class Feature:
    """One OSM way or relation with its geometry as WGS84 (lon, lat) pairs."""

    osm_id: int
    kind: str
    coords: list[tuple[float, float]]
    tags: dict[str, str] = field(default_factory=dict)
    closed: bool = False

    @property
    def name(self) -> str:
        return self.tags.get("name", "")


@dataclass
class TileFeatures:
    buildings: list[Feature] = field(default_factory=list)
    roads: list[Feature] = field(default_factory=list)
    water: list[Feature] = field(default_factory=list)
    landuse: list[Feature] = field(default_factory=list)
    #: Open ways, not areas — see `features.sea_from_coastline`.
    coastline: list[Feature] = field(default_factory=list)
    #: Hedges, fences and garden walls. Also open ways: extruded, not filled.
    barriers: list[Feature] = field(default_factory=list)
    #: Track centrelines, buffered into a ballasted corridor like a road.
    railways: list[Feature] = field(default_factory=list)

    def __len__(self) -> int:
        return (
            len(self.buildings)
            + len(self.roads)
            + len(self.railways)
            + len(self.water)
            + len(self.landuse)
            + len(self.coastline)
            + len(self.barriers)
        )


def build_query(bbox: tuple[float, float, float, float], selectors: list[str]) -> str:
    south, west, north, east = bbox
    b = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    body = "\n  ".join(f"{sel}({b});" for sel in selectors)
    return f"[out:json][timeout:{TIMEOUT}];\n(\n  {body}\n);\nout geom;"


def fetch_tile(
    bbox: tuple[float, float, float, float],
    cache_dir: Path | None = None,
    refresh: bool = False,
    selectors: list[str] | None = None,
) -> TileFeatures:
    """One query, one cache entry, features split by tag on the way back."""
    query = build_query(bbox, selectors or TILE_SELECTORS)

    payload = None if refresh else cache.load_json("overpass", query, cache_dir)
    if payload is None:
        payload = _post(query)
        cache.store_json("overpass", query, payload, cache_dir)

    features = []
    for element in payload.get("elements", []):
        geometry = element.get("geometry")
        if not geometry:
            # Relations answer `out geom;` with per-member geometry and no
            # top-level `geometry`, so a multipolygon contributes nothing. That
            # is a known gap, not an accident — see the note on `sort_features`.
            continue
        coords = [(p["lon"], p["lat"]) for p in geometry]
        if len(coords) < 2:
            continue

        features.append(
            Feature(
                osm_id=element.get("id", 0),
                kind=element.get("type", "way"),
                coords=coords,
                tags=element.get("tags", {}) or {},
                closed=coords[0] == coords[-1],
            )
        )

    return sort_features(features)


def sort_features(features: list[Feature]) -> TileFeatures:
    """Split a flat list of ways into the buckets the builder consumes.

    Lives here, apart from the fetching, because there is now more than one
    thing that can do the fetching — Overpass and a local .osm.pbf extract —
    and the two must not be allowed to disagree about what counts as a
    building. One classifier, one set of rules, no drift.

    Order within each bucket is the caller's order and is load-bearing:
    `geometry.build_geometry` resolves overlaps last-wins, so two overlapping
    buildings come out differently if they arrive the other way round. Both
    sources hand this OSM id ascending, which is what Overpass emits.
    """
    out = TileFeatures()
    for feature in features:
        tags = feature.tags
        # A way can carry several of these tags; classify by precedence.
        if "building" in tags:
            if feature.closed and len(feature.coords) >= 4:
                out.buildings.append(feature)
        elif "highway" in tags:
            out.roads.append(feature)
        elif "railway" in tags:
            # After highway on purpose: a level crossing carries both, and the
            # road surface is the one you stand on.
            out.railways.append(feature)
        elif "barrier" in tags and "waterway" not in tags:
            out.barriers.append(feature)
        elif tags.get("natural") == "coastline":
            # Deliberately no `closed` check: the coast of Great Britain is a
            # single open way thousands of kilometres long, and what arrives here
            # is whatever fragment of it crosses the tile.
            out.coastline.append(feature)
        elif tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            if feature.closed:
                out.water.append(feature)
        elif ("landuse" in tags or "leisure" in tags or "natural" in tags) and feature.closed:
            # `closed` matters here: plenty of natural features are open ways
            # (cliff, tree_row, ridge) and would be nonsense as filled areas.
            out.landuse.append(feature)

    return out


def _post(query: str) -> dict:
    """Try each mirror, then back off and go round again.

    Every endpoint's failure is kept, not just the last one. With only the last
    one you get told about whichever mirror happens to be at the end of the
    list, which is actively misleading when the real problem is that the first
    two rate-limited you.
    """
    failures: dict[str, str] = {}
    last_error: Exception | None = None

    for attempt in range(RETRIES):
        if attempt:
            time.sleep(BACKOFF_SECONDS * attempt)
        for endpoint in ENDPOINTS:
            try:
                response = requests.post(
                    endpoint,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                )
                # 429 = rate limited, 504 = the mirror is saturated. Both are
                # "come back later", not "your query is wrong".
                if response.status_code in (429, 502, 503, 504):
                    last_error = RuntimeError(f"{endpoint} returned {response.status_code}")
                    failures[endpoint] = f"HTTP {response.status_code}"
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                failures[endpoint] = type(exc).__name__

    detail = "; ".join(f"{host.split('/')[2]}: {why}" for host, why in failures.items())
    raise RuntimeError(
        f"all Overpass endpoints failed after {RETRIES} rounds ({detail}). "
        "429 across the board means we are being rate limited — Overpass is "
        "donated infrastructure, so back off rather than retrying harder."
    ) from last_error
