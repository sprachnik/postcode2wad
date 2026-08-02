"""Package a generated map as a PK3.

A PK3 is just a zip with directories GZDoom knows by name. Ours is deliberately
thin:

    maps/MAP01.wad   the marker + TEXTMAP + ENDMAP sandwich
    MAPINFO          level name, sky, music, next map
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .wad import build_map_wad

MAPINFO_TEMPLATE = """\
map {map_name} "{title}"
{{
    sky1 = "{sky}"
    music = "{music}"
    cluster = 1
    next = "{map_name}"
    secretnext = "{map_name}"
}}
"""


def build_mapinfo(
    map_name: str = "MAP01",
    title: str = "Untitled",
    sky: str = "SKY1",
    music: str = "D_RUNNIN",
) -> str:
    return MAPINFO_TEMPLATE.format(map_name=map_name, title=title, sky=sky, music=music)


def write_pk3(
    path: str | Path,
    textmap: str,
    map_name: str = "MAP01",
    title: str = "Untitled",
    sky: str = "SKY1",
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as pk3:
        pk3.writestr(f"maps/{map_name}.wad", build_map_wad(textmap, map_name))
        pk3.writestr("MAPINFO", build_mapinfo(map_name, title, sky))
        for name, data in (extra_files or {}).items():
            pk3.writestr(name, data)

    return path
