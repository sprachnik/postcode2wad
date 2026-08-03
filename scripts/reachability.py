"""How much of a tile can the player actually walk to?

    python scripts/reachability.py "CT1 2EH"

Flood-fills the sector graph from the player start, crossing a two-sided edge
only when the step at its midpoint is within Doom's 24-unit climb limit and the
edge is long enough to fit through. This is the systematic version of "walk
around and see where you get stuck": it finds every choke point at once and
names what causes it.

Exists because of a bug report that took three wrong guesses to diagnose: the
player walked down a footpath and stopped dead. The terrain, the contour bands
and the sloped planes were all blamed before measurement showed the geometry
was fine and a solid tree actor was standing in the path. Sector reachability
rules the geometry in or out in one run; anything left is a Thing.

What good looks like: ~90% of area reachable, a frontier made only of building
roofs (CEIL5_2), barrier tops (DMCONC / DMWOOD) and water, and unreachable
*ground* under 1% — enclosed back gardens are real, walled-off streets are not.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shapely.geometry import Point  # noqa: E402

import postcode2wad.build as build_module  # noqa: E402
from postcode2wad import geometry as geometry_module  # noqa: E402
from postcode2wad.sources import postcodes  # noqa: E402

#: Player fits through nothing shorter than this (2 * radius 16, plus play).
MIN_GAP_UNITS = 33
MAX_STEP_UNITS = 24

GROUND = {
    "DMGRASS", "DMGARDEN", "DMMEADOW", "DMPITCH", "DMFARM", "DMSAND",
    "DMTARMAC", "DMPAVE", "DMCONC", "DMGRAVEL", "DMSCRUB", "DMWOOD",
}


def floor_z(sector: dict, x: float, y: float) -> float:
    if "floorplane_a" not in sector:
        return float(sector["heightfloor"])
    return -(
        sector["floorplane_a"] * x + sector["floorplane_b"] * y + sector["floorplane_d"]
    ) / sector["floorplane_c"]


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "CT1 2EH"

    captured = {}
    original = geometry_module.build_geometry

    def spy(*args, **kwargs):
        geo = original(*args, **kwargs)
        captured["geo"] = geo
        return geo

    build_module.build_geometry = spy
    place = postcodes.lookup(query, Path("cache"))
    build_module.build_tile(place, cache_dir=Path("cache"))
    build_module.build_geometry = original
    geo = captured["geo"]

    adjacency = collections.defaultdict(list)
    for line in geo.linedefs:
        if "sideback" not in line:
            continue
        s0 = geo.sidedefs[line["sidefront"]]["sector"]
        s1 = geo.sidedefs[line["sideback"]]["sector"]
        if s0 == s1:
            continue
        v1, v2 = geo.vertices[line["v1"]], geo.vertices[line["v2"]]
        mx, my = (v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2
        length = ((v2[0] - v1[0]) ** 2 + (v2[1] - v1[1]) ** 2) ** 0.5
        adjacency[s0].append((s1, mx, my, length))
        adjacency[s1].append((s0, mx, my, length))

    start = next(t for t in geo.things if t.type == 1)
    start_point = Point(start.x, start.y)
    start_sector = next(i for i, f in enumerate(geo.faces) if f.contains(start_point))

    seen = {start_sector}
    queue = [start_sector]
    while queue:
        current = queue.pop()
        for other, mx, my, length in adjacency[current]:
            if other in seen or length < MIN_GAP_UNITS:
                continue
            step = abs(floor_z(geo.sectors[current], mx, my) - floor_z(geo.sectors[other], mx, my))
            if step <= MAX_STEP_UNITS:
                seen.add(other)
                queue.append(other)

    total = sum(f.area for f in geo.faces)
    reached = sum(geo.faces[i].area for i in seen)
    stuck_ground = sum(
        geo.faces[i].area
        for i in range(len(geo.sectors))
        if i not in seen and geo.sectors[i].get("texturefloor") in GROUND
    )

    frontier = collections.Counter()
    for current in seen:
        for other, mx, my, length in adjacency[current]:
            if other in seen or length < MIN_GAP_UNITS:
                continue
            frontier[geo.sectors[other].get("texturefloor")] += 1

    print(f"{place.label}: {len(seen)}/{len(geo.sectors)} sectors reachable, "
          f"{100 * reached / total:.1f}% of area")
    print(f"unreachable ground: {100 * stuck_ground / total:.2f}% of the tile")
    print("frontier:", dict(frontier.most_common(8)))

    # Exit code for CI-ish use: unreachable ground over 2% means something is
    # walled off that should not be.
    return 1 if stuck_ground / total > 0.02 else 0


if __name__ == "__main__":
    raise SystemExit(main())
