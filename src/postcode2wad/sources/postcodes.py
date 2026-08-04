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


def place_name(result: dict) -> str:
    """"Ramsgate, Thanet" from a postcodes.io result.

    Two things postcodes.io does that read badly as a level title:

    * where there is no civil parish it returns the parish as
      "<district>, unparished area", which joined to the district gives
      "Thanet, unparished area, Thanet" — 25 of Thanet's 200 maps. The ward is
      a real place name, so it wins there.
    * parish and district can be the same word, giving "Dover, Dover".
    """
    parish = result.get("parish") or ""
    local = parish if parish and "unparished" not in parish.lower() else ""
    local = local or (result.get("admin_ward") or "")
    district = result.get("admin_district") or ""

    parts = [b for b in (local, district) if b]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return ", ".join(parts)


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

    name = place_name(result)

    return Place(
        query=postcode,
        postcode=result.get("postcode"),
        lat=result["latitude"],
        lon=result["longitude"],
        easting=float(easting),
        northing=float(northing),
        name=name,
    )


def nearest(lat: float, lon: float, cache_dir: Path | None = None) -> Place:
    """The nearest real postcode to a point, for naming a tile nobody asked for.

    A district pack builds tiles by grid position, not by postcode, so there is
    no name to hang on them: `from_latlng` leaves `name` empty and the title
    falls through to the query string, which would make every level in a
    200-tile pack a pair of coordinates. Reverse geocoding gives "Cliftonville,
    Thanet" instead, which is what a player sees on the HUD and in the menu.

    Falls back to `from_latlng` rather than raising. A tile out at sea, or a
    postcodes.io hiccup, must not be able to kill a run of hundreds of tiles
    over a *cosmetic* lookup — the geometry does not depend on this at all.
    """
    key = f"{lat:.5f},{lon:.5f}"
    payload = cache.load_json("nearest", key, cache_dir)

    if payload is None:
        try:
            response = requests.get(
                f"{API}/postcodes",
                params={"lat": lat, "lon": lon, "limit": 1, "radius": 2000},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            cache.store_json("nearest", key, payload, cache_dir)
        except (requests.RequestException, ValueError):
            return from_latlng(lat, lon)

    results = payload.get("result") or []
    if not results:
        return from_latlng(lat, lon)

    result = results[0]
    name = place_name(result)
    # The point stays where the caller put it: we want the tile's own centre,
    # not the postcode's, or the spawn would drift toward whichever postcode
    # happened to be nearest.
    easting, northing = lonlat_to_osgb(lon, lat)
    return Place(
        query=key,
        postcode=None,
        lat=lat,
        lon=lon,
        easting=easting,
        northing=northing,
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
