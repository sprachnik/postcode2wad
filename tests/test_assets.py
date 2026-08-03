"""Every texture a map names must actually exist somewhere.

GZDoom does not refuse to load a map with a missing texture — it substitutes a
placeholder, logs a line, and carries on. On a 14,000-linedef map that line
scrolls past unnoticed and the result is a wall rendered in the wrong material
somewhere you happen not to be standing. Checking statically is both faster and
more reliable than looking for it.

Skipped when the Freedoom IWAD is not present, since tools/ is gitignored.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path

import pytest

from postcode2wad import art, textures
from postcode2wad.demo import build_m0_geometry
from postcode2wad.pk3 import write_pk3
from postcode2wad.udmf import emit_textmap

IWAD = Path("tools/freedoom/freedoom-0.13.0/freedoom2.wad")

#: Not a texture: the engine special that makes a ceiling render as sky.
SPECIAL = {"F_SKY1", "-", ""}


def iwad_lump_names() -> set[str]:
    """Every name the engine can resolve out of the IWAD.

    Two populations, and missing the second is an easy mistake: flats are real
    directory lumps, but *wall* textures are composites assembled from patches
    and exist only as entries inside the TEXTURE1 lump. Looking at the directory
    alone reports every wall texture in Doom as missing.
    """
    data = IWAD.read_bytes()
    _magic, count, directory = struct.unpack_from("<4sii", data, 0)

    names, lumps = set(), {}
    for index in range(count):
        pos, size, raw = struct.unpack_from("<ii8s", data, directory + index * 16)
        name = raw.rstrip(b"\0").decode("ascii", "replace").upper()
        names.add(name)
        lumps[name] = (pos, size)

    for table in ("TEXTURE1", "TEXTURE2"):
        if table not in lumps:
            continue
        pos, size = lumps[table]
        blob = data[pos : pos + size]
        entries = struct.unpack_from("<i", blob, 0)[0]
        for offset in struct.unpack_from(f"<{entries}i", blob, 4):
            names.add(blob[offset : offset + 8].rstrip(b"\0").decode("ascii", "replace").upper())

    return names


def test_generated_asset_names_are_unique():
    """Two assets writing the same path would silently shadow one another."""
    assets = art.pk3_assets(1)
    stems = [Path(name).stem for name in assets]
    assert len(stems) == len(set(stems))


def test_every_name_textures_py_uses_is_generated_or_in_freedoom():
    """The names module must not reference art that does not exist."""
    if not IWAD.exists():
        pytest.skip("Freedoom IWAD not present")

    available = iwad_lump_names() | {Path(n).stem.upper() for n in art.pk3_assets(1)}
    referenced = {
        value.upper()
        for name, value in vars(textures).items()
        if not name.startswith("_") and isinstance(value, str) and name.isupper()
    }
    for mapping in (textures.LAND_COVER, textures.BUILDING_WALLS, textures.BARRIER_WALLS):
        referenced |= {value.upper() for value in mapping.values()}

    missing = sorted(referenced - available - SPECIAL)
    assert not missing, f"textures.py names lumps that do not exist: {missing}"


def test_every_facade_building_facade_can_return_is_generated():
    """`building_facade` composes a name from a material key and a storey count.

    Composed names are the easy ones to get wrong — nothing references them as a
    literal, so a key added to FACADE_KEYS without a matching Facade would only
    show up as a blank wall in game.
    """
    shipped = {Path(name).stem for name in art.pk3_assets(1)}
    keys = set(textures.FACADE_KEYS.values()) | set(textures.DOMESTIC_FACADES)

    for key in keys:
        for height_m in (2.4, 6.5, 9.0, 14.0, 30.0):
            name, scale_y = textures.building_facade({"building": "yes"}, 0, height_m)
            assert scale_y > 0
            composed = art.facade_name(key, textures.building_storeys(height_m))
            assert composed in shipped, f"{composed} is referenced but never generated"
            assert name in shipped


def test_facade_covers_the_wall_exactly_once():
    """scaley must map the texture onto the wall 1:1, top to bottom.

    This is the whole reason windows land at believable heights. Get it wrong
    and the texture either tiles (two rows of eaves) or crops (no ground floor).
    """
    for height_m in (2.6, 6.5, 9.3, 12.4):
        _name, scale_y = textures.building_facade({"building": "house"}, 1, height_m)
        storeys = textures.building_storeys(height_m)
        rendered_units = (storeys * art.STOREY_PX) / scale_y
        assert rendered_units == pytest.approx(height_m * 32, rel=1e-6)


def test_generated_map_references_only_existing_textures(tmp_path):
    if not IWAD.exists():
        pytest.skip("Freedoom IWAD not present")

    out = write_pk3(tmp_path / "t.pk3", emit_textmap(build_m0_geometry(size_m=200.0)))
    with zipfile.ZipFile(out) as pk3:
        shipped = {Path(n).stem.upper() for n in pk3.namelist() if "/" in n}
        wad = pk3.read("maps/MAP01.wad")

    _magic, count, directory = struct.unpack_from("<4sii", wad, 0)
    textmap = b""
    for index in range(count):
        pos, size, raw = struct.unpack_from("<ii8s", wad, directory + index * 16)
        if raw.rstrip(b"\0") == b"TEXTMAP":
            textmap = wad[pos : pos + size]

    used = {
        value.decode().upper() for value in re.findall(rb'texture\w+ = "([^"]*)"', textmap)
    }
    missing = sorted(used - iwad_lump_names() - shipped - SPECIAL)
    assert not missing, f"map references lumps that do not exist: {missing}"


def test_tree_sprite_carries_bottom_centre_offsets():
    """A Doom sprite hangs downward from its top offset.

    With the default offsets Pillow writes (none at all, since grAb is not a
    standard PNG chunk) the engine reads zero and draws the whole tree below the
    floor. Nothing in the map format catches this — it just renders wrong.
    """
    png = art.tree_png(1)
    index = png.find(b"grAb")
    assert index != -1, "sprite has no grAb chunk"

    x_offset, y_offset = struct.unpack_from(">ii", png, index + 4)
    assert x_offset == art.TREE_PX // 2, "trunk must be centred on the thing"
    assert y_offset == art.TREE_PX, "sprite must sit entirely above the actor's feet"


def test_tree_png_still_decodes_with_the_extra_chunk():
    """grAb is inserted by hand, so prove the file is still a valid PNG."""
    from PIL import Image

    image = Image.open(io.BytesIO(art.tree_png(1)))
    assert image.mode == "RGBA"
    assert image.size == (art.TREE_PX, art.TREE_PX)
    # A billboard with no transparency would render as an opaque square.
    assert image.getchannel("A").getextrema()[0] == 0
