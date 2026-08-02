"""Texture and flat names, verified present in freedoom2.wad 0.13.0.

Freedoom reuses Doom 2 lump names for compatibility but the artwork behind them
is completely different, so these were picked by looking at the rendered images
rather than by assuming a Doom 2 name means what it used to.

Names are checked in CI-ish fashion by scripts/lumps.py --check.
"""

from __future__ import annotations

SKY_FLAT = "F_SKY1"  # special: makes a ceiling render as sky
SKY_TEXTURE = "SKY1"  # MAPINFO sky1; brown/tan clouds in Freedoom

# Flats (F_START..F_END) — legal in texturefloor/textureceiling.
GRASS = "GRASS1"  # green grass
GRASS_DARK = "GRASS2"
ROAD = "MFLR8_4"  # dark grey gravel, reads as tarmac
PAVEMENT = "FLOOR0_1"  # grey slabs
CONCRETE = "FLAT5_4"  # light grey fine grain
ROOF = "CEIL5_2"
WATER = "FWATER1"

# Wall textures (TEXTURE1) — legal in texturemiddle/texturetop/texturebottom.
BRICK = "BRICK1"  # tan/brown brick
BRICK_GREY = "A-BRICK1"
BRICK_DARK = "BRICK5"
STONE = "STONE2"
KERB = "STONE2"
TERRAIN_SIDE = "A-MUD"  # brown mud, for the cut faces of terrain steps

#: Wall texture per OSM building class, so a church doesn't look like a shed.
BUILDING_WALLS = {
    "church": STONE,
    "cathedral": STONE,
    "chapel": STONE,
    "industrial": BRICK_GREY,
    "warehouse": BRICK_GREY,
    "commercial": BRICK_GREY,
    "retail": BRICK_GREY,
    "garage": BRICK_DARK,
    "garages": BRICK_DARK,
    "shed": BRICK_DARK,
}


def building_wall(tags: dict[str, str]) -> str:
    kind = tags.get("building", "yes")
    if kind in BUILDING_WALLS:
        return BUILDING_WALLS[kind]
    if tags.get("amenity") == "place_of_worship" or tags.get("historic"):
        return STONE
    return BRICK
