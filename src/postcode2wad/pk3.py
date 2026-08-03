"""Package a generated map as a PK3.

A PK3 is just a zip with directories GZDoom knows by name. Ours is deliberately
thin:

    maps/MAP01.wad   the marker + TEXTMAP + ENDMAP sandwich
    MAPINFO          level name, sky, fog, music, next map
    textures/*.png   generated art (see art.py), auto-registered by folder
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from . import art
from .wad import build_map_wad

#: Distance haze. This is the single strongest "you are outdoors" cue available:
#: without it every wall in the tile is rendered at identical contrast and the
#: scene reads as a flat backdrop rather than something with depth. The colour
#: must match the sky's horizon (`art.haze_hex`) or the two meet in a visible
#: band. Tuned by eye on a 400m tile at 4 / 8 / 14 / 26: at 26 a house 150m away
#: is a white silhouette with no brick left in it, and at 14 it still loses most
#: of its colour. 8 veils the far edge of the tile without taking the paint off
#: anything you could walk to in twenty seconds.
FOG_DENSITY = 8

#: The sky is fogged much more lightly than the world. At parity the sky flattens
#: to a single grey wash and the clouds disappear.
SKY_FOG = 8

#: 1 = "Bright": no distance-based light falloff. Doom's default darkening makes
#: sense in a corridor and is simply wrong at midday in a field — with it on, a
#: house 100m away is in twilight. Fog now does all the depth work.
LIGHT_MODE = 1

#: Freedoom's D_RUNNIN over a photograph of your street is a strange experience.
#: "-" is MAPINFO for silence.
DEFAULT_MUSIC = "-"

MAPINFO_TEMPLATE = """\
map {map_name} "{title}"
{{
    sky1 = "{sky}"
    music = "{music}"
    cluster = 1
    next = "{map_name}"
    secretnext = "{map_name}"

    lightmode = {light_mode}
    fade = "{haze}"
    outsidefog = "{haze}"
    fogdensity = {fog_density}
    outsidefogdensity = {fog_density}
    skyfog = {sky_fog}
}}
"""


def build_mapinfo(
    map_name: str = "MAP01",
    title: str = "Untitled",
    sky: str = art.SKY,
    music: str = DEFAULT_MUSIC,
    fog_density: int = FOG_DENSITY,
) -> str:
    return MAPINFO_TEMPLATE.format(
        map_name=map_name,
        title=title,
        sky=sky,
        music=music,
        light_mode=LIGHT_MODE,
        haze=art.haze_hex(),
        fog_density=fog_density,
        sky_fog=SKY_FOG,
    )


def write_pk3(
    path: str | Path,
    textmap: str,
    map_name: str = "MAP01",
    title: str = "Untitled",
    sky: str = art.SKY,
    extra_files: dict[str, bytes] | None = None,
    art_seed: int | None = 1,
    fog_density: int = FOG_DENSITY,
) -> Path:
    """Write the PK3. `art_seed=None` skips generated art (and its Pillow cost)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {}
    if art_seed is not None:
        files.update(art.pk3_assets(art_seed))
    files.update(extra_files or {})

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as pk3:
        pk3.writestr(f"maps/{map_name}.wad", build_map_wad(textmap, map_name))
        pk3.writestr("MAPINFO", build_mapinfo(map_name, title, sky, fog_density=fog_density))
        for name, data in files.items():
            pk3.writestr(name, data)

    return path
