"""Local authority district boundaries, from the ONS.

Kent ships as one PK3 per district (12 of them), so "which tiles am I building"
is answered by a real administrative polygon rather than a bounding box. A box
over Thanet pulls in a corner of Canterbury and a great deal of the Thames
estuary; the polygon does not.

Full-resolution *clipped to the coastline* (BFC), deliberately. The generalised
boundaries (BGC/BUC) run out to the mean low water mark and in places well
beyond it, which would add tiles that are entirely sea — they generate nothing,
but they still cost a LIDAR probe each to discover that.

The polygon comes back in OSGB (`outSR=27700`) so it can be intersected with
grid tiles directly, with no reprojection on our side.
"""

from __future__ import annotations

from pathlib import Path

import requests

from . import cache

#: ONS Local Authority Districts, December 2023, full-resolution clipped.
ONS_LAD = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_December_2023_Boundaries_UK_BFC/FeatureServer/0/query"
)

USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 600


def district_geojson(name: str, cache_dir: Path | None = None, refresh: bool = False) -> dict:
    """The raw GeoJSON feature for a named district, cached on disk.

    Cached because a district build asks for it once per invocation and a
    resumed run asks again — and because the service is somebody else's.
    """
    key = name.strip()
    payload = None if refresh else cache.load_json("boundaries", key, cache_dir)

    if payload is None:
        response = requests.get(
            ONS_LAD,
            params={
                "where": f"LAD23NM='{key}'",
                "outFields": "LAD23CD,LAD23NM",
                "returnGeometry": "true",
                "outSR": "27700",
                "f": "geojson",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        cache.store_json("boundaries", key, payload, cache_dir)

    features = payload.get("features") or []
    if not features:
        raise ValueError(f"no district named {name!r} in the ONS 2023 boundaries")
    return features[0]


def district_polygon(name: str, cache_dir: Path | None = None, refresh: bool = False):
    """A shapely polygon of the district, in OSGB metres."""
    from shapely.geometry import shape

    return shape(district_geojson(name, cache_dir, refresh)["geometry"])
