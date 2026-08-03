"""Top-down PNG preview of a generated map.

Loading the map in GZDoom tells you it works; it doesn't tell you whether the
place is *right*. A plan view next to a real map answers that in one look, and
it catches whole classes of bug — footprints in the wrong place, roads not
joining, a tile offset by one — far faster than walking around in-engine.

Sectors are shaded by floor height, so terrain reads as contours.
"""

from __future__ import annotations

from pathlib import Path

from .geometry import MapGeometry

BACKGROUND = (18, 20, 24)
BUILDING = (232, 158, 92)
OUTLINE = (12, 12, 14)
PLAYER = (255, 64, 96)


def _shade(fraction: float) -> tuple[int, int, int]:
    """Low ground dark green, high ground pale — a crude hypsometric ramp."""
    fraction = max(0.0, min(1.0, fraction))
    r = int(46 + fraction * 150)
    g = int(74 + fraction * 130)
    b = int(46 + fraction * 110)
    return r, g, b


#: Minimap palette. Colour comes from the floor *texture*, not the floor height:
#: on a plan view the thing you navigate by is the road network, and a
#: hypsometric ramp buries it in the terrain it happens to sit level with.
MINIMAP_BACKGROUND = (26, 34, 28)
MINIMAP_COLOURS = {
    "DMTARMAC": (150, 152, 158),
    "DMPAVE": (120, 122, 128),
    "DMCONC": (120, 122, 128),
    "DMGRAVEL": (112, 106, 96),
    "DMWATER": (58, 92, 132),
    "DMFARM": (86, 68, 48),
    "DMSAND": (150, 138, 106),
    "DMWOOD": (34, 52, 32),
    "DMSCRUB": (58, 64, 42),
    "DMMEADOW": (72, 84, 44),
    "DMGARDEN": (44, 68, 36),
}
MINIMAP_BUILDING = (228, 196, 140)


def render_minimap(
    geo: MapGeometry,
    tile_units: int,
    size_px: int = 256,
    roof_texture: str = "CEIL5_2",
) -> bytes:
    """A north-up plan of the tile, as PNG bytes for the HUD.

    Deliberately *baked* rather than drawn at runtime. Reconstructing this from
    map geometry in ZScript would mean walking 14,000 linedefs every frame, and
    we already know the plan at generation time.

    Square, not round. A disc looks better but the tile *is* a square, so
    masking it to a circle throws away the corners — and a player standing in
    one then has no marker on the map at all, which is exactly when you most
    want to know where you are.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size_px, size_px), MINIMAP_BACKGROUND)
    draw = ImageDraw.Draw(image)
    scale = size_px / float(tile_units)

    def project(x: float, y: float) -> tuple[float, float]:
        # Doom's +y is north; image +y is down.
        return x * scale, size_px - y * scale

    for face, sector in zip(geo.faces, geo.sectors, strict=False):
        floor_tex = sector.get("texturefloor", "")
        colour = (
            MINIMAP_BUILDING
            if floor_tex == roof_texture
            else MINIMAP_COLOURS.get(floor_tex, MINIMAP_COLOURS["DMGARDEN"])
        )
        try:
            ring = [project(x, y) for x, y in face.exterior.coords]
        except AttributeError:
            continue
        if len(ring) >= 3:
            draw.polygon(ring, fill=colour)

    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render(
    geo: MapGeometry,
    path: str | Path,
    size_px: int = 1400,
    tile_units: int | None = None,
    building_floor_threshold: int = 0,
) -> Path:
    """Draw the arrangement to a PNG. Requires Pillow (an extra, not a core dep)."""
    from PIL import Image, ImageDraw

    if not geo.faces:
        raise ValueError("geometry has no faces to preview")

    xs = [v[0] for v in geo.vertices]
    ys = [v[1] for v in geo.vertices]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if tile_units:
        min_x, min_y = 0, 0
        max_x = max_y = tile_units

    span = max(max_x - min_x, max_y - min_y) or 1
    scale = (size_px - 8) / span

    def project(x: float, y: float) -> tuple[float, float]:
        # Doom's +y is north; image +y is down, so flip.
        return 4 + (x - min_x) * scale, size_px - 4 - (y - min_y) * scale

    floors = [s["heightfloor"] for s in geo.sectors]
    ground = sorted(floors)
    low = ground[0]
    # Ignore the top decile so a few tall buildings don't flatten the ramp.
    high = ground[max(0, int(len(ground) * 0.9) - 1)]
    span_h = (high - low) or 1

    image = Image.new("RGB", (size_px, size_px), BACKGROUND)
    draw = ImageDraw.Draw(image)

    for face, sector in zip(geo.faces, geo.sectors, strict=False):
        height = sector["heightfloor"]
        colour = (
            BUILDING
            if height > high + building_floor_threshold
            else _shade((height - low) / span_h)
        )
        try:
            ring = [project(x, y) for x, y in face.exterior.coords]
        except AttributeError:
            continue
        if len(ring) >= 3:
            draw.polygon(ring, fill=colour)
        for interior in face.interiors:
            hole = [project(x, y) for x, y in interior.coords]
            if len(hole) >= 3:
                draw.polygon(hole, fill=BACKGROUND)

    for line in geo.linedefs:
        a = geo.vertices[line["v1"]]
        b = geo.vertices[line["v2"]]
        width = 2 if not line.get("twosided") else 1
        draw.line([project(*a), project(*b)], fill=OUTLINE, width=width)

    for thing in geo.things:
        x, y = project(thing.x, thing.y)
        draw.ellipse([x - 7, y - 7, x + 7, y + 7], fill=PLAYER, outline=(255, 255, 255), width=2)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path
