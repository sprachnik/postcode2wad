"""The browser demo's two-language seams.

The crossing between web tiles runs through three artifacts that have to agree
and cannot check each other at runtime: the ZScript prints telemetry, the
launcher parses it with a JavaScript regex, and the arrival comes back as
cvars the ZScript reads by name. Every one of those handshakes fails *quietly*
-- an unmatched line is no crossing, an unknown cvar is a normal spawn -- and
none of it is exercised by anything that runs outside a browser. So it is
pinned here, from the files themselves rather than from copies.
"""

import re
from pathlib import Path

import pytest

from postcode2wad.hud import (
    CVARINFO,
    DIR_EAST,
    DIR_NORTH,
    DIR_SOUTH,
    DIR_WEST,
    ENTRY_CVARS,
    ENTRY_Z_BIAS,
    WEAPON_CVAR,
    WEAPON_DEFAULT,
    WEAPON_FIST,
    build_zscript,
)
from postcode2wad.tiles import Tile

PLAY_HTML = Path(__file__).resolve().parent.parent / "scripts" / "webdemo" / "play.html"


@pytest.fixture(scope="module")
def launcher() -> str:
    return PLAY_HTML.read_text(encoding="utf-8")


def zscript() -> str:
    return build_zscript([Tile(782, 208, 800)], ["MAP01"])


def test_every_entry_cvar_is_declared_read_and_sent(launcher):
    """Three names for one thing, in three languages, none of them checked.

    A cvar the CVARINFO does not declare has no default; one the ZScript does
    not read is ignored; one the launcher does not send never arrives. Each
    failure looks identical from the player's seat: you cross a seam and come
    out at the map's spawn point instead of the street you were on.
    """
    script = zscript()
    for name in ENTRY_CVARS:
        assert f"int {name} " in CVARINFO, f"{name} is not declared in CVARINFO"
        assert f'"{name}"' in script, f"{name} is never read by the ZScript"
        assert f"'{name}'" in launcher, f"{name} is never sent by the launcher"


def test_weapon_cvar_defaults_to_the_fist():
    """The demo opens with a hand, not a gun -- it is a map, not a shooter."""
    assert f"int {WEAPON_CVAR} = {WEAPON_DEFAULT};" in CVARINFO
    assert WEAPON_DEFAULT == WEAPON_FIST


def telemetry_line(**kw) -> str:
    """One P2W TLM line, in the exact format the generated ZScript prints.

    Taken from the script rather than written out here: a copy would keep
    passing after the real format string changed, which is precisely the bug
    this file exists to catch.
    """
    fmt = re.search(r'Console\.Printf\("(P2W TLM [^"]+)"', zscript()).group(1)
    values = {
        "map": "MAP01", "x": 12345, "y": 9000, "z": 500,
        "lat": 51.351773, "lng": 1.244091, "ang": 270, "fwd": 100, "side": 0,
        "spd": 6.3,
    }
    values.update(kw)
    # ZScript's Printf is C's, so the same conversions work in Python -- with
    # the lat/lng pair spliced in first, because they arrive as one preformatted
    # %s from FmtLatLng.
    latlng = f"lat={values['lat']:.6f} lng={values['lng']:.6f}"
    return fmt % (
        values["map"], values["x"], values["y"], values["z"], latlng,
        values["ang"], values["fwd"], values["side"], values["spd"],
    )


def launcher_tlm_regex(launcher: str) -> re.Pattern:
    """The launcher's own regex, lifted out of the page and compiled here.

    Its syntax is a subset both languages share; nothing in it is
    JavaScript-only, and if that ever stops being true this raises instead of
    quietly testing something else.
    """
    src = re.search(r"const TLM_RE = /(.+?)/;", launcher).group(1)
    return re.compile(src)


def test_the_launcher_can_read_the_telemetry_it_navigates_by(launcher):
    """The crossing is driven entirely by parsing this one line.

    It has already been broken once by a formatting change made three files
    away (`"%.3f"` on plane normals), and nothing in the browser reports a
    regex that stopped matching: the player just walks into the tile edge and
    stands there.
    """
    m = launcher_tlm_regex(launcher).search(telemetry_line())
    assert m, "the launcher's TLM_RE does not match the line the ZScript prints"
    assert [int(g) for g in m.groups()] == [12345, 9000, 500, 270]


def test_telemetry_parse_survives_a_negative_coordinate(launcher):
    """Below the ordnance datum is real ground here, and z goes negative on it."""
    m = launcher_tlm_regex(launcher).search(telemetry_line(z=-64, ang=0))
    assert m and int(m.group(3)) == -64


def test_the_arrival_edge_is_mirrored_by_the_script_not_the_launcher(launcher):
    """One copy of the mirroring rule, in the language that owns the constants.

    The launcher sends the departure position untouched plus the direction of
    travel; the ZScript turns that into an arrival point. If the launcher ever
    starts sending a finished point instead, the rule exists twice, in two
    languages, and the copies are free to drift apart without either looking
    wrong at the place it is written.
    """
    script = zscript()
    assert "pendingX = REENTRY" in script and "pendingY = REENTRY" in script

    hop = re.search(r"function hopUrl\(.*?\n}", launcher, re.DOTALL).group(0)
    assert "lastTlm.x" in hop and "lastTlm.y" in hop
    assert "DIR_CODE[dir]" in hop


def test_nothing_the_launcher_sends_can_be_negative(launcher):
    """A negative value does not survive GZDoom's command line.

    It splits on arguments beginning with `-`, so `+set p2w_entry_z -400` is
    read as a `set` with no value followed by an unknown `-400` parameter: the
    cvar keeps its default and *nothing is logged*. Measured on a real launch
    2026-08-04, after a westward crossing arrived on the wrong side of the
    tile. The encoding is what stops it happening again, so the encoding is
    what is pinned: a compass index instead of (dx, dy), and a biased z.
    """
    assert DIR_CODE(launcher) == {
        "north": DIR_NORTH, "south": DIR_SOUTH, "east": DIR_EAST, "west": DIR_WEST,
    }
    assert re.search(r"const Z_BIAS = (\d+);", launcher).group(1) == str(ENTRY_Z_BIAS)

    hop = re.search(r"function hopUrl\(.*?\n}", launcher, re.DOTALL).group(0)
    assert "Math.max(0, lastTlm.x)" in hop and "Math.max(0, lastTlm.y)" in hop
    assert "lastTlm.z + Z_BIAS" in hop
    # ang is normalised into 0..359 where it is read, not where it is sent.
    assert "% 360) + 360) % 360" in launcher

    # And the script has to take the bias back off, or every arrival is 2km
    # underground and fails the floor check it exists to pass.
    assert f'IntCVar("p2w_entry_z", {ENTRY_Z_BIAS}) - {ENTRY_Z_BIAS}' in zscript()


def DIR_CODE(launcher: str) -> dict:
    """The launcher's compass table, read out of the page."""
    src = re.search(r"const DIR_CODE = \{(.*?)\};", launcher, re.DOTALL).group(1)
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"(\w+):\s*(\d+)", src)
    }
