import io
import struct
import zipfile

import numpy as np
from PIL import Image

from postcode2wad import art
from postcode2wad.pk3 import write_pk3
from postcode2wad.wad import Lump, build_map_wad, build_wad


def read_dir(data):
    magic, numlumps, infotableofs = struct.unpack_from("<4sii", data, 0)
    entries = []
    for i in range(numlumps):
        pos, size, name = struct.unpack_from("<ii8s", data, infotableofs + i * 16)
        entries.append((name.rstrip(b"\0").decode(), pos, size))
    return magic, entries


def test_header_and_directory():
    data = build_wad([Lump("MARKER"), Lump("DATA", b"hello")])
    magic, entries = read_dir(data)
    assert magic == b"PWAD"
    assert [e[0] for e in entries] == ["MARKER", "DATA"]
    # Marker is zero length; the payload lump reports its real size.
    assert entries[0][2] == 0
    assert entries[1][2] == 5
    pos, size = entries[1][1], entries[1][2]
    assert data[pos : pos + size] == b"hello"


def test_map_wad_lump_order():
    """GZDoom requires marker, then TEXTMAP, then ENDMAP — order is load-bearing."""
    _magic, entries = read_dir(build_map_wad('namespace = "zdoom";\n', "MAP01"))
    assert [e[0] for e in entries] == ["MAP01", "TEXTMAP", "ENDMAP"]


def test_pk3_layout(tmp_path):
    out = write_pk3(tmp_path / "t.pk3", 'namespace = "zdoom";\n', title="Test Level", art_seed=None)
    with zipfile.ZipFile(out) as z:
        # ATTRIBUTION.txt is not optional: the licences behind the geometry
        # require a credit, so it ships even with the generated art turned off.
        assert set(z.namelist()) == {"maps/MAP01.wad", "MAPINFO", "ATTRIBUTION.txt"}
        mapinfo = z.read("MAPINFO").decode()
        assert 'map MAP01 "Test Level"' in mapinfo
        _magic, entries = read_dir(z.read("maps/MAP01.wad"))
        assert [e[0] for e in entries] == ["MAP01", "TEXTMAP", "ENDMAP"]


def test_every_pk3_carries_its_licence_attribution(tmp_path):
    """ODbL s4.3 and OGL v3 both require a credit; nothing else enforces it.

    Every PK3 generated before this existed shipped 42 files and not one word
    of attribution, which is a licence breach that no test or tool would catch.
    """
    out = write_pk3(
        tmp_path / "t.pk3", 'namespace = "zdoom";\n', title="Test", tile_id="bng400-1-2"
    )
    with zipfile.ZipFile(out) as z:
        text = z.read("ATTRIBUTION.txt").decode()

    for required in (
        "OpenStreetMap contributors",
        "openstreetmap.org/copyright",
        "opendatacommons.org/licenses/odbl",
        "Environment Agency",
        "Open Government Licence",
        "Crown copyright",
        "bng400-1-2",           # the tile ID, so the extract is reproducible
    ):
        assert required in text, f"attribution is missing {required!r}"


def test_generated_art_ships_and_matches_the_fog_colour(tmp_path):
    """The sky must be in the zip under textures/, and MAPINFO must agree with it.

    A mismatch between the fog colour and the sky's horizon is not a crash, it is
    a visible band across the skyline — the kind of bug only a screenshot finds,
    so pin it here instead.
    """
    out = write_pk3(tmp_path / "t.pk3", 'namespace = "zdoom";\n')
    with zipfile.ZipFile(out) as z:
        assert f"textures/{art.SKY}.png" in z.namelist()
        mapinfo = z.read("MAPINFO").decode()
        assert f'sky1 = "{art.SKY}"' in mapinfo
        assert f'outsidefog = "{art.haze_hex()}"' in mapinfo


def test_sky_is_seamless_across_the_wrap():
    """A sky tiles around 360 degrees of yaw, so a seam in x is permanent."""
    image = Image.open(io.BytesIO(art.sky_png(seed=7)))
    pixels = np.asarray(image, dtype=float)
    # The join is between the last column and the first. Compare that step
    # against a typical interior step so the test scales with the noise level.
    seam = np.abs(pixels[:, -1] - pixels[:, 0]).mean()
    interior = np.abs(np.diff(pixels, axis=1)).mean()
    assert seam < interior * 3.0
