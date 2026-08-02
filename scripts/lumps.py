"""Inspect an IWAD/PWAD: list lumps, flats, and wall textures.

Used to verify that the texture names the generator emits actually exist in
Freedoom, rather than assuming Doom 2 names carry over.

    python scripts/lumps.py tools/freedoom/freedoom-0.13.0/freedoom2.wad
    python scripts/lumps.py <wad> --check GRASS1 BRICK7 F_SKY1
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def read_directory(data: bytes) -> list[tuple[str, int, int]]:
    magic, numlumps, infotableofs = struct.unpack_from("<4sii", data, 0)
    if magic not in (b"IWAD", b"PWAD"):
        raise ValueError(f"not a WAD: {magic!r}")
    out = []
    for i in range(numlumps):
        filepos, size, name = struct.unpack_from("<ii8s", data, infotableofs + i * 16)
        out.append((name.rstrip(b"\0").decode("ascii", "replace"), filepos, size))
    return out


def between(entries, start_markers, end_markers) -> list[str]:
    """Collect lump names inside marker pairs, e.g. F_START .. F_END."""
    names, inside = [], False
    for name, _pos, size in entries:
        if name in start_markers:
            inside = True
            continue
        if name in end_markers:
            inside = False
            continue
        if inside and size > 0:
            names.append(name)
    return names


def parse_texture_lump(data: bytes) -> list[str]:
    """TEXTUREx: int32 count, then count int32 offsets, each to an 8-byte name."""
    if len(data) < 4:
        return []
    (count,) = struct.unpack_from("<i", data, 0)
    names = []
    for i in range(count):
        (offset,) = struct.unpack_from("<i", data, 4 + i * 4)
        name = struct.unpack_from("<8s", data, offset)[0]
        names.append(name.rstrip(b"\0").decode("ascii", "replace"))
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wad")
    ap.add_argument("--check", nargs="*", default=[], help="names to test for existence")
    ap.add_argument("--grep", default="", help="substring filter for the listing")
    ap.add_argument("--list", choices=["flats", "textures", "all"], default="")
    args = ap.parse_args()

    data = Path(args.wad).read_bytes()
    entries = read_directory(data)
    by_name = {name: (pos, size) for name, pos, size in entries}

    flats = between(entries, {"F_START", "FF_START"}, {"F_END", "FF_END"})

    textures: list[str] = []
    for lump in ("TEXTURE1", "TEXTURE2"):
        if lump in by_name:
            pos, size = by_name[lump]
            textures += parse_texture_lump(data[pos : pos + size])

    print(
        f"{Path(args.wad).name}: {len(entries)} lumps, {len(flats)} flats, {len(textures)} textures"
    )

    if args.list in ("flats", "all"):
        print("\n-- FLATS --")
        for n in sorted(set(flats)):
            if args.grep.upper() in n:
                print(" ", n)
    if args.list in ("textures", "all"):
        print("\n-- TEXTURES --")
        for n in sorted(set(textures)):
            if args.grep.upper() in n:
                print(" ", n)

    if args.check:
        print("\n-- CHECK --")
        flatset, texset = set(flats), set(textures)
        for n in args.check:
            n = n.upper()
            where = []
            if n in flatset:
                where.append("flat")
            if n in texset:
                where.append("texture")
            if n in by_name and not where:
                where.append("lump")
            print(f"  {n:<10} {'+ ' + ', '.join(where) if where else '** MISSING **'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
