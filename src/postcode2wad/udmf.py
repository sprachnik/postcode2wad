"""Emit a UDMF TEXTMAP lump.

UDMF is plain text, which is why the brief picked it: easy to emit, easy to
diff, easy to eyeball when something is wrong. The grammar is a flat list of
typed blocks, each a bag of `key = value;` assignments.

    namespace = "zdoom";

    vertex { x = 0.000; y = 0.000; }
    linedef { v1 = 0; v2 = 1; sidefront = 0; blocking = true; }
    sidedef { sector = 0; texturemiddle = "BRICK7"; }
    sector { heightfloor = 0; heightceiling = 4096; texturefloor = "FLAT1"; }
    thing { x = 64.000; y = 64.000; type = 1; }

Indices are zero-based and refer to declaration order within each block type.
Booleans are only written when true — UDMF defaults every unlisted flag false.
"""

from __future__ import annotations

from .geometry import MapGeometry, Thing

NAMESPACE = "zdoom"


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _block(kind: str, fields: dict) -> str:
    body = " ".join(
        f"{k} = {_value(v)};" for k, v in fields.items() if v is not None and v is not False
    )
    return f"{kind} {{ {body} }}"


def _thing_fields(thing: Thing) -> dict:
    return {
        "x": float(thing.x),
        "y": float(thing.y),
        "angle": thing.angle,
        "type": thing.type,
        "scalex": float(thing.scale) if thing.scale != 1.0 else None,
        "scaley": float(thing.scale) if thing.scale != 1.0 else None,
        "skill1": True,
        "skill2": True,
        "skill3": True,
        "skill4": True,
        "skill5": True,
        "single": True,
        "dm": True,
        "coop": True,
    }


def emit_textmap(geo: MapGeometry, comment: str | None = None) -> str:
    lines = [f'namespace = "{NAMESPACE}";', ""]
    if comment:
        lines[1:1] = [f"// {line}" for line in comment.splitlines()]

    # Per-vertex floor heights. Terrain slope now comes from per-sector plane
    # equations (see geometry._floor_plane) precisely because zfloor is shared:
    # GZDoom applies it to any three-sided sector touching the vertex, which
    # dragged building roofs down to terrain height. The plumbing stays because
    # it is the right tool for anything that *wants* shared heights, but nothing
    # populates vertex_floor today.
    for index, (x, y) in enumerate(geo.vertices):
        fields = {"x": float(x), "y": float(y)}
        height = geo.vertex_floor.get(index)
        if height is not None:
            fields["zfloor"] = float(height)
        lines.append(_block("vertex", fields))
    lines.append("")

    for line in geo.linedefs:
        lines.append(_block("linedef", line))
    lines.append("")

    for side in geo.sidedefs:
        lines.append(_block("sidedef", side))
    lines.append("")

    for sector in geo.sectors:
        lines.append(_block("sector", sector))
    lines.append("")

    for thing in geo.things:
        lines.append(_block("thing", _thing_fields(thing)))

    return "\n".join(lines) + "\n"
