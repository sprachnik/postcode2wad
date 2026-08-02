"""The British National Grid tile scheme.

Generation is a pure function of a tile ID, not of a postcode. A postcode only
selects which tile you start in. That is what makes adjacent tiles line up: two
neighbouring tiles are generated independently but share an edge, because both
derive their geometry from absolute OSGB coordinates rather than from anything
local to one map.

Map-unit origin is the tile's south-west corner, so local coordinates are always
positive and comfortably inside Doom's coordinate range: a 400m tile at 32
units/m is 12,800 units across.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pyproj import Transformer

from . import CRS_OSGB, CRS_WGS84, DEFAULT_TILE_SIZE_M, UNITS_PER_METRE


@lru_cache(maxsize=2)
def _transformer(src: str, dst: str) -> Transformer:
    # always_xy keeps the argument order as (x, y) = (lon, lat) / (easting,
    # northing), instead of pyproj's CRS-declared axis order which for both
    # EPSG:4326 and EPSG:27700 is the other way round.
    return Transformer.from_crs(src, dst, always_xy=True)


def lonlat_to_osgb(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 lon/lat -> OSGB36 easting/northing in metres."""
    return _transformer(CRS_WGS84, CRS_OSGB).transform(lon, lat)


def osgb_to_lonlat(easting: float, northing: float) -> tuple[float, float]:
    return _transformer(CRS_OSGB, CRS_WGS84).transform(easting, northing)


@dataclass(frozen=True)
class Tile:
    """One square cell of the national grid."""

    ix: int
    iy: int
    size_m: int = DEFAULT_TILE_SIZE_M

    @classmethod
    def containing(cls, easting: float, northing: float, size_m: int = DEFAULT_TILE_SIZE_M) -> Tile:
        return cls(int(easting // size_m), int(northing // size_m), size_m)

    @property
    def id(self) -> str:
        """Stable identifier. Same ID always means the same bytes out."""
        return f"bng{self.size_m}-{self.ix}-{self.iy}"

    @property
    def origin(self) -> tuple[int, int]:
        return self.ix * self.size_m, self.iy * self.size_m

    @property
    def bbox_osgb(self) -> tuple[int, int, int, int]:
        e, n = self.origin
        return e, n, e + self.size_m, n + self.size_m

    @property
    def bbox_wgs84(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) — the order Overpass wants."""
        min_e, min_n, max_e, max_n = self.bbox_osgb
        corners = [
            osgb_to_lonlat(min_e, min_n),
            osgb_to_lonlat(min_e, max_n),
            osgb_to_lonlat(max_e, min_n),
            osgb_to_lonlat(max_e, max_n),
        ]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        return min(lats), min(lons), max(lats), max(lons)

    @property
    def size_units(self) -> int:
        return self.size_m * UNITS_PER_METRE

    def to_map(self, easting: float, northing: float) -> tuple[float, float]:
        """OSGB metres -> map units relative to this tile's SW corner."""
        origin_e, origin_n = self.origin
        return (easting - origin_e) * UNITS_PER_METRE, (northing - origin_n) * UNITS_PER_METRE

    def to_map_lonlat(self, lon: float, lat: float) -> tuple[float, float]:
        return self.to_map(*lonlat_to_osgb(lon, lat))

    def neighbour(self, dx: int, dy: int) -> Tile:
        return Tile(self.ix + dx, self.iy + dy, self.size_m)
