import struct
import zipfile

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
    _magic, entries = read_dir(build_map_wad("namespace = \"zdoom\";\n", "MAP01"))
    assert [e[0] for e in entries] == ["MAP01", "TEXTMAP", "ENDMAP"]


def test_pk3_layout(tmp_path):
    out = write_pk3(tmp_path / "t.pk3", 'namespace = "zdoom";\n', title="Test Level")
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == {"maps/MAP01.wad", "MAPINFO"}
        mapinfo = z.read("MAPINFO").decode()
        assert 'map MAP01 "Test Level"' in mapinfo
        _magic, entries = read_dir(z.read("maps/MAP01.wad"))
        assert [e[0] for e in entries] == ["MAP01", "TEXTMAP", "ENDMAP"]
