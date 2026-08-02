"""Postcode geocoding via postcodes.io.

Free, no API key, and it returns OSGB eastings/northings directly alongside
lat/lng — which saves a reprojection and, more importantly, uses OS's own
figures rather than our approximation of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from ..tiles import lonlat_to_osgb
from . import cache

API = "https://api.postcodes.io"
USER_AGENT = "postcode2wad/0.1 (+https://github.com/sprachnik/postcode2wad)"
TIMEOUT = 20


@dataclass(frozen=True)
class Place:
    query: str
    postcode: str | None
    lat: float
    lon: float
    easting: float
    northing: float
    name: str

    @property
    def label(self) -> str:
        bits = [b for b in (self.postcode, self.name) if b]
        return " — ".join(bits) if bits else self.query


def lookup(postcode: str, cache_dir: Path | None = None) -> Place:
    """Resolve a UK postcode. Raises ValueError if it is not a real one."""
    key = postcode.strip().upper()
    payload = cache.load_json("postcodes", key, cache_dir)

    if payload is None:
        url = f"{API}/postcodes/{requests.utils.quote(key)}"
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if response.status_code == 404:
            raise ValueError(f"{postcode!r} is not a recognised UK postcode")
        response.raise_for_status()
        payload = response.json()
        cache.store_json("postcodes", key, payload, cache_dir)

    result = payload.get("result")
    if not result:
        raise ValueError(f"no result for postcode {postcode!r}")

    # postcodes.io gives eastings/northings for mainland GB but zeroes them for
    # Northern Ireland and the islands, so fall back to reprojecting ourselves.
    easting = result.get("eastings") or 0
    northing = result.get("northings") or 0
    if not easting or not northing:
        easting, northing = lonlat_to_osgb(result["longitude"], result["latitude"])

    name = ", ".join(
        b
        for b in (result.get("parish") or result.get("admin_ward"), result.get("admin_district"))
        if b
    )

    return Place(
        query=postcode,
        postcode=result.get("postcode"),
        lat=result["latitude"],
        lon=result["longitude"],
        easting=float(easting),
        northing=float(northing),
        name=name,
    )


def from_latlng(lat: float, lon: float) -> Place:
    easting, northing = lonlat_to_osgb(lon, lat)
    return Place(
        query=f"{lat},{lon}",
        postcode=None,
        lat=lat,
        lon=lon,
        easting=easting,
        northing=northing,
        name="",
    )
