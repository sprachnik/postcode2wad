"""Measure how choppy each surface is, in every map of a PK3.

    python scripts/flatness.py out/birchington-area.pk3

The symptom a player reports as "the path isn't flat" is not a height error --
every join is exact to the unit, because neighbouring triangles are fitted
through the same two corners. It is the *angle* between neighbouring floor
planes: a surface whose normal swings 30 degrees from one triangle to the next
reads as chop underfoot however well the seams line up.

So this measures the dihedral angle across every join between two sloped
sectors wearing the same floor flat, grouped by that flat. Same-material only,
because a pavement meeting the grass beside it is *meant* to change angle;
what is wrong is a pavement disagreeing with the next slab of the same
pavement.

Faces under `MIN_AREA_M2` are excluded from both sides. The arrangement is full
of triangles a few centimetres across, and their angles are dominated by which
DTM pixel their corners happened to land in rather than by anything a player
can stand on -- including them buries the real signal under noise.

Reads the shipped archive rather than rebuilding, so it measures what actually
went out.
"""

from __future__ import annotations

import collections
import math
import re
import struct
import sys
import zipfile

UNITS_PER_METRE = 32

#: Below this a face is smaller than the player's own footprint.
MIN_AREA_M2 = 2.0

#: The surfaces worth naming separately. Everything else is summarised.
INTEREST = ("DMTARMAC", "DMPAVE", "DMGRASS")


def textmap(archive: zipfile.ZipFile, name: str) -> str:
    wad = archive.read(f"maps/{name}.wad")
    _magic, count, directory = struct.unpack_from("<4sii", wad, 0)
    for index in range(count):
        pos, size, raw = struct.unpack_from("<ii8s", wad, directory + index * 16)
        if raw.rstrip(b"\0") == b"TEXTMAP":
            return wad[pos : pos + size].decode()
    raise SystemExit(f"no TEXTMAP in {name}")


def blocks(text: str, kind: str) -> list[dict]:
    return [
        dict(re.findall(r"(\w+) = ([^;]+);", body))
        for body in re.findall(rf"{kind} \{{ ([^}}]*)\}}", text)
    ]


def sector_areas(verts, sectors, sides, lines) -> list[float]:
    """Shoelace area per sector, in square metres.

    A sector's boundary is exactly its sidedefs' lines. `geometry.py` emits
    v1=q, v2=p for a ring segment p->q whose face interior lies left of p->q,
    so the face is on the RIGHT of v1->v2 for the front side. Walking v2->v1
    therefore goes anticlockwise round the front sector, and v1->v2 goes
    anticlockwise round the back one. Summing the cross terms over those
    directed edges gives twice the signed area, holes included.
    """
    doubled = [0.0] * len(sectors)
    for line in lines:
        v1, v2 = verts[int(line["v1"])], verts[int(line["v2"])]
        for key, (a, b) in (("sidefront", (v2, v1)), ("sideback", (v1, v2))):
            if key not in line:
                continue
            index = int(sides[int(line[key])]["sector"])
            doubled[index] += a[0] * b[1] - b[0] * a[1]
    return [abs(d) / 2.0 / UNITS_PER_METRE**2 for d in doubled]


def normal(sector: dict):
    if "floorplane_a" not in sector:
        return None
    return (
        float(sector["floorplane_a"]),
        float(sector["floorplane_b"]),
        float(sector["floorplane_c"]),
    )


def percentile(values: list[float], fraction: float) -> float:
    return values[min(len(values) - 1, int(fraction * len(values)))]


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    archive = zipfile.ZipFile(sys.argv[1])
    names = sorted(n[5:-4] for n in archive.namelist() if n.startswith("maps/"))

    by_flat: dict[str, list[float]] = collections.defaultdict(list)
    for name in names:
        text = textmap(archive, name)
        verts = [
            (float(x), float(y))
            for x, y in re.findall(r"vertex \{ x = ([-\d.]+); y = ([-\d.]+);", text)
        ]
        sectors = blocks(text, "sector")
        sides = blocks(text, "sidedef")
        lines = blocks(text, "linedef")
        areas = sector_areas(verts, sectors, sides, lines)
        normals = [normal(s) for s in sectors]
        flats = [s.get("texturefloor", "").strip('"') for s in sectors]

        seen = set()
        for line in lines:
            if "sideback" not in line:
                continue
            a = int(sides[int(line["sidefront"])]["sector"])
            b = int(sides[int(line["sideback"])]["sector"])
            if a == b or (min(a, b), max(a, b)) in seen:
                continue
            seen.add((min(a, b), max(a, b)))
            if flats[a] != flats[b] or normals[a] is None or normals[b] is None:
                continue
            if areas[a] < MIN_AREA_M2 or areas[b] < MIN_AREA_M2:
                continue
            dot = sum(u * v for u, v in zip(normals[a], normals[b], strict=True))
            by_flat[flats[a]].append(math.degrees(math.acos(max(-1.0, min(1.0, dot)))))

    print(f"{'flat':10} {'joins':>7} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}")
    ordered = sorted(by_flat, key=lambda f: (f not in INTEREST, -len(by_flat[f])))
    for flat in ordered:
        angles = sorted(by_flat[flat])
        print(
            f"{flat:10} {len(angles):7} {percentile(angles, 0.50):6.1f}° "
            f"{percentile(angles, 0.95):6.1f}° {percentile(angles, 0.99):6.1f}° "
            f"{max(angles):6.1f}°"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
