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

#: The cut face of a terrain contour step.
#:
#: This was A-MUD, which drew every 0.5m contour as a brown band and turned a
#: gently sloping field into a visible flight of stairs — the single most
#: artificial thing in frame after the sky. Painting the risers with the same
#: flat as the ground they interrupt makes them read as folds in the grass
#: instead. It does not fix the staircase (that needs slopes, see the brief's
#: research item 7) but it stops it announcing itself.
#:
#: ZDoom permits a flat as a wall texture, which vanilla Doom does not.
TERRAIN_SIDE = GRASS
#: Water's edge is a genuine cut bank, so it keeps an earth face.
BANK = "A-MUD"

#: Texture repeats per default tiling, by wall texture name.
#:
#: Doom maps one texture pixel to one map unit, so a 128px-tall texture covers
#: 4m of wall. Freedoom's BRICK1 shows about ten courses over that, i.e. 40cm
#: bricks — three times life size. Nobody consciously notices, but it is why the
#: M1 screenshots read as "corridor wall" rather than "house": brick course
#: height is one of the few absolute size references a viewer has outdoors.
#:
#: Irregular textures (mud, rubble) are left alone — they have no feature size
#: to be wrong about, and scaling them just makes them noisier.
WALL_SCALE = {
    BRICK: 3.0,
    BRICK_GREY: 3.0,
    BRICK_DARK: 3.0,
    STONE: 2.0,
}


def wall_scale(name: str) -> float:
    return WALL_SCALE.get(name, 1.0)


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
