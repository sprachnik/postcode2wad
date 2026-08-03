"""Invariants on a real generated tile.

Every bug that cost real time in this project rendered perfectly and passed
visual inspection. A floor plane whose normal pointed down looked identical to
one pointing up but made the map unwalkable; a leaked loop variable flattened
the entire map with no error; two of nine tiles lost their HUD and their edge
transitions to an octal parse. None of that is visible in a screenshot, and
none of it would have failed a unit test of the parts in isolation.

So these assert properties of a *whole generated tile*, from cached real data.
They are the gate that has to hold before generating tiles in bulk: a sign
error found after 5,000 tiles is 5,000 tiles.

Skipped when the cache is empty, since the tile has to come from somewhere and
these must never reach for the network.
"""

from __future__ import annotations

import collections
import math
from pathlib import Path

import pytest

from postcode2wad import geometry as geometry_module
from postcode2wad.build import build_tile
from postcode2wad.sources import postcodes

CACHE = Path("cache")
POSTCODE = "CT1 2EH"

#: Doom's climb limit, and the narrowest gap a 16-radius player fits through.
MAX_STEP = 24
MIN_GAP = 33

GROUND = {
    "DMGRASS", "DMGARDEN", "DMMEADOW", "DMPITCH", "DMFARM", "DMSAND",
    "DMTARMAC", "DMPAVE", "DMCONC", "DMGRAVEL", "DMSCRUB", "DMWOOD",
}


@pytest.fixture(scope="module")
def tile():
    if not (CACHE / "overpass").is_dir() or not any((CACHE / "overpass").iterdir()):
        pytest.skip("no cached tile data")
    captured = {}
    original = geometry_module.build_geometry

    def spy(*args, **kwargs):
        geo = original(*args, **kwargs)
        captured["geo"] = geo
        return geo

    import postcode2wad.build as build_module

    build_module.build_geometry = spy
    try:
        build_tile(postcodes.lookup(POSTCODE, CACHE), cache_dir=CACHE)
    finally:
        build_module.build_geometry = original
    return captured["geo"]


def floor_z(sector: dict, x: float, y: float) -> float:
    if "floorplane_a" not in sector:
        return float(sector["heightfloor"])
    return -(
        sector["floorplane_a"] * x + sector["floorplane_b"] * y + sector["floorplane_d"]
    ) / sector["floorplane_c"]


def adjacency(geo):
    adj = collections.defaultdict(list)
    for line in geo.linedefs:
        if "sideback" not in line:
            continue
        a = geo.sidedefs[line["sidefront"]]["sector"]
        b = geo.sidedefs[line["sideback"]]["sector"]
        if a == b:
            continue
        v1, v2 = geo.vertices[line["v1"]], geo.vertices[line["v2"]]
        mx, my = (v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2
        length = math.dist(v1, v2)
        adj[a].append((b, mx, my, length))
        adj[b].append((a, mx, my, length))
    return adj


def test_every_floor_plane_points_up(tile):
    """The one that made the whole map unwalkable while looking perfect.

    GZDoom renders a downward-facing floor identically and the player even
    stands on it at the right height, but the physics treats everything above
    it as inside solid ground and refuses every horizontal move.
    """
    wrong = [s for s in tile.sectors if "floorplane_c" in s and s["floorplane_c"] <= 0]
    assert not wrong, f"{len(wrong)} floor planes face downward"


def test_the_ground_is_actually_sloped(tile):
    """Guards the leaked loop variable that silently flattened every sector.

    Nothing errored: the map generated, loaded and rendered, just entirely
    flat, because the sector loop read a plane left over from the loop above.
    """
    sloped = sum(1 for s in tile.sectors if "floorplane_a" in s)
    assert sloped > len(tile.sectors) * 0.5, (
        f"only {sloped}/{len(tile.sectors)} sectors are sloped; the terrain has gone flat"
    )


def test_no_plane_is_a_shard(tile):
    """Near-vertical planes render as spikes into the sky."""
    steep = []
    for s in tile.sectors:
        if "floorplane_a" not in s:
            continue
        gradient = math.hypot(s["floorplane_a"], s["floorplane_b"]) / abs(s["floorplane_c"])
        if gradient > 1.0:
            steep.append(gradient)
    # A handful of large faces legitimately keep a steep fit; a rash of them
    # means the salvage rule has broken.
    assert len(steep) < 40, f"{len(steep)} planes steeper than 45 degrees"


def test_no_interior_one_sided_lines(tile):
    """One-sided lines away from the map edge are tears or full-height walls."""
    xs = [v[0] for v in tile.vertices]
    ys = [v[1] for v in tile.vertices]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)

    def on_edge(v):
        return (
            abs(v[0] - lo_x) < 1 or abs(v[0] - hi_x) < 1
            or abs(v[1] - lo_y) < 1 or abs(v[1] - hi_y) < 1
        )

    interior = []
    for line in tile.linedefs:
        if "sideback" in line:
            continue
        v1, v2 = tile.vertices[line["v1"]], tile.vertices[line["v2"]]
        if on_edge(v1) and on_edge(v2):
            continue
        if math.dist(v1, v2) > 32:  # under a metre is an invisible hairline
            interior.append(math.dist(v1, v2))
    assert not interior, f"{len(interior)} interior one-sided lines, longest {max(interior):.0f}u"


def test_most_of_the_tile_is_walkable(tile):
    """Flood-fill from the spawn. Catches anything that walls the map off."""
    adj = adjacency(tile)
    start = next(t for t in tile.things if t.type == 1)
    from shapely.geometry import Point

    point = Point(start.x, start.y)
    start_sector = next(i for i, f in enumerate(tile.faces) if f.contains(point))

    seen = {start_sector}
    queue = [start_sector]
    while queue:
        current = queue.pop()
        for other, mx, my, length in adj[current]:
            if other in seen or length < MIN_GAP:
                continue
            step = abs(floor_z(tile.sectors[current], mx, my) - floor_z(tile.sectors[other], mx, my))
            if step <= MAX_STEP:
                seen.add(other)
                queue.append(other)

    total = sum(f.area for f in tile.faces)
    reached = sum(tile.faces[i].area for i in seen)
    assert reached / total > 0.85, f"only {100 * reached / total:.1f}% of the tile is reachable"


def test_unreachable_ground_is_enclosed_by_something_real(tile):
    """Ground you cannot reach is fine if a building or hedge encloses it.

    A courtyard ringed by houses *should* be unreachable. Ground surrounded by
    more ground should not be — that is a street walled off by an artefact.
    """
    adj = adjacency(tile)
    start = next(t for t in tile.things if t.type == 1)
    from shapely.geometry import Point

    point = Point(start.x, start.y)
    start_sector = next(i for i, f in enumerate(tile.faces) if f.contains(point))
    seen = {start_sector}
    queue = [start_sector]
    while queue:
        current = queue.pop()
        for other, mx, my, length in adj[current]:
            if other in seen or length < MIN_GAP:
                continue
            step = abs(floor_z(tile.sectors[current], mx, my) - floor_z(tile.sectors[other], mx, my))
            if step <= MAX_STEP:
                seen.add(other)
                queue.append(other)

    lost = {
        i for i in range(len(tile.sectors))
        if i not in seen and tile.sectors[i].get("texturefloor") in GROUND
    }
    remaining = set(lost)
    suspect_area = 0.0
    while remaining:
        seed = remaining.pop()
        pocket, stack = [seed], [seed]
        while stack:
            current = stack.pop()
            for other, _mx, _my, _len in adj[current]:
                if other in remaining:
                    remaining.discard(other)
                    pocket.append(other)
                    stack.append(other)
        border = collections.Counter()
        for i in pocket:
            for other, _mx, _my, _len in adj[i]:
                if other not in pocket:
                    border[tile.sectors[other].get("texturefloor")] += 1
        if not border:
            continue
        ground_edges = sum(n for tex, n in border.items() if tex in GROUND)
        if ground_edges / sum(border.values()) > 0.75:
            suspect_area += sum(tile.faces[i].area for i in pocket)

    total = sum(f.area for f in tile.faces)
    assert suspect_area / total < 0.01, (
        f"{100 * suspect_area / total:.2f}% of the tile is open ground walled off by nothing"
    )


def test_the_ground_is_not_faceted(tile):
    """Choppiness: the angle between neighbouring ground planes."""
    adj = adjacency(tile)
    angles = []
    seen_pairs = set()
    for i, sector in enumerate(tile.sectors):
        if "floorplane_a" not in sector:
            continue
        n0 = (sector["floorplane_a"], sector["floorplane_b"], sector["floorplane_c"])
        for other, _mx, _my, _len in adj[i]:
            key = (min(i, other), max(i, other))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            neighbour = tile.sectors[other]
            if "floorplane_a" not in neighbour:
                continue
            n1 = (neighbour["floorplane_a"], neighbour["floorplane_b"], neighbour["floorplane_c"])
            dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(n0, n1, strict=True))))
            angles.append(math.degrees(math.acos(dot)))

    angles.sort()
    median = angles[len(angles) // 2]
    assert median < 2.0, f"ground is faceted: median join angle {median:.1f} degrees"


def test_no_solid_thing_blocks_the_spawn(tile):
    """A tree inside the player's radius leaves them unable to move at all."""
    start = next(t for t in tile.things if t.type == 1)
    for thing in tile.things:
        if thing is start or thing.type == 1:
            continue
        distance = math.dist((thing.x, thing.y), (start.x, start.y))
        assert distance > 28, f"a solid thing sits {distance:.0f} units from the player start"


def test_generated_zscript_parses_map_numbers_as_base_ten(tile):
    """"08" and "09" are not valid octal, and ToInt() auto-detects the base.

    Two of nine tiles lost their HUD and their edge transitions to this.
    """
    from postcode2wad.hud import build_zscript
    from postcode2wad.region import map_name
    from postcode2wad.tiles import Tile

    centre = Tile(1574, 423, 400)
    tiles = [centre.neighbour(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    script = build_zscript(tiles, [map_name(i, len(tiles)) for i in range(len(tiles))])
    assert ".ToInt(10)" in script, "map number parsing must specify base 10"
    assert ".ToInt()" not in script, "a base-less ToInt() will read '08' as octal"
