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
    "https://overpass.osm.jp/api/interpreter",
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
    'way["natural"="water"]',
    'relation["natural"="water"]',
    'way["waterway"="riverbank"]',
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

    def __len__(self) -> int:
        return len(self.buildings) + len(self.roads) + len(self.water) + len(self.landuse)


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

    out = TileFeatures()
    for element in payload.get("elements", []):
        geometry = element.get("geometry")
        if not geometry:
            continue
        coords = [(p["lon"], p["lat"]) for p in geometry]
        if len(coords) < 2:
            continue

        tags = element.get("tags", {}) or {}
        feature = Feature(
            osm_id=element.get("id", 0),
            kind=element.get("type", "way"),
            coords=coords,
            tags=tags,
            closed=coords[0] == coords[-1],
        )

        # A way can carry several of these tags; classify by precedence.
        if "building" in tags:
            if feature.closed and len(coords) >= 4:
                out.buildings.append(feature)
        elif "highway" in tags:
            out.roads.append(feature)
        elif tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            if feature.closed:
                out.water.append(feature)
        elif ("landuse" in tags or "leisure" in tags) and feature.closed:
            out.landuse.append(feature)

    return out


def _post(query: str) -> dict:
    """Try each mirror, then back off and go round again."""
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
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc

    raise RuntimeError(f"all Overpass endpoints failed after {RETRIES} rounds: {last_error}")
