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


def _value_noise(rng: np.random.Generator, height: int, width: int, cx: int, cy: int) -> np.ndarray:
    """Bilinear value noise on a lattice that wraps horizontally.

    The wrap matters: a Doom sky texture is tiled around the full 360 degrees of
    yaw, so any seam in x is a permanent vertical scar in the sky that the player
    can turn around and find. Duplicating the first lattice column onto the end
    makes the interpolation continuous across the join.
    """
    lattice = rng.random((cy + 1, cx))
    lattice = np.concatenate([lattice, lattice[:, :1]], axis=1)

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
) -> np.ndarray:
    """Fractional Brownian motion — octaves of value noise at halving amplitude."""
    total = np.zeros((height, width))
    amplitude, norm = 1.0, 0.0
    for octave in range(octaves):
        total += amplitude * _value_noise(rng, height, width, cx << octave, cy << octave)
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


def _encode(rgb: np.ndarray) -> bytes:
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def pk3_assets(seed: int = 1) -> dict[str, bytes]:
    """Every generated file, keyed by its path inside the PK3."""
    return {f"textures/{SKY}.png": sky_png(seed)}
