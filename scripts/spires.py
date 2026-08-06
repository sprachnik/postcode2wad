"""Find sectors standing proud of everything around them, in every map of a PK3.

    python scripts/spires.py out/birchington.pk3
    python scripts/spires.py out/thanet.pk3 --min-rise 3 --top 40

A spire is a patch of ground at a height nothing near it agrees with: a thin
green blade going up into the sky, or a shard sticking out of a roofline. They
are reported by players constantly and have been the most expensive class of
defect on this project.

**Why this is not the check in `tests/test_smoke.py`.** That one walks *pairs*
of sectors and measures the step across the edge they share, which finds a
plane that disagrees with its neighbour. It cannot see a sector standing above
*all* of its neighbours at once, and it skips any sector without a floor plane
-- so a sliver too degenerate to fit one, which is exactly the shape that goes
wrong, is invisible to it. Both blind spots were found the same way: by
somebody looking at a screenshot and saying "there's a spire", on a tile where
every existing check reported clean. On Birchington the pairwise test found
nothing above 1.97m while the map had visible blades in it.

So this asks a different question, per sector rather than per edge:

    how far does this sector stand above the *highest* thing touching it?

Ground that steps normally has a neighbour at nearly its own height, so the
answer is near zero however steep the hillside. A spire has none, because it is
an artefact rather than part of the surface, and the answer is its full height.

Reads the built archive rather than rebuilding, so it reports what shipped, and
prints an OSGB position for every offender so you can go and stand next to one.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zipfile

UNITS_PER_METRE = 32.0

#: Surfaces that are *meant* to stand above their surroundings. A roof is a
#: building and a hedge top is a hedge; counting either as a spire buries the
#: real ones under thousands of correct ones, which is the mistake the smoke
#: test's own docstring records making once already.
EXPECTED_HIGH = {"CEIL5_2", "DMHEDGT", "DMWALLC"}


def textmap(archive: zipfile.ZipFile, name: str) -> str:
    raw = archive.read(name)
    count, offset = struct.unpack("<ii", raw[4:12])
    for index in range(count):
        entry = offset + index * 16
        pos, size, lump = struct.unpack("<ii8s", raw[entry : entry + 16])
        if lump.rstrip(b"\0") == b"TEXTMAP":
            return raw[pos : pos + size].decode("utf-8", "replace")
    raise SystemExit(f"no TEXTMAP in {name}")


def blocks(text: str, kind: str) -> list[dict]:
    out = []
    for body in re.findall(rf"{kind}\s*\{{(.*?)\}}", text, re.DOTALL):
        entry: dict[str, str] = {}
        for key, value in re.findall(r"(\w+)\s*=\s*([^;]+);", body):
            entry[key] = value.strip().strip('"')
        out.append(entry)
    return out


def floor_at(sector: dict, x: float, y: float) -> float:
    """The sector's floor at a point — from its plane if it has one.

    Falling back to `heightfloor` matters: a sector with no plane is one whose
    fit was refused, and those are over-represented among the offenders. Reading
    them is the whole point of this script.
    """
    if "floorplane_a" in sector:
        a = float(sector["floorplane_a"])
        b = float(sector["floorplane_b"])
        c = float(sector["floorplane_c"])
        d = float(sector["floorplane_d"])
        if c:
            return -(a * x + b * y + d) / c
    return float(sector.get("heightfloor", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pk3")
    parser.add_argument(
        "--min-rise",
        type=float,
        default=2.0,
        metavar="M",
        help="report a sector standing this far above every neighbour (default 2)",
    )
    parser.add_argument("--top", type=int, default=20, help="how many to list")
    args = parser.parse_args()

    archive = zipfile.ZipFile(args.pk3)
    maps = sorted(n for n in archive.namelist() if n.startswith("maps/"))
    total = 0

    for name in maps:
        text = textmap(archive, name)
        verts = [(float(v["x"]), float(v["y"])) for v in blocks(text, "vertex")]
        sides = blocks(text, "sidedef")
        sectors = blocks(text, "sector")

        # Highest neighbour touching each sector, sampled at both ends of every
        # shared edge — a plane can agree at one end and not the other.
        best: dict[int, float] = {}
        own: dict[int, float] = {}
        for line in blocks(text, "linedef"):
            if "sideback" not in line:
                continue
            a = int(sides[int(line["sidefront"])]["sector"])
            b = int(sides[int(line["sideback"])]["sector"])
            if a == b:
                continue
            for vi in (int(line["v1"]), int(line["v2"])):
                x, y = verts[vi]
                za = floor_at(sectors[a], x, y)
                zb = floor_at(sectors[b], x, y)
                own[a] = max(own.get(a, -1e9), za)
                own[b] = max(own.get(b, -1e9), zb)
                best[a] = max(best.get(a, -1e9), zb)
                best[b] = max(best.get(b, -1e9), za)

        # Where each sector sits, and how high, for the neighbourhood pass.
        centre: dict[int, tuple[float, float]] = {}
        acc: dict[int, list[float]] = {}
        for line in blocks(text, "linedef"):
            for key in ("sidefront", "sideback"):
                if key not in line:
                    continue
                si = int(sides[int(line[key])]["sector"])
                for vi in (int(line["v1"]), int(line["v2"])):
                    acc.setdefault(si, []).append(verts[vi][0])
                    acc.setdefault(-si - 1, []).append(verts[vi][1])
        for si, xs in acc.items():
            if si >= 0:
                ys = acc.get(-si - 1, [0.0])
                centre[si] = (sum(xs) / len(xs), sum(ys) / len(ys))

        # Bucket by a coarse grid so "nearby" is cheap to ask.
        CELL = 40.0 * UNITS_PER_METRE
        grid: dict[tuple[int, int], list[int]] = {}
        for si, (x, y) in centre.items():
            grid.setdefault((int(x // CELL), int(y // CELL)), []).append(si)

        found = []
        for index, sector in enumerate(sectors):
            if index not in best or sector.get("texturefloor") in EXPECTED_HIGH:
                continue

            # 1. Standing above everything it touches.
            rise = (own[index] - best[index]) / UNITS_PER_METRE

            # 2. Standing above the ground *around* it. Two adjacent slivers
            #    both sticking up hide each other from the test above — each is
            #    the other's high neighbour — so compare against the median of
            #    the neighbourhood, which a handful of artefacts cannot move.
            x, y = centre[index]
            cx, cy = int(x // CELL), int(y // CELL)
            local = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((cx + dx, cy + dy), ()):
                        if other != index and sectors[other].get(
                            "texturefloor"
                        ) not in EXPECTED_HIGH:
                            local.append(floor_at(sectors[other], x, y))
            if len(local) >= 8:
                local.sort()
                median = local[len(local) // 2]
                rise = max(rise, (own[index] - median) / UNITS_PER_METRE)

            if rise >= args.min_rise:
                found.append((rise, index, sector.get("texturefloor", "?"), centre[index]))

        found.sort(reverse=True)
        total += len(found)
        label = name.split("/")[-1].split(".")[0]
        print(f"{label}: {len(found)} sectors stand >{args.min_rise:g}m above every neighbour")
        for rise, index, tex, (x, y) in found[: args.top]:
            # Map units, which is what scripts/whystuck.py takes and what the
            # HUD telemetry reports — so an offender can be walked to.
            print(
                f"    {rise:8.2f}m  sector {index:6d}  {tex:9s}"
                f"  at x={x:.0f} y={y:.0f}"
            )

    print(f"\n{total} across {len(maps)} maps")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
