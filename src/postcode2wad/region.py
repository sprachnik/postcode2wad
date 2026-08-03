"""A block of neighbouring tiles in one PK3, with transitions at the edges.

M4. GZDoom builds BSP nodes when it loads a map and offers no way to inject
geometry into a running level, so a world larger than one tile cannot be
streamed in on stock engine — it has to be pre-generated and swapped between.
This module does the pre-generating: an N x N block of grid tiles becomes
MAP01..MAPnn inside a single PK3, and walking into a tile edge changes level to
the neighbour.

The transition is deliberately unglamorous. GZDoom's own level-change pause is
the loading state; the player keeps their heading and their perpendicular
position across the seam, and arrives just inside the far edge. Making it
seamless means preloading the next level while the current one runs, which is
an engine fork (M5) and not something to fake here.

Tiles line up because generation is a pure function of the grid tile: terrain
bands are quantised against ordnance datum rather than the tile's own range, so
neighbours put their 21m contour at exactly the same height.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .build import BuiltMap, build_tile
from .sources import overpass
from .sources.postcodes import Place
from .tiles import Tile, osgb_to_lonlat

#: Doom map lump names are MAPxx, so 99 is the hard ceiling — a 9x9 block.
MAX_MAPS = 99


def map_name(index: int) -> str:
    return f"MAP{index + 1:02d}"


def region_bbox_wgs84(centre: Tile, radius: int) -> tuple[float, float, float, float]:
    """(south, west, north, east) covering the whole block, as Overpass wants.

    All four corners are converted rather than just two: the grid is square in
    OSGB but not in WGS84, so the extreme latitude and longitude do not
    necessarily come from the same corner.
    """
    low = centre.neighbour(-radius, -radius)
    high = centre.neighbour(radius, radius)
    min_e, min_n = low.origin
    max_e = high.origin[0] + high.size_m
    max_n = high.origin[1] + high.size_m

    corners = [
        osgb_to_lonlat(min_e, min_n),
        osgb_to_lonlat(min_e, max_n),
        osgb_to_lonlat(max_e, min_n),
        osgb_to_lonlat(max_e, max_n),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    return min(lats), min(lons), max(lats), max(lons)


@dataclass
class RegionTile:
    tile: Tile
    map_name: str
    built: BuiltMap


@dataclass
class Region:
    tiles: list[RegionTile] = field(default_factory=list)
    radius: int = 1
    size_m: int = 400

    @property
    def span_m(self) -> int:
        return (self.radius * 2 + 1) * self.size_m

    def summary(self) -> str:
        sectors = sum(t.built.stats.sectors for t in self.tiles)
        lines = sum(t.built.stats.linedefs for t in self.tiles)
        side = self.radius * 2 + 1
        return (
            f"{len(self.tiles)} tiles ({side}x{side}, {self.span_m}m across), "
            f"{sectors} sectors, {lines} linedefs total"
        )


def build_region(
    place: Place,
    radius: int = 1,
    size_m: int = 400,
    cache_dir: Path | None = None,
    refresh: bool = False,
    progress=None,
    **tile_options,
) -> Region:
    """Build a (2*radius+1)^2 block of tiles centred on the postcode's tile.

    Tiles are laid out in row-major order from the south-west corner, which is
    only a convention — the ZScript navigates by grid index, not by map number,
    so nothing downstream depends on the ordering.
    """
    side = radius * 2 + 1
    if side * side > MAX_MAPS:
        raise ValueError(
            f"radius {radius} needs {side * side} maps; MAPxx only goes to {MAX_MAPS}"
        )

    centre = Tile.containing(place.easting, place.northing, size_m)
    region = Region(radius=radius, size_m=size_m)

    # One Overpass query for the whole block, not one per tile. Features get
    # clipped to each tile downstream anyway, so nine tiles need one round trip
    # rather than nine — which matters because a 3x3 run was enough to get
    # rate-limited across every mirror, and this is the shape bulk generation
    # has to take regardless.
    features = overpass.fetch_tile(region_bbox_wgs84(centre, radius), cache_dir, refresh)

    index = 0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            tile = centre.neighbour(dx, dy)
            name = map_name(index)
            if progress:
                progress(index, side * side, name, tile)

            built = build_tile(
                place,
                size_m=size_m,
                cache_dir=cache_dir,
                refresh=refresh,
                tile=tile,
                features=features,
                **tile_options,
            )
            region.tiles.append(RegionTile(tile=tile, map_name=name, built=built))
            index += 1

    return region
