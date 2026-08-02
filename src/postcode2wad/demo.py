"""M0 — the geometry proof.

A synthetic scene with no real-world data in it, built only to prove the
pipeline end to end: overlapping polygons -> planar arrangement -> UDMF ->
PK3 -> walkable in GZDoom.

It deliberately exercises the three things that break naive generators:
a terrain step sharing an edge with flat ground, two buildings standing on
different terrain heights, and a building with a courtyard hole in it.
"""

from __future__ import annotations

from shapely.geometry import Polygon, box

from . import UNITS_PER_METRE as U
from .geometry import SKY_FLAT, SectorSpec, Thing, build_geometry

SKY_HEIGHT = 4096  # 128m of headroom; the sky flat means you never see it

# Placeholder texture names, replaced once the Freedoom lump audit lands.
GRASS_FLAT = "RROCK19"
ROAD_FLAT = "FLAT5_4"
ROOF_FLAT = "CEIL5_2"
WALL_TEX = "BRICK7"
TERRAIN_WALL = "ROCK4"


def m(metres: float) -> int:
    """Metres -> map units, integer-snapped."""
    return int(round(metres * U))


def build_m0_scene(size_m: float = 200.0):
    """Return (SectorSpecs, Things) for the synthetic proof scene."""
    ground = box(0, 0, m(size_m), m(size_m))

    specs = [
        # Ground plane.
        SectorSpec(
            polygon=ground,
            floor=0,
            ceiling=SKY_HEIGHT,
            floor_tex=GRASS_FLAT,
            wall_tex=TERRAIN_WALL,
        ),
        # A terrain step: raised plateau across the north of the tile. Shares a
        # long edge with the ground, which is the case that breaks naive emitters.
        SectorSpec(
            polygon=box(m(20), m(120), m(180), m(180)),
            floor=m(2),
            ceiling=SKY_HEIGHT,
            floor_tex=GRASS_FLAT,
            wall_tex=TERRAIN_WALL,
        ),
        # A sunken hollow, to prove negative steps work too.
        SectorSpec(
            polygon=box(m(120), m(20), m(170), m(60)),
            floor=m(-1.5),
            ceiling=SKY_HEIGHT,
            floor_tex=ROAD_FLAT,
            wall_tex=TERRAIN_WALL,
        ),
        # A road strip crossing the whole tile, flat at ground level.
        SectorSpec(
            polygon=box(0, m(94), m(size_m), m(100)),
            floor=0,
            ceiling=SKY_HEIGHT,
            floor_tex=ROAD_FLAT,
            wall_tex=TERRAIN_WALL,
        ),
        # Building 1: simple house on flat ground, 8m to the eaves.
        SectorSpec(
            polygon=box(m(40), m(40), m(58), m(52)),
            floor=m(8),
            ceiling=SKY_HEIGHT,
            floor_tex=ROOF_FLAT,
            wall_tex=WALL_TEX,
        ),
        # Building 2: taller block standing on the raised plateau, so its walls
        # meet terrain that is already 2m up.
        SectorSpec(
            polygon=box(m(60), m(140), m(90), m(165)),
            floor=m(14),
            ceiling=SKY_HEIGHT,
            floor_tex=ROOF_FLAT,
            wall_tex=WALL_TEX,
        ),
        # Building 3: a courtyard block — an outer ring with a hole in it. The
        # courtyard must come out as walkable ground enclosed by walls.
        SectorSpec(
            polygon=Polygon(
                shell=box(m(120), m(140), m(170), m(175)).exterior.coords,
                holes=[box(m(133), m(150), m(157), m(166)).exterior.coords],
            ),
            floor=m(10),
            ceiling=SKY_HEIGHT,
            floor_tex=ROOF_FLAT,
            wall_tex=WALL_TEX,
        ),
    ]

    # Player start on the road, facing north up the tile.
    things = [Thing(x=m(size_m / 2), y=m(97), type=1, angle=90)]
    return specs, things


def build_m0_geometry(size_m: float = 200.0):
    specs, things = build_m0_scene(size_m)
    return build_geometry(specs, things)
