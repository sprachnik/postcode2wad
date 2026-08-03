"""Procedurally generated art, shipped inside the PK3.

GZDoom auto-registers anything under `textures/` in a PK3 as a wall texture and
anything under `flats/` as a flat, named after the file with the extension
stripped. So a PNG dropped into the zip is usable from UDMF immediately — no
TEXTURES lump, no patch definitions, and no 8-character name limit.

That matters more than it sounds. Freedoom is a *demon shooter's* texture set:
it has no daylight sky, no domestic brickwork, and above all no windows. A
building with no windows does not read as a building, it reads as a wall, which
is exactly what the first M1 screenshots looked like. Generating our own art is
therefore not decoration — it is the difference between a map of a place and a
maze that happens to share its floorplan.

Everything here is deterministic: seeded from a caller-supplied integer, so the
same tile always produces byte-identical art and the golden-file test stays
meaningful.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

#: Texture names we generate. Chosen not to collide with any Freedoom lump.
SKY = "DMSKY"

SKY_WIDTH = 1024
SKY_HEIGHT = 512

#: The colour the sky fades to at the horizon. Distance fog is set to the same
#: value in MAPINFO, so a wall at the far edge of the tile dissolves into the
#: skyline instead of standing against it as a hard silhouette. If you change
#: one of these, change both — a mismatch is instantly visible as a band across
#: the horizon.
HAZE = (176, 197, 216)
ZENITH = (68, 118, 184)

_CLOUD_LIT = (252, 252, 250)
_CLOUD_SHADE = (168, 176, 190)


def haze_hex() -> str:
    """The haze colour as MAPINFO wants it: six hex digits, no leading hash."""
    return "{:02X}{:02X}{:02X}".format(*HAZE)


def _smoothstep(t: np.ndarray) -> np.ndarray:
    return t * t * (3.0 - 2.0 * t)


def _value_noise(
    rng: np.random.Generator,
    height: int,
    width: int,
    cx: int,
    cy: int,
    wrap_y: bool = False,
) -> np.ndarray:
    """Bilinear value noise on a lattice that wraps horizontally.

    The wrap matters: a Doom sky texture is tiled around the full 360 degrees of
    yaw, so any seam in x is a permanent vertical scar in the sky that the player
    can turn around and find. Duplicating the first lattice column onto the end
    makes the interpolation continuous across the join.

    Flats need the same guarantee in both axes — a field is the same texture
    tiled hundreds of times across a tile, and a seam becomes a visible grid.
    """
    lattice = rng.random((cy if wrap_y else cy + 1, cx))
    lattice = np.concatenate([lattice, lattice[:, :1]], axis=1)
    if wrap_y:
        lattice = np.concatenate([lattice, lattice[:1, :]], axis=0)

    xs = np.linspace(0.0, cx, width, endpoint=False)
    ys = np.linspace(0.0, cy, height, endpoint=False)
    x0 = np.floor(xs).astype(int)
    y0 = np.floor(ys).astype(int)
    fx = _smoothstep(xs - x0)[None, :]
    fy = _smoothstep(ys - y0)[:, None]

    v00 = lattice[np.ix_(y0, x0)]
    v01 = lattice[np.ix_(y0, x0 + 1)]
    v10 = lattice[np.ix_(y0 + 1, x0)]
    v11 = lattice[np.ix_(y0 + 1, x0 + 1)]

    top = v00 * (1.0 - fx) + v01 * fx
    bottom = v10 * (1.0 - fx) + v11 * fx
    return top * (1.0 - fy) + bottom * fy


def _fbm(
    rng: np.random.Generator,
    height: int,
    width: int,
    cx: int,
    cy: int,
    octaves: int = 5,
    wrap_y: bool = False,
) -> np.ndarray:
    """Fractional Brownian motion — octaves of value noise at halving amplitude."""
    total = np.zeros((height, width))
    amplitude, norm = 1.0, 0.0
    for octave in range(octaves):
        total += amplitude * _value_noise(
            rng, height, width, cx << octave, cy << octave, wrap_y
        )
        norm += amplitude
        amplitude *= 0.5
    return total / norm


def sky_png(seed: int = 1, width: int = SKY_WIDTH, height: int = SKY_HEIGHT) -> bytes:
    """A daylight sky with cumulus, tileable in x.

    Row 0 is the zenith and the last row is the horizon, which is how GZDoom
    maps a sky texture onto the upper hemisphere.
    """
    rng = np.random.default_rng(seed)

    # Vertical gradient, zenith to haze. Squared so most of the texture stays
    # blue and the pale band stays tight to the horizon, as it is in life.
    t = np.linspace(0.0, 1.0, height)[:, None]
    gradient = _smoothstep(np.clip(t, 0.0, 1.0)) ** 0.75
    zenith = np.array(ZENITH, dtype=float)
    haze = np.array(HAZE, dtype=float)
    rgb = zenith[None, None, :] + (haze - zenith)[None, None, :] * gradient[..., None]

    # Cells are wider than they are tall, so clouds come out as sheets seen at a
    # glancing angle rather than round blobs. That anisotropy is most of what
    # makes a generated sky read as sky.
    density = _fbm(rng, height, width, cx=6, cy=2, octaves=6)
    cover = _smoothstep(np.clip((density - 0.46) / 0.26, 0.0, 1.0))

    # Thin the cloud out at the zenith and clear the last strip above the
    # horizon, where in a real sky perspective compresses everything to haze.
    band = np.clip(t * 3.0, 0.0, 1.0) * np.clip((1.0 - t) * 5.0, 0.0, 1.0)
    alpha = (cover * band)[..., None]

    # A second, finer noise field lights the tops and shades the undersides.
    lit = _fbm(rng, height, width, cx=12, cy=6, octaves=4)
    lit = np.clip((lit - 0.3) * 1.8, 0.0, 1.0)[..., None]
    cloud = np.array(_CLOUD_SHADE, float) + (
        np.array(_CLOUD_LIT, float) - np.array(_CLOUD_SHADE, float)
    ) * lit

    rgb = rgb * (1.0 - alpha) + cloud * alpha

    # A little noise breaks up the gradient. Without it the smooth ramp banks
    # into visible bands once the engine's own light maths has been applied.
    rgb += rng.normal(0.0, 1.2, rgb.shape)

    return _encode(rgb)


#: A flat is 64 map units square — exactly 2m of ground at 32 units/m. Rendering
#: it at 128px therefore gives 64 pixels per metre, which is more than enough for
#: grass and about right for the mortar course in a paving slab.
FLAT_SIZE = 128
FLAT_UNITS_M = 2.0


@dataclass(frozen=True)
class Ground:
    """Recipe for one ground flat.

    Real ground varies at two scales at once — broad patches of colour metres
    across, and fine grain at the centimetre level. Using only the fine scale
    gives the flat, even mush that Freedoom's GRASS1 has and that made the first
    screenshots read as a billiard table.
    """

    name: str
    rgb: tuple[int, int, int]
    #: Coarse mottling: how far patches drift from the base colour, 0-1.
    blotch: float = 0.10
    #: Fine grain amplitude, 0-1.
    grain: float = 0.06
    #: Lattice cells across the tile for the coarse layer. Higher = busier.
    cells: int = 4
    #: Optional directional banding: (period_px, strength). Furrows in a ploughed
    #: field, mower stripes on a pitch, the joint lines in paving.
    stripes: tuple[int, float] | None = None
    #: Draw a slab grid at this pixel pitch — paving and concrete only.
    slabs: int | None = None


#: Ground cover we can generate. Names stay within 8 characters: long lump names
#: are legal in a PK3 and in UDMF, but staying short keeps them greppable in a
#: TEXTMAP and safe if any of this is ever emitted as a plain WAD.
GROUNDS = (
    Ground("DMGRASS", (86, 112, 58), blotch=0.13, grain=0.07, cells=4),
    # Mown and watered: darker, richer and far more even than a rough field.
    # This is what separates a village from the farmland around it — 20 of the
    # 37 parcels on the Birchington tile are landuse=residential.
    Ground("DMGARDEN", (62, 96, 48), blotch=0.07, grain=0.05, cells=5),
    Ground("DMMEADOW", (120, 134, 66), blotch=0.16, grain=0.09, cells=3),
    Ground("DMPITCH", (74, 116, 52), blotch=0.06, grain=0.04, stripes=(32, 0.07)),
    Ground("DMWOOD", (48, 66, 38), blotch=0.22, grain=0.10, cells=6),
    Ground("DMSCRUB", (92, 100, 62), blotch=0.20, grain=0.11, cells=5),
    Ground("DMFARM", (108, 84, 58), blotch=0.14, grain=0.08, stripes=(16, 0.10)),
    Ground("DMSAND", (198, 180, 138), blotch=0.09, grain=0.06, cells=3),
    Ground("DMTARMAC", (62, 62, 66), blotch=0.07, grain=0.06, cells=3),
    Ground("DMPAVE", (146, 143, 136), blotch=0.05, grain=0.04, slabs=64),
    Ground("DMCONC", (158, 156, 150), blotch=0.06, grain=0.04, cells=3),
    Ground("DMGRAVEL", (132, 124, 110), blotch=0.10, grain=0.16, cells=8),
    Ground("DMWATER", (58, 92, 112), blotch=0.10, grain=0.03, cells=3),
)

GROUND_BY_NAME = {ground.name: ground for ground in GROUNDS}


def ground_png(ground: Ground, seed: int = 1, size: int = FLAT_SIZE) -> bytes:
    """One tileable ground flat."""
    rng = np.random.default_rng(seed)

    coarse = _fbm(rng, size, size, ground.cells, ground.cells, octaves=3, wrap_y=True)
    fine = _fbm(rng, size, size, size // 8, size // 8, octaves=2, wrap_y=True)

    shade = 1.0 + (coarse - 0.5) * 2.0 * ground.blotch + (fine - 0.5) * 2.0 * ground.grain

    if ground.stripes:
        period, strength = ground.stripes
        rows = np.arange(size)[:, None]
        # Full cycles across the tile only, or the stripe steps at the seam.
        cycles = max(1, round(size / period))
        shade = shade * (1.0 + strength * np.sin(2.0 * np.pi * cycles * rows / size))

    rgb = np.array(ground.rgb, dtype=float)[None, None, :] * shade[..., None]

    if ground.slabs:
        # Joints darken; the slab faces themselves vary slightly in tone so the
        # grid does not read as a single stamped sheet.
        xs = np.arange(size)[None, :]
        ys = np.arange(size)[:, None]
        joint = ((xs % ground.slabs) < 2) | ((ys % ground.slabs) < 2)
        tone = rng.uniform(0.94, 1.06, (size // ground.slabs + 1, size // ground.slabs + 1))
        tone = np.repeat(np.repeat(tone, ground.slabs, 0), ground.slabs, 1)[:size, :size]
        rgb = rgb * tone[..., None]
        rgb = np.where(joint[..., None], rgb * 0.78, rgb)

    return _encode(rgb)


def _encode(rgb: np.ndarray) -> bytes:
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def pk3_assets(seed: int = 1) -> dict[str, bytes]:
    """Every generated file, keyed by its path inside the PK3.

    GZDoom registers by folder: textures/ becomes wall textures, flats/ becomes
    flats. Each asset gets its own derived seed so adding one does not reshuffle
    the others and invalidate a golden-file comparison.
    """
    assets = {f"textures/{SKY}.png": sky_png(seed)}
    for index, ground in enumerate(GROUNDS):
        assets[f"flats/{ground.name}.png"] = ground_png(ground, seed + 101 * (index + 1))
    return assets
