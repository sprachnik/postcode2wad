"""Explain why the player cannot move at a specific point.

    python scripts/whystuck.py out/birchington-area.pk3 MAP06 12405 6941

Takes the coordinates straight out of a `P2W STUCK` telemetry line and reports
everything within reach of the player's collision cylinder: solid Things, the
floor under each corner of the player's box, blocking lines, and the step to
every neighbouring sector.

Written after a stuck report was diagnosed by three wrong theories in a row.
Reading the actual map data at the actual coordinates takes seconds and rules
out entire categories at once.
"""

from __future__ import annotations

import math
import re
import struct
import sys
import zipfile

#: Doom player: radius 16, and it climbs 24 units.
PLAYER_RADIUS = 16
MAX_STEP = 24
#: Tree actors are radius 12, so centres closer than this are impassable.
TREE_RADIUS = 12
TREE_TYPE = "20501"


def textmap_of(pk3: str, mapname: str) -> str:
    with zipfile.ZipFile(pk3) as z:
        wad = z.read(f"maps/{mapname}.wad")
    _magic, count, directory = struct.unpack_from("<4sii", wad, 0)
    for index in range(count):
        pos, size, raw = struct.unpack_from("<ii8s", wad, directory + index * 16)
        if raw.rstrip(b"\0") == b"TEXTMAP":
            return wad[pos : pos + size].decode()
    raise SystemExit(f"no TEXTMAP in {mapname}")


def blocks(text: str, kind: str) -> list[dict]:
    return [
        dict(re.findall(r"(\w+) = ([^;]+);", body))
        for body in re.findall(rf"{kind} \{{ ([^}}]*)\}}", text)
    ]


def floor_z(sector: dict, x: float, y: float) -> float:
    if "floorplane_a" not in sector:
        return float(sector["heightfloor"])
    a, b, c, d = (float(sector[f"floorplane_{k}"]) for k in "abcd")
    return -(a * x + b * y + d) / c


def main() -> int:
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    pk3, mapname, px, py = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    text = textmap_of(pk3, mapname)

    verts = [
        (float(x), float(y))
        for x, y in re.findall(r"vertex \{ x = ([-\d.]+); y = ([-\d.]+);", text)
    ]
    sectors = blocks(text, "sector")
    sides = blocks(text, "sidedef")
    lines = blocks(text, "linedef")
    things = blocks(text, "thing")

    print(f"{mapname} at ({px:.0f}, {py:.0f})")

    solid = []
    for t in things:
        if t.get("type") != TREE_TYPE:
            continue
        d = math.hypot(float(t["x"]) - px, float(t["y"]) - py)
        if d < 200:
            solid.append((d, float(t["x"]), float(t["y"])))
    solid.sort()
    blocking_things = [s for s in solid if s[0] < PLAYER_RADIUS + TREE_RADIUS]
    print(f"\nsolid things within 200 units: {len(solid)}")
    for d, x, y in solid[:6]:
        flag = "  <-- BLOCKING" if d < PLAYER_RADIUS + TREE_RADIUS else ""
        print(f"   tree ({x:.0f},{y:.0f})  {d:.0f} units = {d / 32:.2f} m{flag}")
    if blocking_things:
        print(f"   >>> {len(blocking_things)} tree(s) inside the player's collision radius")

    # Every line whose segment passes within a player radius of the point.
    near = []
    for line in lines:
        v1 = verts[int(line["v1"])]
        v2 = verts[int(line["v2"])]
        dx, dy = v2[0] - v1[0], v2[1] - v1[1]
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else max(0, min(1, ((px - v1[0]) * dx + (py - v1[1]) * dy) / span))
        cx, cy = v1[0] + t * dx, v1[1] + t * dy
        dist = math.hypot(px - cx, py - cy)
        if dist > PLAYER_RADIUS + 24:
            continue
        two_sided = "sideback" in line
        if two_sided:
            s0 = int(sides[int(line["sidefront"])]["sector"])
            s1 = int(sides[int(line["sideback"])]["sector"])
            step = abs(floor_z(sectors[s0], cx, cy) - floor_z(sectors[s1], cx, cy))
            impassable = step > MAX_STEP or "blocking" in line
            near.append((dist, f"two-sided step {step:5.1f}u", impassable, s0, s1))
        else:
            near.append((dist, "ONE-SIDED (solid wall)", True, int(sides[int(line["sidefront"])]["sector"]), -1))
    near.sort()
    hard = [n for n in near if n[2]]
    print(f"\nlines within reach: {len(near)}, impassable: {len(hard)}")
    for dist, what, impassable, s0, s1 in near[:10]:
        mark = "  <-- BLOCKS" if impassable else ""
        tex = sectors[s0].get("texturefloor", "?")
        tex1 = sectors[s1].get("texturefloor", "?") if s1 >= 0 else "-"
        print(f"   {dist:5.1f}u  {what}  [{tex} | {tex1}]{mark}")

    if not blocking_things and not hard:
        print("\nNothing in the map data blocks this point.")
        print("That points at the engine rather than the geometry -- check the floor")
        print("plane normal (c must be positive) before anything else.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
