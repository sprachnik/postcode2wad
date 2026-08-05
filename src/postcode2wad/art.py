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
    # Chalk. Kent is chalk downland that ends in a cliff, and every contour
    # riser was being painted DMGRASS — so a 10m sea cliff rendered as a grass
    # wall. Near-white with very little blotch, because a fresh chalk face is
    # the most uniform surface in the county; the grain carries the flint
    # banding without turning it grey.
    Ground("DMCHALK", (222, 220, 210), blotch=0.05, grain=0.07, cells=4),
    # Marsh. North Kent and Romney are marsh, and natural=wetland was falling
    # through to bare terrain. Between meadow and water: wet, dark, unmown.
    Ground("DMMARSH", (86, 100, 70), blotch=0.20, grain=0.10, cells=4),
    # Ballast. Coarse grey stone with the sleepers reading across it — the
    # stripes are at 64px, which at 32 px/m is a sleeper every 2m. Birchington
    # had a station and no track before this existed.
    Ground("DMRAIL", (104, 100, 96), blotch=0.13, grain=0.20, cells=9,
           stripes=(64, 0.14)),
    # The rail head: worn steel, near-uniform because it is polished by traffic.
    Ground("DMRAILH", (168, 170, 176), blotch=0.04, grain=0.05, cells=2),
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


#: Barrier textures are 128x64, mapped 1:1, so they cover 4m across by 2m up at
#: 32 pixels per metre. Doom draws a lower texture downward from the top of the
#: riser, which for a barrier means the top of the texture lands on the top of
#: the hedge or fence — so detail belongs at the top of the image, and the
#: bottom is what gets cropped on a short one.
BARRIER_WIDTH = 128
BARRIER_HEIGHT = 64

HEDGE = "DMHEDGE"
FENCE = "DMFENCE"


def hedge_png(seed: int = 1) -> bytes:
    """Clipped privet: dense, dark, and lit from above."""
    rng = np.random.default_rng(seed)
    w, h = BARRIER_WIDTH, BARRIER_HEIGHT

    leaf = _fbm(rng, h, w, cx=8, cy=4, octaves=4, wrap_y=True)
    clump = _fbm(rng, h, w, cx=3, cy=2, octaves=2, wrap_y=True)

    # Sunlight falls on the top of a hedge and barely reaches the bottom; that
    # vertical ramp does more for the shape than any amount of leaf detail.
    ramp = np.linspace(1.18, 0.62, h)[:, None]
    shade = ramp * (0.80 + 0.40 * leaf) * (0.88 + 0.24 * clump)

    base = np.array((54, 78, 42), dtype=float)
    rgb = base[None, None, :] * shade[..., None]
    # A few pale leaves catching the light, so it is not a flat green slab.
    highlight = (leaf > 0.74)[..., None]
    rgb = np.where(highlight, rgb * 1.35, rgb)
    return _encode(rgb)


def fence_png(seed: int = 1) -> bytes:
    """Closeboard timber: vertical boards, each a slightly different tone."""
    rng = np.random.default_rng(seed)
    w, h = BARRIER_WIDTH, BARRIER_HEIGHT

    board_px = 5  # ~150mm at 32 px/m
    xs = np.arange(w)
    board_id = xs // board_px
    tone = rng.uniform(0.82, 1.12, board_id.max() + 1)[board_id][None, :]

    # Wood grain runs along the board, so the noise is stretched vertically.
    grain = _fbm(rng, h, w, cx=w // 4, cy=1, octaves=3, wrap_y=True)
    shade = tone * (0.88 + 0.24 * grain)

    # The dark gap between boards is what makes it read as boards at all.
    shade = np.where((xs % board_px == 0)[None, :], shade * 0.55, shade)

    base = np.array((132, 104, 72), dtype=float)
    rgb = base[None, None, :] * shade[..., None]

    # Capping rail across the top two rows — the top of the texture is the top
    # of the fence, so this lands where it should on any fence height.
    rgb[:2, :, :] = base[None, None, :] * 0.72
    return _encode(rgb)


#: Façade geometry, all of it derived from real dimensions.
#:
#: A texture covers one 8m width of frontage and the *whole* height of the wall,
#: which is the trick that makes this work. Doom draws a lower texture downward
#: from the top of the riser, so if the texture is exactly as tall as the wall
#: then the top row lands on the eaves and the bottom row on the pavement — and
#: every window in between sits at the height it should. `build.py` sets
#: scaley to make that true for each building's measured height.
#:
#: 8m rather than 4m so that one door appears per frontage instead of one every
#: four metres, which reads as a row of front doors on a detached house.
FACADE_WIDTH = 256  # px, = 8m at 32 px/m
STOREY_PX = 128  # px per storey; a storey is 3.1m, so ~41 px/m vertically
BAYS = 4  # 2m per bay
MAX_STOREYS = 4

STOREY_M = 3.1


@dataclass(frozen=True)
class Facade:
    """A wall material. `courses` is the brick/block size in metres, or None
    for render and pebbledash, which have no coursing at all."""

    key: str
    brick: tuple[int, int, int]
    mortar: tuple[int, int, int]
    trim: tuple[int, int, int]
    courses: tuple[float, float] | None = (0.215, 0.075)
    #: Per-unit tone scatter. Stock brick is far more varied than machine brick.
    scatter: float = 0.10
    #: What gets *drawn* on the wall, which turns out to matter more than its
    #: colour. Every façade used to be domestic: sash windows in bays and a
    #: front door one bay in from the left, on grey blockwork as readily as on
    #: brick. A distribution shed and a 90,000 m² glasshouse both came out
    #: looking like a terrace that had been painted grey.
    #:
    #:   domestic    sash windows per bay, a front door, eaves
    #:   industrial  continuous glazing band high on the wall, roller shutter
    #:   glass       mostly glazing with structural mullions
    style: str = "domestic"


FACADES = (
    Facade("R", (138, 88, 74), (186, 180, 170), (238, 238, 234), scatter=0.11),
    Facade("Y", (178, 160, 124), (198, 192, 180), (238, 238, 234), scatter=0.10),
    Facade("N", (220, 216, 206), (220, 216, 206), (96, 96, 100), courses=None, scatter=0.03),
    Facade("S", (156, 150, 136), (176, 172, 160), (86, 84, 80), courses=(0.45, 0.22), scatter=0.10),
    # Profiled steel cladding, not blockwork: wide flat courses, almost no tone
    # scatter, because a clad shed is machine-made and uniform in a way brick
    # never is.
    Facade(
        "G", (138, 138, 136), (168, 168, 164), (78, 78, 80),
        courses=(1.0, 0.02), scatter=0.03, style="industrial",
    ),
    # Glasshouse. Thanet Earth is ~90 hectares of this and is one of the largest
    # structures in Kent; rendering it as a grey terrace with front doors was
    # the report that prompted all of this.
    Facade(
        "H", (168, 194, 200), (196, 214, 218), (150, 160, 164),
        courses=None, scatter=0.02, style="glass",
    ),
    # Curtain wall, for anything too tall to be a house.
    #
    # Every other façade is drawn once and *stretched* to the wall, which works
    # because MAX_STOREYS is 4 and a four-storey texture over a four-storey
    # building is one-to-one. Over a tower it is grotesque: the same texture
    # across 310m of the Shard makes each window 77m tall. This one is drawn to
    # tile seamlessly instead, one texture-storey per real storey, so a 40-floor
    # building gets 40 floors rather than four enormous ones. See the scaley in
    # `building_facade`.
    Facade(
        "T", (64, 78, 92), (92, 108, 124), (150, 164, 178),
        courses=None, scatter=0.02, style="tower",
    ),
)
FACADE_BY_KEY = {facade.key: facade for facade in FACADES}

_GLASS = (74, 92, 104)
_GLASS_SKY = (150, 178, 200)


def facade_name(key: str, storeys: int) -> str:
    return f"DMW{key}{storeys}"


def facade_png(facade: Facade, storeys: int, seed: int = 1) -> bytes:
    """One whole-wall façade texture: `storeys` storeys, 8m wide."""
    rng = np.random.default_rng(seed)
    w = FACADE_WIDTH
    h = STOREY_PX * storeys
    px_per_m_x = w / 8.0
    px_per_m_y = STOREY_PX / STOREY_M

    rgb = _masonry(rng, facade, h, w, px_per_m_x, px_per_m_y)

    if facade.style == "tower":
        _curtain_wall(rgb, facade, h, w, storeys, px_per_m_x, px_per_m_y)
        return _encode(rgb)
    if facade.style == "glass":
        _glasshouse(rgb, facade, h, w, px_per_m_x, px_per_m_y)
        return _encode(rgb)
    if facade.style == "industrial":
        _industrial(rgb, facade, rng, h, w, storeys, px_per_m_x, px_per_m_y)
        return _encode(rgb)

    bay = w // BAYS
    for storey in range(storeys):
        # Storey 0 is the top of the texture, which is the top of the building.
        top = storey * STOREY_PX
        ground = storey == storeys - 1
        for index in range(BAYS):
            left = index * bay
            # One bay of the ground floor is the front door.
            if ground and index == 1:
                _door(rgb, facade, top, left, bay, px_per_m_x, px_per_m_y)
            else:
                _window(rgb, facade, rng, top, left, bay, px_per_m_x, px_per_m_y)

    _eaves(rgb, facade, px_per_m_y)
    return _encode(rgb)


def _industrial(rgb, facade: Facade, rng, h: int, w: int, storeys: int,
                px_x: float, px_y: float) -> None:
    """A clad shed: one continuous glazing band high up, and a roller shutter.

    Sheds are not storeyed the way a house is — the wall is one volume with a
    strip of daylight near the eaves and a vehicle door at the bottom. Drawing
    them per-storey with sash windows is what made a distribution warehouse read
    as a very large terrace.
    """
    band_top = int(0.18 * h)
    band_bottom = band_top + max(2, int(1.1 * px_y))
    rgb[band_top:band_bottom, :] = _GLASS
    # Mullions at ~3m, which is what holds a glazing band up.
    for x in range(0, w, max(4, int(3.0 * px_x))):
        rgb[band_top:band_bottom, x:x + max(1, int(0.12 * px_x))] = facade.trim
    # A lighter run along the top of the band, so it reads as glass catching the
    # sky rather than as a painted stripe.
    rgb[band_top:band_top + max(1, int(0.2 * px_y))] = _GLASS_SKY

    # Roller shutter, ground level, roughly 4m wide and 4.5m tall.
    door_w = max(6, int(4.0 * px_x))
    door_h = max(8, int(4.5 * px_y))
    left = (w - door_w) // 2
    top = h - door_h
    rgb[top:h, left:left + door_w] = facade.trim
    # Horizontal slats.
    for y in range(top, h, max(2, int(0.25 * px_y))):
        rgb[y:y + 1, left:left + door_w] = facade.mortar


def _curtain_wall(rgb, facade: Facade, h: int, w: int, storeys: int,
                  px_x: float, px_y: float) -> None:
    """Glazing and spandrel, repeating, with no top or bottom.

    Every storey is identical on purpose: this texture tiles up the wall rather
    than being stretched to fit it, so it must have no eaves, no ground floor
    and no door — anything that belongs at one end would appear on every floor.
    """
    band = max(2, STOREY_PX // 6)  # the opaque spandrel between floors
    for storey in range(storeys):
        top = storey * STOREY_PX
        rgb[top:top + STOREY_PX] = _GLASS
        # Glazing is lighter towards the top of each floor, where it takes sky.
        rgb[top:top + STOREY_PX // 3] = _GLASS_SKY
        rgb[top:top + band] = facade.brick
    # Mullions every 1.5m, which is a normal curtain-wall module.
    for x in range(0, w, max(3, int(1.5 * px_x))):
        rgb[:, x:x + max(1, int(0.1 * px_x))] = facade.trim


def _glasshouse(rgb, facade: Facade, h: int, w: int, px_x: float, px_y: float) -> None:
    """Mostly glass on a light frame — a commercial greenhouse.

    No storeys, no doors: from outside, a glasshouse is a grid of panes on
    mullions, and its whole character is that you can see the sky through it.
    """
    rgb[:, :] = _GLASS_SKY
    pane_x = max(3, int(1.2 * px_x))
    pane_y = max(3, int(1.2 * px_y))
    frame = max(1, int(0.08 * px_x))
    for x in range(0, w, pane_x):
        rgb[:, x:x + frame] = facade.trim
    for y in range(0, h, pane_y):
        rgb[y:y + frame, :] = facade.trim
    # A darker band at the foot: the dwarf wall a glasshouse actually sits on.
    foot = max(2, int(0.8 * px_y))
    rgb[h - foot:h, :] = facade.brick


def _masonry(rng, facade: Facade, h: int, w: int, px_x: float, px_y: float) -> np.ndarray:
    """Brickwork, or flat render where the material has no coursing."""
    base = np.array(facade.brick, dtype=float)
    if facade.courses is None:
        # Render still needs grain, or it reads as a solid colour swatch.
        grain = _fbm(rng, h, w, cx=8, cy=8, octaves=3)
        return base[None, None, :] * (0.94 + 0.12 * grain)[..., None]

    length_m, height_m = facade.courses
    unit_w = max(2, round(length_m * px_x))
    unit_h = max(2, round(height_m * px_y))

    rows = np.arange(h)[:, None]
    cols = np.arange(w)[None, :]
    course = rows // unit_h
    # Stretcher bond: every other course offset by half a brick.
    shifted = cols + (course % 2) * (unit_w // 2)
    unit = shifted // unit_w

    tone = rng.normal(1.0, facade.scatter, (h // unit_h + 2, w // unit_w + 2))
    tone = np.clip(tone, 0.72, 1.28)
    rgb = base[None, None, :] * tone[course % tone.shape[0], unit % tone.shape[1]][..., None]

    # A joint is the thinnest thing we can draw — one pixel — but at ~3px per
    # course that is still a third of the wall, where real brickwork is nearer a
    # tenth. Blending rather than replacing keeps the coursing legible without
    # turning the elevation into a bright grid.
    joint = (((rows % unit_h) == 0) | ((shifted % unit_w) == 0))[..., None]
    mortar = np.array(facade.mortar, float)[None, None, :]
    return np.where(joint, rgb * 0.45 + mortar * 0.55, rgb)


def _window(rgb, facade: Facade, rng, top: int, left: int, bay: int, px_x: float, px_y: float):
    """A sash window with frame, sill and a hint of sky in the glass."""
    width = round(1.15 * px_x)
    height = round(1.35 * px_y)
    x0 = left + (bay - width) // 2
    y0 = top + round(0.60 * px_y)
    x1, y1 = x0 + width, y0 + height
    if y1 >= rgb.shape[0] or x1 >= rgb.shape[1]:
        return

    frame = np.array(facade.trim, dtype=float)
    rgb[y0:y1, x0:x1] = frame

    inset = max(2, round(0.06 * px_x))
    gx0, gy0, gx1, gy1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    if gx1 <= gx0 or gy1 <= gy0:
        return

    # Glass is a mirror: pale sky at the top darkening down into the room.
    fade = np.linspace(0.0, 1.0, gy1 - gy0)[:, None, None]
    glass = np.array(_GLASS_SKY, float) * (1 - fade) + np.array(_GLASS, float) * fade
    rgb[gy0:gy1, gx0:gx1] = glass * rng.uniform(0.9, 1.1)

    # Glazing bars: one vertical mullion, one horizontal at the sash meeting rail.
    mid_x = (gx0 + gx1) // 2
    mid_y = (gy0 + gy1) // 2
    rgb[gy0:gy1, mid_x : mid_x + max(1, inset // 2)] = frame
    rgb[mid_y : mid_y + max(1, inset // 2), gx0:gx1] = frame

    # Sill, projecting a little past the reveal on each side.
    sill = max(2, round(0.09 * px_y))
    overhang = max(1, round(0.08 * px_x))
    sy1 = min(rgb.shape[0], y1 + sill)
    rgb[y1:sy1, max(0, x0 - overhang) : min(rgb.shape[1], x1 + overhang)] = frame * 0.86


def _door(rgb, facade: Facade, top: int, left: int, bay: int, px_x: float, px_y: float):
    width = round(0.92 * px_x)
    height = round(2.02 * px_y)
    x0 = left + (bay - width) // 2
    y1 = top + STOREY_PX
    y0 = y1 - height
    x1 = x0 + width
    if y0 < 0 or y1 > rgb.shape[0] or x1 >= rgb.shape[1]:
        return

    frame = np.array(facade.trim, dtype=float)
    rgb[y0:y1, x0:x1] = frame

    inset = max(2, round(0.07 * px_x))
    dx0, dy0, dx1 = x0 + inset, y0 + inset, x1 - inset
    if dx1 <= dx0 or y1 - inset <= dy0:
        return
    # Painted door, darker than the surround so it reads as an opening.
    rgb[dy0 : y1 - 1, dx0:dx1] = np.array((58, 66, 78), dtype=float)
    # Fanlight over the top.
    light = max(2, round(0.28 * px_y))
    rgb[dy0 : dy0 + light, dx0:dx1] = np.array(_GLASS_SKY, dtype=float) * 0.9


def _eaves(rgb, facade: Facade, px_y: float) -> None:
    """Fascia and soffit. Cheap, and it stops the wall ending in mid-air."""
    depth = max(2, round(0.30 * px_y))
    rgb[:depth, :, :] = np.array(facade.trim, dtype=float) * 0.92
    rgb[depth : depth + 1, :, :] = np.array(facade.trim, dtype=float) * 0.60


#: Doom sprite naming: four characters of name, then a frame letter and a
#: rotation digit. 0 means "same picture from every angle", which is what a
#: billboard tree wants.
TREE_SPRITE = "DMTRA0"
TREE_PX = 256  #: 8m tall at 32 units/m; build.py scales each tree from there.
TREE_HEIGHT_M = 8.0


def tree_png(seed: int = 1, size: int = TREE_PX) -> bytes:
    """A broadleaf billboard, RGBA with a cut-out canopy."""
    rng = np.random.default_rng(seed)
    rgb = np.zeros((size, size, 3), dtype=float)
    alpha = np.zeros((size, size), dtype=float)

    ys = np.arange(size)[:, None]
    xs = np.arange(size)[None, :]

    # Trunk: tapering, and only in the bottom third.
    trunk_top = int(size * 0.62)
    half = np.zeros((size, 1))
    half[trunk_top:, 0] = np.linspace(size * 0.045, size * 0.028, size - trunk_top)
    trunk = np.abs(xs - size / 2) < half
    rgb[trunk] = (74, 58, 44)
    alpha[trunk] = 1.0

    # Canopy: a squashed radial falloff, chewed at the edges by noise so the
    # outline is ragged. A clean ellipse reads as a lollipop from any distance.
    cx, cy = size / 2, size * 0.36
    rx, ry = size * 0.44, size * 0.34
    radial = np.sqrt(((xs - cx) / rx) ** 2 + ((ys - cy) / ry) ** 2)
    noise = _fbm(rng, size, size, cx=5, cy=5, octaves=4)
    canopy = radial + (noise - 0.5) * 0.55 < 1.0

    # Light from above-left, so the canopy has a top and a bottom.
    lit = np.clip(1.75 - radial * 0.42 - (ys / size) * 1.05 + (noise - 0.5) * 0.55, 0.32, 1.55)
    leaf = np.array((62, 92, 46), dtype=float)
    rgb[canopy] = (leaf[None, :] * lit[canopy][:, None])[:, :3]
    alpha[canopy] = 1.0

    # Punch a few gaps through so sky shows between the branches.
    gaps = (noise > 0.68) & (radial > 0.45) & canopy
    alpha[gaps] = 0.0

    rgba = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)]).astype(np.uint8)
    image = Image.fromarray(rgba, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    # Bottom-centre origin. Without this the tree is drawn underground.
    return _with_grab(buffer.getvalue(), size // 2, size)


def _with_grab(png: bytes, x_offset: int, y_offset: int) -> bytes:
    """Insert a PNG `grAb` chunk, which is how Doom engines carry sprite offsets.

    This is not optional. A Doom sprite hangs *downward* from its top offset: the
    engine puts the pixel row `TopOffset` above the actor's feet, so an offset of
    zero — which is what Pillow writes, since grAb is not a standard PNG chunk —
    draws the entire sprite below the floor. Setting it to the image height puts
    the whole sprite above the actor's feet, where a tree belongs, and half the
    width centres the trunk on the thing rather than hanging it off to one side.
    """
    import struct
    import zlib

    body = b"grAb" + struct.pack(">ii", x_offset, y_offset)
    chunk = struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body))

    # Straight after IHDR: 8-byte signature, then IHDR's own 4+4+13+4 bytes.
    split = 8 + 25
    return png[:split] + chunk + png[split:]


#: A tree is an ordinary Doom actor; DECORATE is enough and needs no ZScript.
#: The DoomEdNum is in the 20000s, well clear of anything Doom or Freedoom uses.
#:
#: Note the state lines carry **no semicolons**. Those are ZScript syntax; the
#: DECORATE parser reads a trailing `;` as an action-function parameter and dies
#: with "Invalid parameter ';'" before the map is even reached.
TREE_DOOMEDNUM = 20501
DECORATE = f"""\
// Generated by postcode2wad. One billboard actor; each thing carries its own
// scale, taken from the LIDAR canopy height at that point.
ACTOR PostcodeTree {TREE_DOOMEDNUM}
{{
    Radius 12
    Height 160
    +SOLID
    States
    {{
    Spawn:
        {TREE_SPRITE[:4]} A -1
        Stop
    }}
}}
"""


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
    assets = {
        f"textures/{SKY}.png": sky_png(seed),
        f"textures/{HEDGE}.png": hedge_png(seed + 11),
        f"textures/{FENCE}.png": fence_png(seed + 13),
        f"sprites/{TREE_SPRITE}.png": tree_png(seed + 17),
        "DECORATE": DECORATE.encode(),
    }
    for index, ground in enumerate(GROUNDS):
        assets[f"flats/{ground.name}.png"] = ground_png(ground, seed + 101 * (index + 1))
    for index, facade in enumerate(FACADES):
        for storeys in range(1, MAX_STOREYS + 1):
            name = facade_name(facade.key, storeys)
            assets[f"textures/{name}.png"] = facade_png(
                facade, storeys, seed + 1009 * (index + 1) + storeys
            )
    return assets
