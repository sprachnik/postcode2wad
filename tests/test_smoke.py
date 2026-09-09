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
import re
from pathlib import Path

import pytest

from postcode2wad import geometry as geometry_module
from postcode2wad.build import build_tile
from postcode2wad.sources import postcodes
from postcode2wad.textures import STANDS_PROUD

CACHE = Path("cache")
POSTCODE = "CT7 0EP"

#: The roof flat, so a building face is recognisable in the emitted map.
ROOF_FLAT = "CEIL5_2"

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


def test_no_spires_between_ground_sectors(tile):
    """Two patches of ground that touch must meet at the same height.

    The reported symptom was "rectangles of grass going right up into the sky":
    a thin face whose plane disagreed with its neighbour's along the edge they
    share, leaving a tall, paper-thin wall standing in the open.

    Steepness was the first thing measured here and it is the wrong signal --
    a steep plane that meets its neighbours exactly is a bank, and banks are
    correct. What matters is the disagreement at the join, so that is what this
    measures: sampled along every edge between two sloped ground sectors,
    end to end. It should be exactly zero, because both planes are fitted
    through the same two rounded corners.

    Only ground-to-ground joins count. A hedge or fence top against the grass
    beside it is legitimately 1.8m proud, and including those hid the real
    number behind a crowd of correct ones for some time.
    """
    walls = []
    for line in tile.linedefs:
        if "sideback" not in line:
            continue
        a = tile.sidedefs[line["sidefront"]]["sector"]
        b = tile.sidedefs[line["sideback"]]["sector"]
        if a == b:
            continue
        sa, sb = tile.sectors[a], tile.sectors[b]
        if "floorplane_a" not in sa or "floorplane_a" not in sb:
            continue
        # A barrier top is not ground. It was excluded until 5 Aug only by the
        # accident of barriers being *flat* sectors, so the moment they were
        # sloped to sit properly on the ground the crowd of legitimate 1.8m
        # hedge joins came back and buried the real number again — exactly what
        # this docstring says was fixed once already. Excluded by name now,
        # which is what the intent always was.
        if (
            sa.get("texturefloor") in STANDS_PROUD
            or sb.get("texturefloor") in STANDS_PROUD
        ):
            continue
        v1, v2 = tile.vertices[line["v1"]], tile.vertices[line["v2"]]
        for t in (0.0, 0.5, 1.0):
            x = v1[0] + t * (v2[0] - v1[0])
            y = v1[1] + t * (v2[1] - v1[1])
            walls.append(abs(floor_z(sa, x, y) - floor_z(sb, x, y)))

    assert walls, "no ground-to-ground joins found at all"
    tall = [w for w in walls if w > MAX_STEP]
    assert not tall, (
        f"{len(tall)} of {len(walls)} ground joins step more than the player can "
        f"climb, the worst by {max(tall) / 32:.2f} m — that is a spire"
    )


def test_no_interior_one_sided_lines(tile):
    """One-sided lines away from the map edge are tears or full-height walls.

    GZDoom draws a one-sided line as solid from its sector's floor to its
    ceiling, and outdoor ceilings here are 130m up. So an orphaned edge is not
    a subtle defect: it is a grass-textured blade standing in the sky, which is
    what a player reported after this test had been passing for weeks.

    It passed because it allowed anything under 32 units as "an invisible
    hairline". That was wrong twice over -- a hairline is invisible edge-on and
    a metre-wide wall face-on, and the tolerance was hiding real ones. There is
    no length at which an orphaned edge is acceptable, so there is no tolerance
    here now.

    It also only ever ran on this one tile, which had 11 of them (all short,
    all forgiven) while a neighbouring tile in the same region had 134. Per-map
    invariants need checking on more than the map they were written against;
    `scripts/onesided.py` sweeps a whole region PK3.
    """
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
        interior.append(math.dist(v1, v2))
    assert not interior, (
        f"{len(interior)} interior one-sided lines, longest {max(interior) / 32:.2f} m "
        "— each is a wall from the ground to the sky"
    )


def test_no_building_stands_on_its_own(tile):
    """A building face must be closed off by other building faces.

    Stated by the player who found these, and it is the right invariant: a
    house texture that is not boxed off is either wrong and should go, or it
    belongs to a building and should join it. There is no third case.

    They arise because footprints get clipped by things unrelated to them -- a
    contour band, a garden boundary a few centimetres off the wall -- and the
    offcut becomes its own sector at roof height. With ground on both sides it
    renders as a freestanding brick fin, and the worst was 24m long and 48cm
    thick, standing in a garden with nothing behind it.

    Two conditions, and both are needed. Mostly-open perimeter alone is not a
    fin: a detached house is a single sector with grass right round it, which
    is exactly what a house should be. Thinness alone is not a fin either: a
    narrow strip *between* two roof faces is flush with them and invisible.
    A face that is both is a wall standing on its own.
    """
    boundary = collections.defaultdict(lambda: collections.defaultdict(float))
    for line in tile.linedefs:
        if "sideback" not in line:
            continue
        a = tile.sidedefs[line["sidefront"]]["sector"]
        b = tile.sidedefs[line["sideback"]]["sector"]
        if a == b:
            continue
        length = math.dist(tile.vertices[line["v1"]], tile.vertices[line["v2"]])
        boundary[a][b] += length
        boundary[b][a] += length

    def is_roof(index: int) -> bool:
        return tile.sectors[index].get("texturefloor") == ROOF_FLAT

    fins = []
    for index, joins in boundary.items():
        if not is_roof(index) or not joins:
            continue
        face = tile.faces[index]
        # Mean width of a polygon: four times its area over its perimeter.
        if not face.length or 4 * face.area / face.length >= 32:  # 1 metre
            continue
        roofed = sum(length for other, length in joins.items() if is_roof(other))
        if roofed / sum(joins.values()) < 0.5:
            fins.append((4 * face.area / face.length / 32, index))

    fins.sort()
    assert not fins, (
        f"{len(fins)} building sectors are under a metre thick with most of their "
        f"perimeter against open ground — freestanding walls with no building "
        f"behind them; thinnest is {fins[0][0]:.2f} m"
    )


def test_every_sector_is_closed(tile):
    """A sector whose boundary does not close becomes a collision trap.

    GZDoom's node builder does not reject an unclosed sector; it builds
    something, and the something has phantom solid space in it. The symptom is
    the player standing on open ground, rendered perfectly, moving 1 unit per
    second in every direction.

    A closed boundary is a set of loops, so every vertex on it must be entered
    as many times as it is left -- which means each vertex appears an even
    number of times among the sector's own lines. That is cheap to check and it
    is exactly the property the node builder needs.

    This exists because the rule that used to guarantee it -- dropping
    sub-unit-area faces -- was doing so at the cost of 219 sky-high walls
    across a region, and was removed. Measured before removing it: no edge in
    the arrangement has more than two users either way, so the rule was not the
    thing keeping sectors closed. This test is the standing proof of that.
    """
    per_sector = collections.defaultdict(collections.Counter)
    for line in tile.linedefs:
        for key in ("sidefront", "sideback"):
            if key not in line:
                continue
            sector = tile.sidedefs[line[key]]["sector"]
            per_sector[sector][line["v1"]] += 1
            per_sector[sector][line["v2"]] += 1

    unclosed = [s for s, verts in per_sector.items() if any(n % 2 for n in verts.values())]
    assert not unclosed, (
        f"{len(unclosed)} of {len(tile.sectors)} sectors have an unclosed boundary"
    )


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


def test_plane_coefficients_survive_being_written_out(tile):
    """A slope written to three decimals is not a slope.

    Plane normals are unit vectors, so on ordinary ground the horizontal
    components are tiny -- a 4% gradient gives ~0.04, gentler ground far less.
    Formatted with "%.3f" they became "-0.000" and every plane emerged
    perfectly flat at its own height, turning the terrain into a mosaic of
    level triangles with a crack at every edge. It looked like a hillside from
    a distance, which is why it survived so long.

    The planes were correct everywhere in Python; only the file was wrong. So
    this asserts on the *emitted text*, which is the only place the bug existed.
    """
    from postcode2wad.udmf import emit_textmap

    text = emit_textmap(tile)
    emitted = re.findall(r"floorplane_a = (-?[\d.eE+-]+);", text)
    assert emitted, "no floor planes were emitted at all"

    # Every plane that should be tilted must still be tilted on paper.
    tilted_in_memory = sum(
        1 for s in tile.sectors
        if "floorplane_a" in s and abs(s["floorplane_a"]) > 1e-6
    )
    tilted_on_paper = sum(1 for v in emitted if abs(float(v)) > 1e-6)
    assert tilted_on_paper >= tilted_in_memory * 0.99, (
        f"{tilted_in_memory} planes are tilted in memory but only {tilted_on_paper} "
        "survived being written out — the float format is destroying them"
    )
