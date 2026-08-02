"""Minimal PWAD writer.

A WAD is a header, a blob of lump data, then a directory. We only ever need to
write one: the tiny `maps/MAP01.wad` that lives inside the PK3 and holds the
map marker, the UDMF TEXTMAP and the ENDMAP terminator.

    header      4s  "PWAD"
                i   number of lumps
                i   byte offset of the directory
    directory   i   byte offset of this lump's data
                i   lump size in bytes
                8s  lump name, NUL-padded, uppercase
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = struct.Struct("<4sii")
DIRENT = struct.Struct("<ii8s")


@dataclass(frozen=True)
class Lump:
    name: str
    data: bytes = b""

    def __post_init__(self) -> None:
        if len(self.name) > 8:
            raise ValueError(f"lump name {self.name!r} exceeds 8 characters")


def build_wad(lumps: list[Lump]) -> bytes:
    """Serialise lumps into a PWAD. Zero-length lumps are legal markers."""
    body = bytearray()
    offsets = []
    for lump in lumps:
        offsets.append(HEADER.size + len(body))
        body += lump.data

    directory = bytearray()
    for lump, offset in zip(lumps, offsets, strict=True):
        name = lump.name.upper().encode("ascii").ljust(8, b"\0")
        directory += DIRENT.pack(offset if lump.data else 0, len(lump.data), name)

    header = HEADER.pack(b"PWAD", len(lumps), HEADER.size + len(body))
    return bytes(header + body + directory)


def build_map_wad(textmap: str, map_name: str = "MAP01") -> bytes:
    """Wrap a UDMF TEXTMAP in the marker/ENDMAP sandwich GZDoom expects.

    Lump order is significant: the map marker must come first, TEXTMAP must
    follow it, and ENDMAP closes the map. Any extra lumps (SCRIPTS, BEHAVIOR,
    ZNODES) go between TEXTMAP and ENDMAP.
    """
    return build_wad(
        [
            Lump(map_name),
            Lump("TEXTMAP", textmap.encode("utf-8")),
            Lump("ENDMAP"),
        ]
    )
