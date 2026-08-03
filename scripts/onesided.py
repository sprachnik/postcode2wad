"""Find one-sided lines away from the map edge, in every map of a PK3.

    python scripts/onesided.py out/birchington-area.pk3

A one-sided line is drawn solid from its sector's floor to its ceiling, and
outdoor ceilings here are around 130m. So an orphaned edge -- one claimed by a
single face instead of two -- is not a hairline crack. It is a wall the height
of a tower block, textured with whatever the ground beside it is, standing in
the open. Players call them blades, spires and shards; all three reports were
this.

Exists because the equivalent test in `tests/test_smoke.py` runs on one tile
and that tile was the clean one: it had 11 short offenders, forgiven by a
32-unit tolerance, while a neighbouring tile in the same region had 134
including one 9.8m wide. Whole-map invariants have to be swept across a whole
region, and the built PK3 is the only place the real answer lives.

Reads the shipped archive rather than rebuilding, so it checks what actually
went out.
"""

from __future__ import annotations

import math
import re
import struct
import sys
import zipfile

#: On the tile border a one-sided line is correct -- it is the edge of the
#: world, and carries Line_Horizon.
BORDER_TOLERANCE = 1.0


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


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    archive = zipfile.ZipFile(sys.argv[1])
    names = sorted(n[5:-4] for n in archive.namelist() if n.startswith("maps/"))

    total = 0
    for name in names:
        text = textmap(archive, name)
        verts = [
            (float(x), float(y))
            for x, y in re.findall(r"vertex \{ x = ([-\d.]+); y = ([-\d.]+);", text)
        ]
        sectors, sides, lines = blocks(text, "sector"), blocks(text, "sidedef"), blocks(text, "linedef")

        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)

        def on_border(v, lo_x=lo_x, hi_x=hi_x, lo_y=lo_y, hi_y=hi_y) -> bool:
            return (
                abs(v[0] - lo_x) < BORDER_TOLERANCE or abs(v[0] - hi_x) < BORDER_TOLERANCE
                or abs(v[1] - lo_y) < BORDER_TOLERANCE or abs(v[1] - hi_y) < BORDER_TOLERANCE
            )

        found = []
        for line in lines:
            if "sideback" in line:
                continue
            v1, v2 = verts[int(line["v1"])], verts[int(line["v2"])]
            if on_border(v1) and on_border(v2):
                continue
            side = sides[int(line["sidefront"])]
            sector = sectors[int(side["sector"])]
            height = float(sector["heightceiling"]) - float(sector["heightfloor"])
            found.append((
                math.dist(v1, v2) / 32,
                height / 32,
                side.get("texturemiddle", "-").strip('"'),
                v1,
            ))

        found.sort(reverse=True)
        total += len(found)
        mark = "" if not found else "   <-- blades"
        print(f"{name}: {len(found)} interior one-sided lines{mark}")
        for width, height, tex, at in found[:3]:
            print(f"    {width:6.2f}m wide, {height:6.1f}m tall, {tex} at {at}")

    print(f"\n{total} across {len(names)} maps")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
