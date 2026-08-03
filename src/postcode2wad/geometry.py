"""Turn a pile of overlapping polygons into valid Doom sector topology.

This is the heart of the generator. Doom geometry is a *planar partition*: every
linedef is shared by at most two sectors, and a sector is a closed loop of them.
Real-world polygons are nothing like that — buildings sit on top of terrain,
roads cross each other, footprints share walls.

The fix is to stop thinking in polygons and think in arrangements:

1. Take the boundary of every input polygon as a set of linestrings.
2. `unary_union` them. Shapely nodes the network — every crossing becomes a
   shared vertex, so no two segments cross except at endpoints.
3. `polygonize` the result into faces. Faces tile the plane with no overlaps,
   which is exactly the planar partition Doom wants.
4. Assign each face to whichever input polygon contains it. Later specs win, so
   a building laid over terrain takes the building's floor height.

Winding is the part that bites. `orient` gives every face a CCW exterior and CW
holes, so walking any ring the face interior is always on your LEFT. Doom's
front side is on the RIGHT of v1->v2. So a ring segment p->q is emitted as a
linedef v1=q, v2=p, and the face becomes its front sector.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

SKY_FLAT = "F_SKY1"

#: GZDoom's Line_Horizon. A one-sided line with this special renders as an
#: infinite horizon — its sector's floor below, sky above — instead of a wall.
#: Without it the tile perimeter is a 128m-tall wall boxing the player in.
LINE_HORIZON = 9

Coord = tuple[int, int]


@dataclass
class SectorSpec:
    """One input region. Heights are map units; polygons are map-unit coords."""

    polygon: Polygon
    floor: int
    ceiling: int
    floor_tex: str
    ceil_tex: str = SKY_FLAT
    wall_tex: str = "BRICK7"
    light: int = 192
    #: Texture repeats per default tiling. A Doom texture is mapped one pixel to
    #: one map unit, so a 128px brick texture spans 4m of wall — which makes each
    #: visible brick course roughly 40cm tall. Scaling up shrinks the texture and
    #: gets the masonry back to a believable size. >1 means "repeat more".
    wall_scale_x: float = 1.0
    wall_scale_y: float = 1.0
    #: Legitimately narrow, so exempt from sliver absorption. A fence really is
    #: 15cm wide; without this flag it would be merged into the ground.
    thin: bool = False
    #: Follow the ground continuously instead of sitting flat. Faces belonging
    #: to a sloped spec are cut into triangles and given per-vertex heights, so
    #: the surface ramps rather than stepping.
    sloped: bool = False


@dataclass
class Thing:
    x: int
    y: int
    type: int
    angle: int = 0
    #: Sprite scale. UDMF lets each thing carry its own, which is what allows a
    #: single tree actor to stand in for canopy from 3m to 25m tall.
    scale: float = 1.0


@dataclass
class MapGeometry:
    vertices: list[Coord] = field(default_factory=list)
    sectors: list[dict] = field(default_factory=list)
    sidedefs: list[dict] = field(default_factory=list)
    linedefs: list[dict] = field(default_factory=list)
    things: list[Thing] = field(default_factory=list)
    #: The arrangement face behind each sector, parallel to `sectors`. Not part
    #: of the UDMF output — kept for the top-down preview renderer.
    faces: list[Polygon] = field(default_factory=list)
    #: Per-vertex floor height, by vertex index. Only vertices belonging to a
    #: sloped triangle appear here; everything else is flat and takes its
    #: sector's `heightfloor`.
    vertex_floor: dict[int, float] = field(default_factory=dict)

    def stats(self) -> str:
        return (
            f"{len(self.vertices)} vertices, {len(self.linedefs)} linedefs, "
            f"{len(self.sidedefs)} sidedefs, {len(self.sectors)} sectors, "
            f"{len(self.things)} things"
        )


class _VertexTable:
    """Dedupes integer coordinates to vertex indices."""

    def __init__(self) -> None:
        self.coords: list[Coord] = []
        self._index: dict[Coord, int] = {}

    def add(self, x: float, y: float) -> int:
        key = (round(x), round(y))
        if key not in self._index:
            self._index[key] = len(self.coords)
            self.coords.append(key)
        return self._index[key]

    def index_of(self, key: Coord) -> int | None:
        return self._index.get(key)


def _rings(poly: Polygon) -> list[list[tuple[float, float]]]:
    poly = orient(poly, sign=1.0)  # CCW exterior, CW interiors
    out = [list(poly.exterior.coords)]
    out.extend(list(interior.coords) for interior in poly.interiors)
    return out


def build_geometry(
    specs: list[SectorSpec],
    things: list[Thing] | None = None,
    #: Discarding sliver faces is not free, which is why this defaults to zero.
    #: Every face dropped here leaves the edges it backed onto with a single
    #: user, and a one-sided line is a solid wall running the full height of its
    #: sector — 128m, here. On Birchington a threshold of 4.0 produced 372 such
    #: walls, the longest 11m, visible in game as pale vertical streaks standing
    #: in mid-air. Keeping every face removes all of them for about 4% more
    #: sectors; `_drop_empty_sectors` still clears anything that loses all its
    #: edges to integer snapping.
    min_face_area: float = 0.0,
    #: Faces narrower than this adopt a neighbour's height instead of standing
    #: at their own. 8 units is 25cm — below anything the generator means to
    #: build, and above the arrangement noise it is there to remove.
    min_sliver_width: float = 8.0,
    horizon_border: bool = True,
    #: Ground height in map units at a map-unit position. Required for sloped
    #: specs and ignored otherwise.
    height_at=None,
) -> MapGeometry:
    """Arrange overlapping SectorSpecs into Doom-legal sector topology."""
    if not specs:
        raise ValueError("need at least one SectorSpec")

    boundaries: list[LineString] = []
    for spec in specs:
        poly = spec.polygon
        boundaries.append(LineString(poly.exterior.coords))
        boundaries.extend(LineString(r.coords) for r in poly.interiors)

    noded = unary_union(boundaries)
    faces = [f for f in polygonize(noded) if f.area >= min_face_area]
    if not faces:
        raise ValueError("arrangement produced no faces — check input polygons")

    # The outer edge of the whole arrangement, used below to tell a genuine
    # horizon from a hole. Taken from the inputs rather than the surviving faces
    # so that dropping a sliver on the tile edge cannot shrink it.
    min_x, min_y, max_x, max_y = unary_union([s.polygon for s in specs]).bounds

    # Assign each face to the last spec containing it. Buildings are passed
    # after terrain, so they win the overlap.
    owned: list[tuple[Polygon, SectorSpec]] = []
    for face in faces:
        probe = face.representative_point()
        owner = None
        for spec in specs:
            if spec.polygon.contains(probe):
                owner = spec
        if owner is not None:
            owned.append((face, owner))

    if not owned:
        raise ValueError("no face fell inside any SectorSpec")

    owned = _triangulate_sloped(owned)

    # Collect directed ring segments. Face interior is always on the LEFT of p->q.
    edges: dict[tuple[Coord, Coord], list[tuple[int, Coord, Coord]]] = {}
    for face_idx, (face, _spec) in enumerate(owned):
        for ring in _rings(face):
            # Rings are closed, so ring[1:] is exactly the segment partner list.
            for (ax, ay), (bx, by) in itertools.pairwise(ring):
                a = (round(ax), round(ay))
                b = (round(bx), round(by))
                if a == b:
                    continue  # collapsed by integer snapping
                key = (a, b) if a <= b else (b, a)
                edges.setdefault(key, []).append((face_idx, a, b))

    face_spec = _absorb_slivers(owned, edges, min_sliver_width)

    geo = MapGeometry()
    verts = _VertexTable()

    # A sloped face takes its height from its vertices, so its sector's own
    # heightfloor is only a fallback -- but the riser logic below compares floor
    # heights to decide which side of a two-sided line is standing up, and for
    # sloped ground that has to be the real local height, not the spec's.
    face_floor: list[int] = []
    for index, (face, _owner) in enumerate(owned):
        spec = face_spec[index]
        if spec.sloped and height_at is not None:
            ring = list(face.exterior.coords)[:-1]
            heights = [height_at(x, y) for x, y in ring]
            face_floor.append(round(sum(heights) / len(heights)) if heights else spec.floor)
        else:
            face_floor.append(spec.floor)

    # One sector per face. Merging co-planar neighbours outright is a later
    # optimisation; adjacent faces sharing a spec already render as one surface
    # because the line between them has no height difference to draw.
    face_sector: list[int] = []
    for index, (face, _owner) in enumerate(owned):
        spec = face_spec[index]
        face_sector.append(len(geo.sectors))
        geo.faces.append(face)
        sector = {
            "heightfloor": face_floor[index],
            "heightceiling": spec.ceiling,
            "texturefloor": spec.floor_tex,
            "textureceiling": spec.ceil_tex,
            "lightlevel": spec.light,
        }
        if spec.sloped and height_at is not None:
            plane = _floor_plane(face, height_at)
            if plane is not None:
                a, b, c, d = plane
                sector["floorplane_a"] = a
                sector["floorplane_b"] = b
                sector["floorplane_c"] = c
                sector["floorplane_d"] = d
        geo.sectors.append(sector)

    for uses in edges.values():
        if len(uses) > 2:
            # Non-manifold: more than two faces claim this edge. Shapely's
            # arrangement should prevent it; keep the first two and move on.
            uses = uses[:2]

        front_face, p, q = uses[0]
        back_face = uses[1][0] if len(uses) == 2 else None
        if back_face == front_face:
            continue  # a face touching itself along an edge; skip the sliver

        front_spec = face_spec[front_face]
        # Face interior is left of p->q, so reverse to put it on the right.
        v1 = verts.add(*q)
        v2 = verts.add(*p)

        sidefront = len(geo.sidedefs)
        line: dict = {"v1": v1, "v2": v2, "sidefront": sidefront}

        if back_face is None:
            # A one-sided line is *usually* the tile perimeter, but not always:
            # `min_face_area` discards sliver faces, and every edge the discarded
            # face used to back onto is left with a single user. Those interior
            # orphans must be plain walls. Stamping Line_Horizon on one turns it
            # into a window onto an infinite flat plane — which is precisely what
            # it looks like in game, a tear in the world with a void behind it.
            geo.sidedefs.append(
                {
                    "sector": face_sector[front_face],
                    "texturemiddle": front_spec.wall_tex,
                    **_scale(front_spec, "mid"),
                }
            )
            line["blocking"] = True
            if horizon_border and _on_border(p, q, min_x, min_y, max_x, max_y):
                line["special"] = LINE_HORIZON
        else:
            back_spec = face_spec[back_face]
            # The lower texture is drawn on the side facing the LOWER sector,
            # but it is the *higher* sector that is standing up — so a building
            # next to grass must be painted with the building's texture, not the
            # grass's. Getting this backwards paints every wall in the terrain's
            # texture and the whole map looks like mud cliffs.
            riser = front_spec if face_floor[front_face] >= face_floor[back_face] else back_spec
            # Same logic inverted for uppers: the lower ceiling is the overhang.
            soffit = front_spec if front_spec.ceiling <= back_spec.ceiling else back_spec

            # Scale follows the spec that owns each texture, not the sidedef's
            # own sector — otherwise a building wall seen from the grass gets the
            # grass's scale and the brickwork stretches.
            faces_of_line = {
                "texturebottom": riser.wall_tex,
                "texturetop": soffit.wall_tex,
                **_scale(riser, "bottom"),
                **_scale(soffit, "top"),
            }
            geo.sidedefs.append({"sector": face_sector[front_face], **faces_of_line})
            geo.sidedefs.append({"sector": face_sector[back_face], **faces_of_line})
            line["sideback"] = sidefront + 1
            line["twosided"] = True

        geo.linedefs.append(line)

    geo.vertices = verts.coords
    geo.things = list(things or [])
    _drop_empty_sectors(geo)
    return geo


def _triangulate_sloped(
    owned: list[tuple[Polygon, SectorSpec]],
) -> list[tuple[Polygon, SectorSpec]]:
    """Cut every sloped face into triangles.

    Not for GZDoom's sake — a sector plane works on any polygon — but for
    *continuity*. A plane fitted to a face with four or more corners cannot pass
    through the ground height at all of them, so two neighbours each fit their
    own compromise and disagree along the edge they share. Measured on a real
    tile that put 1.8% of shared edges over Doom's 24-unit climb limit: invisible
    walls, a couple of metres tall, scattered across open ground. A triangle's
    three corners define a plane exactly, and neighbours share two of them, so
    triangles are the largest face that can be guaranteed to line up.

    Constrained Delaunay because it neither moves nor adds boundary vertices,
    so the mesh still meets the flat faces around it exactly.
    """
    import shapely

    out: list[tuple[Polygon, SectorSpec]] = []
    for face, spec in owned:
        if not spec.sloped or len(face.exterior.coords) <= 4:
            out.append((face, spec))
            continue
        try:
            pieces = shapely.constrained_delaunay_triangles(face)
        except shapely.errors.GEOSException:
            out.append((face, spec))
            continue
        triangles = [
            g for g in getattr(pieces, "geoms", []) if isinstance(g, Polygon) and g.area > 0
        ]
        out.extend((t, spec) for t in triangles) if triangles else out.append((face, spec))
    return out


def _floor_plane(face: Polygon, height_at) -> tuple[float, float, float, float] | None:
    """The ground plane under a face, as UDMF `floorplane_a..d`.

    GZDoom reads these as a*x + b*y + c*z + d = 0 with (a,b,c) a unit normal.
    Writing the surface as z = mx*x + my*y + h0 gives the normal as
    (mx, my, -1), normalised.

    Per *sector*, not per vertex. Vertex `zfloor` is shared, and GZDoom applies
    it to any three-sided sector touching that vertex — which silently dragged
    110 building roofs down to terrain height, up to 14m out, wherever a
    footprint happened to be a triangle. A sector plane cannot leak.

    Heights are sampled at the **rounded** corner coordinates, which is what
    makes adjacent triangles agree: the vertex table rounds to integers, so two
    faces sharing an edge sample the identical two points and their planes meet
    along it exactly.
    """
    ring = [(round(x), round(y)) for x, y in list(face.exterior.coords)[:-1]]
    if len(ring) != 3:
        return None

    (x1, y1), (x2, y2), (x3, y3) = ring
    z1, z2, z3 = (height_at(x, y) for x, y in ring)

    # Cross product of two edges gives the plane normal directly.
    ux, uy, uz = x2 - x1, y2 - y1, z2 - z1
    vx, vy, vz = x3 - x1, y3 - y1, z3 - z1
    a = uy * vz - uz * vy
    b = uz * vx - ux * vz
    c = ux * vy - uy * vx
    if c == 0:
        return None  # collinear corners: no plane through them

    norm = (a * a + b * b + c * c) ** 0.5
    if norm == 0:
        return None
    # GZDoom wants the normal pointing up, i.e. c negative in its convention.
    sign = -1.0 if c > 0 else 1.0
    a, b, c = a / norm * sign, b / norm * sign, c / norm * sign
    d = -(a * x1 + b * y1 + c * z1)
    return (a, b, c, d)


def _width(polygon: Polygon) -> float:
    """Rough width of a face: for a long thin strip, area over half-perimeter.

    Exact enough to sort slivers from real faces, which is all it is for.
    """
    if polygon.length <= 0:
        return 0.0
    return 2.0 * polygon.area / polygon.length


def _absorb_slivers(
    owned: list[tuple[Polygon, SectorSpec]],
    edges: dict,
    min_width: float,
) -> list[SectorSpec]:
    """Give hair-thin faces the height of the neighbour they sit against.

    Nearly-coincident input edges — a garden boundary that almost follows a
    kerb, a contour band that almost follows a wall — leave slivers a few
    centimetres wide in the arrangement. Each one becomes its own sector at its
    own floor height, and a 2cm-wide sector standing a metre above what
    surrounds it renders as a tall thin plane hanging in the air. A real tile
    has hundreds.

    Dropping them instead is worse and was the previous behaviour: a discarded
    face leaves the edges it backed onto with a single user, and those render as
    full-height walls or, with a horizon special, as holes in the world.

    So they are kept as geometry and merely re-parented: a sliver adopts the
    spec of its widest neighbour, which makes it flush and invisible while the
    topology stays intact. Specs marked `thin` are exempt — a fence really is
    15cm wide and merging it away would delete it.
    """
    face_spec = [spec for _face, spec in owned]
    if min_width <= 0:
        return face_spec

    widths = [_width(face) for face, _spec in owned]
    slivers = [
        i
        for i, (face, spec) in enumerate(owned)
        if not spec.thin and widths[i] < min_width and face.area > 0
    ]
    if not slivers:
        return face_spec

    neighbours: dict[int, list[int]] = {}
    for uses in edges.values():
        if len(uses) != 2:
            continue
        a, b = uses[0][0], uses[1][0]
        if a != b:
            neighbours.setdefault(a, []).append(b)
            neighbours.setdefault(b, []).append(a)

    sliver_set = set(slivers)
    # Widest first, so a sliver that only touches other slivers is more likely
    # to find one that has already been resolved.
    for index in sorted(slivers, key=lambda i: -widths[i]):
        candidates = [n for n in neighbours.get(index, []) if n not in sliver_set]
        if not candidates:
            # Entirely surrounded by slivers: settle for any neighbour rather
            # than leaving it stranded at its own height.
            candidates = neighbours.get(index, [])
        if not candidates:
            continue
        face_spec[index] = face_spec[max(candidates, key=lambda n: widths[n])]

    return face_spec


def _on_border(
    p: Coord,
    q: Coord,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    tolerance: float = 1.0,
) -> bool:
    """True if the segment p->q lies along one edge of the arrangement's bbox.

    Both endpoints have to sit on the *same* edge. Testing them independently
    would accept a line running corner to corner across the map.
    """
    return (
        (abs(p[0] - min_x) <= tolerance and abs(q[0] - min_x) <= tolerance)
        or (abs(p[0] - max_x) <= tolerance and abs(q[0] - max_x) <= tolerance)
        or (abs(p[1] - min_y) <= tolerance and abs(q[1] - min_y) <= tolerance)
        or (abs(p[1] - max_y) <= tolerance and abs(q[1] - max_y) <= tolerance)
    )


def _scale(spec: SectorSpec, part: str) -> dict:
    """UDMF scale keys for one of a sidedef's three texture slots.

    Omitted entirely at 1.0, both to keep the TEXTMAP readable and because an
    explicit `scalex_mid = 1.000` on every one of ~40k sidedefs is a lot of bytes
    for no effect.
    """
    out = {}
    if spec.wall_scale_x != 1.0:
        out[f"scalex_{part}"] = float(spec.wall_scale_x)
    if spec.wall_scale_y != 1.0:
        out[f"scaley_{part}"] = float(spec.wall_scale_y)
    return out


def _drop_empty_sectors(geo: MapGeometry) -> int:
    """Remove sectors no sidedef references, renumbering what's left.

    Slivers can lose every edge — to integer snapping, or to the self-touching
    check above — leaving a sector with no lines. GZDoom warns about these on
    load ("Sector N has no lines") and they are pure noise in the output.
    """
    used = {side["sector"] for side in geo.sidedefs}
    if len(used) == len(geo.sectors):
        return 0

    remap: dict[int, int] = {}
    kept: list[dict] = []
    for index, sector in enumerate(geo.sectors):
        if index in used:
            remap[index] = len(kept)
            kept.append(sector)

    dropped = len(geo.sectors) - len(kept)
    geo.sectors = kept
    if geo.faces:
        geo.faces = [f for i, f in enumerate(geo.faces) if i in remap]
    for side in geo.sidedefs:
        side["sector"] = remap[side["sector"]]
    return dropped
