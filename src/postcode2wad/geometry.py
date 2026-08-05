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
import math
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
    #: What this region *is*, for measuring land cover. Carried separately from
    #: `floor_tex` because the texture cannot answer it: unclassified terrain is
    #: painted DMGRASS, and so is mapped grassland, so counting flats would score
    #: every unsurveyed field as parkland and inflate greenspace with ground we
    #: simply know nothing about. "terrain" is the honest default -- it means no
    #: land cover was recognised here, not that the ground is green.
    cover: str = "terrain"
    #: Texture repeats per default tiling. A Doom texture is mapped one pixel to
    #: one map unit, so a 128px brick texture spans 4m of wall — which makes each
    #: visible brick course roughly 40cm tall. Scaling up shrinks the texture and
    #: gets the masonry back to a believable size. >1 means "repeat more".
    wall_scale_x: float = 1.0
    wall_scale_y: float = 1.0
    #: Legitimately narrow, so exempt from sliver absorption. A fence really is
    #: 15cm wide; without this flag it would be merged into the ground.
    thin: bool = False
    #: Narrowest this spec is ever legitimately allowed to be, in map units.
    #: `None` means the arrangement-wide sliver threshold applies.
    #:
    #: A building sets this, because unlike a kerb or a verge it has a floor on
    #: how thin it can honestly be. Where a footprint gets clipped by something
    #: unrelated -- a contour band, a garden boundary -- the offcut is a strip
    #: of roof a few centimetres thick standing 10m up, and it renders as a
    #: freestanding brick fin with open ground on both sides and no building
    #: behind it. 69 of those across the nine Birchington tiles, the worst 24m
    #: long. They are too wide for the general sliver threshold, and raising
    #: *that* to catch them would swallow kerbs, which are meant to be narrow.
    min_width: float | None = None
    #: Follow the ground continuously instead of sitting flat. Faces belonging
    #: to a sloped spec are cut into triangles and given per-vertex heights, so
    #: the surface ramps rather than stepping.
    sloped: bool = False
    #: This spec's own height field, overriding the arrangement-wide one for
    #: its faces. Same signature: map units in, floor units out.
    #:
    #: The arrangement-wide sampler is a function of (x, y), which is right for
    #: terrain and wrong for anything built. A road or a pavement is level
    #: across its width by construction, so fitting it to a 2-D field copies
    #: that field's cross-slope noise onto a surface that should not have any.
    #: A spec that knows better -- a road, from its own centreline -- supplies
    #: the height itself.
    #:
    #: Continuity survives this because it never depended on the *sampler*, only
    #: on it being a deterministic function of the rounded corner: two faces
    #: sharing a spec still sample identical values at the two corners they
    #: share, so their planes still meet exactly. Faces on opposite sides of a
    #: spec boundary do step, which is the kerb.
    height_at: object | None = None


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
    #: Ground area in square map units per `SectorSpec.cover`, measured on the
    #: *arrangement*, which is the only place it can honestly be measured. The
    #: input specs overlap — a road is laid over a field, a building over a
    #: garden — so summing their polygons would count the same ground several
    #: times and total well over the tile. Arrangement faces are disjoint by
    #: construction, so these sum to the tile and every square metre is
    #: attributed exactly once, to whatever ended up on top.
    cover_area: dict[str, float] = field(default_factory=dict)
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
    owned = _drop_degenerate_faces(owned)

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
    face_plane = _floor_planes(owned, face_spec, edges, height_at)

    geo = MapGeometry()
    verts = _VertexTable()

    # A sloped face takes its height from its vertices, so its sector's own
    # heightfloor is only a fallback -- but the riser logic below compares floor
    # heights to decide which side of a two-sided line is standing up, and for
    # sloped ground that has to be the real local height, not the spec's.
    face_floor: list[int] = []
    for index, (face, _owner) in enumerate(owned):
        spec = face_spec[index]
        # face_spec, never owned[index][1] — `_absorb_slivers` may have handed
        # this face to a different spec, and the whole point of measuring here
        # rather than on the inputs is to count what actually ended up on the
        # ground. Reading the original owner once put 128 water and building
        # faces on terrain planes; it would put their area in the wrong column
        # just as quietly.
        geo.cover_area[spec.cover] = geo.cover_area.get(spec.cover, 0.0) + face.area
        plane = face_plane[index]
        if plane is not None:
            # Sit the sector's nominal height on its own plane, so the riser
            # logic below compares like with like.
            probe = face.representative_point()
            a, b, c, d = plane
            face_floor.append(round(-(a * probe.x + b * probe.y + d) / c))
        elif spec.sloped and (spec.height_at or height_at) is not None:
            here = spec.height_at or height_at
            ring = list(face.exterior.coords)[:-1]
            heights = [here(x, y) for x, y in ring]
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
        # Index it here rather than reusing a name from the loop above: doing
        # that silently read the *last* face's plane for every sector, which
        # was None, and flattened the entire map without an error anywhere.
        plane = face_plane[index]
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


def _drop_degenerate_faces(
    owned: list[tuple[Polygon, SectorSpec]],
) -> list[tuple[Polygon, SectorSpec]]:
    """Remove faces that collapse under integer vertex snapping.

    Vertices are rounded to integers on emit. A triangle whose corners round to
    two distinct points -- or three collinear ones -- has no interior left at
    map precision, and it is worse than useless: its edges collapse onto the
    same key as the segment its healthy neighbours share, making that edge
    non-manifold, and the "keep the first two users" fallback then silently
    discards one healthy neighbour's side. That leaves the neighbour's sector
    unclosed, and GZDoom's node builder turns unclosed sectors into phantom
    collision pockets. Found by an automated walk test: the player, shoved at
    8 units/tic across open ground at the spawn, moved 1 unit per second.

    Dropping these is safe precisely because they have no rounded interior:
    two of their corners land on the same point, so their edges duplicate the
    segment their healthy neighbours already share. With the face gone, those
    neighbours pair with each other and the topology is manifold again.

    That argument is the whole justification, and it only covers faces whose
    corners *collide*. A second rule used to drop faces with three distinct
    corners but under a square unit of area, and those have three real edges of
    their own -- dropping one orphans all three, and an orphaned edge becomes a
    one-sided line, which GZDoom draws as a solid wall from the ground to the
    sky ceiling. A needle 10m long and a millimetre wide therefore left a
    10m-wide, 130m-tall grass-textured blade standing in the open. 219 of them
    across the nine Birchington tiles, and the reason this went unnoticed for
    so long is that they render as scenery rather than as an error.

    The rule bought nothing anyway: measured on this tile, no edge has more
    than two users with or without it. It dropped 5 extra faces and created 11
    extra holes to do it.
    """
    kept: list[tuple[Polygon, SectorSpec]] = []
    for face, spec in owned:
        ring = [(round(x), round(y)) for x, y in list(face.exterior.coords)[:-1]]
        if len(set(ring)) < 3:
            continue
        kept.append((face, spec))
    return kept


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
    # The floor normal must point UP, into the sector: c positive. The first
    # version of this forced c negative, and the result was diabolical -- the
    # plane RENDERS identically (ZatPoint is sign-agnostic), the player STANDS
    # on it at the right height, but the physics treats everything above a
    # downward-facing floor as inside solid ground and rejects every horizontal
    # move. Symptom: pinned to the spot on open grass, at full render quality,
    # with jumping still working. Only an automated walk test (shove the player
    # four ways, log displacement: 1 unit per second everywhere) made it
    # diagnosable, because every *visual* check passed.
    sign = 1.0 if c > 0 else -1.0
    a, b, c = a / norm * sign, b / norm * sign, c / norm * sign

    # No steepness cap. One was tried while hunting the movement bug, on the
    # theory that near-vertical planes (the worst DTM triangle fits a gradient
    # of 64) were unwalkable. The real cause turned out to be the normal's sign,
    # and the cap made things worse: a rejected plane falls back to the flat
    # ring-average height, and the steps that leaves between neighbours are
    # *harder* barriers than the slope was. Measured on Birchington, unreachable
    # ground went 0.41% -> 2.14% with the cap in. A steep plane is walked
    # correctly, or slid down, which is what a bank should do.
    d = -(a * x1 + b * y1 + c * z1)

    # Does the plane actually pass through the corners it was fitted to?
    #
    # In exact arithmetic that is a tautology, which is why it went unchecked
    # for a long time. In doubles it is not: constrained triangulation produces
    # needles, and the worst here was 1 unit wide by 310 long. Two edges that
    # nearly parallel cancel almost entirely in the cross product, so the
    # normal is mostly rounding error, and the plane -- anchored exactly at
    # corner one -- missed corner three by 2.64 metres.
    #
    # That is invisible as a slope and unmistakable in game: the needle's
    # neighbour fits its own corners correctly, so the two disagree along the
    # 310-unit edge they share, and the join renders as a grass-textured blade
    # nine metres long standing in the sky.
    #
    # Failing the check returns None, and the caller hands the face its
    # neighbour's plane across its longest edge, which is exactly the edge that
    # would otherwise have grown the blade.
    for x, y, z in ((x1, y1, z1), (x2, y2, z2), (x3, y3, z3)):
        if abs((-(a * x + b * y + d) / c) - z) > 1.0:  # 1 unit = 3cm
            return None

    # A fit can be self-consistent and still be nonsense: one survivor across
    # nine tiles had a gradient of 100 -- 89 degrees -- through corners it
    # agreed with to the unit, and its 10cm-long join with a flat neighbour
    # stands up as a 10cm-wide, 10m-tall blade.
    #
    # Rejecting those was tried and is *much* worse, because rejection is
    # contagious: the face takes a neighbour's plane, which then misses its own
    # other corners, and those joins step in turn. Across the nine tiles the
    # worst ground step went from 9.9m on one 10cm edge to 19.8m on ordinary
    # ones. One blade is the better state, and it is logged in TODO.md.
    return (a, b, c, d)


def _width(polygon: Polygon) -> float:
    """Rough width of a face: for a long thin strip, area over half-perimeter.

    Exact enough to sort slivers from real faces, which is all it is for.
    """
    if polygon.length <= 0:
        return 0.0
    return 2.0 * polygon.area / polygon.length




def _floor_planes(
    owned: list[tuple[Polygon, SectorSpec]],
    face_spec: list[SectorSpec],
    edges: dict,
    height_at,
) -> list[tuple[float, float, float, float] | None]:
    """A floor plane per sloped face.

    Every face keeps the plane fitted exactly through its own three corners,
    and that exactness is the whole continuity mechanism: three points define a
    plane, adjacent triangles share two of them, so their planes agree along
    the edge they share -- everywhere, with no tolerance to tune.

    Three rounds of "salvage" were built on top of this to tame steep fits, and
    every one made things worse, because a salvaged plane no longer passes
    through the shared corners and therefore steps the face's whole perimeter.
    Measured on ground-to-ground joins, walls over half a metre: 43 with
    salvage, 10 without. A steep fit left alone is not a spire; it is a steep
    wedge meeting its neighbours exactly, which is what a bank looks like.

    A width threshold was tried here too, on the theory that a face too thin to
    fit anything meaningful through should take a neighbour's plane instead.
    Same mistake in a smaller hat: an adopted plane misses the sliver's *own*
    corners, so every edge it has steps. It tripled the walls over a metre,
    from 8 to 33.

    That leaves only faces with no plane at all -- three corners that round to
    collinear integers, so nothing passes through them. Those adopt the plane
    across their **longest** edge. It cannot be exact everywhere, but it is
    exact along the edge with the most wall to build: the worst case here was a
    ribbon 0.34m wide and 14m long, and a 4m step down its long side is the
    grass-textured blade standing in the sky that started all this.
    """
    def sampler(index: int):
        """The height field for one face: its spec's own, else the tile's."""
        return face_spec[index].height_at or height_at

    planes: list[tuple[float, float, float, float] | None] = []
    for index, (face, _owner) in enumerate(owned):
        spec = face_spec[index]
        if not spec.sloped or sampler(index) is None:
            planes.append(None)
            continue
        planes.append(_floor_plane(face, sampler(index)))

    # Longest shared edge first, so an orphan touching only orphans still finds
    # a resolved neighbour by the time its own turn comes.
    joins: list[tuple[float, int, int]] = []
    for (p, q), uses in edges.items():
        if len(uses) != 2 or uses[0][0] == uses[1][0]:
            continue
        length = math.dist(p, q)
        joins.append((length, uses[0][0], uses[1][0]))
    joins.sort(reverse=True)

    # `face_spec`, not the owner's spec: `_absorb_slivers` may have handed this
    # face a neighbour's spec, and reading the owner's here gave 128 faces a
    # ground plane after the fit loop had (correctly) declined to give them one
    # -- water and building floors tilted to follow the terrain under them, and
    # disagreeing with every neighbour by up to five metres.
    orphans = {
        i
        for i in range(len(owned))
        if face_spec[i].sloped and sampler(i) is not None and planes[i] is None
    }
    # Repeat until nothing more resolves: a run of orphans in a row hands the
    # plane along one face per pass, and stopping after one pass leaves the
    # far end of the run to fall back on level ground.
    resolved = True
    while resolved:
        resolved = False
        for _length, a, b in joins:
            if a in orphans and planes[b] is not None:
                planes[a], resolved = planes[b], True
                orphans.discard(a)
            elif b in orphans and planes[a] is not None:
                planes[b], resolved = planes[a], True
                orphans.discard(b)

    # An orphan with no sloped neighbour at all: level ground at its own height
    # beats leaving the sector at whatever default its spec carries.
    for index in orphans:
        probe = owned[index][0].representative_point()
        here = sampler(index)
        planes[index] = _plane_through(0.0, 0.0, probe.x, probe.y, here(probe.x, probe.y))

    return planes


def _plane_through(
    slope_x: float, slope_y: float, x: float, y: float, z: float
) -> tuple[float, float, float, float]:
    """UDMF plane for z = slope_x*x + slope_y*y + h, passing through (x, y, z).

    Normalised with c positive, because the floor normal must point up into the
    sector: a downward normal renders identically and the player even stands at
    the right height, but the physics treats everything above it as solid and
    refuses all horizontal movement.
    """
    norm = (slope_x * slope_x + slope_y * slope_y + 1.0) ** 0.5
    a, b, c = -slope_x / norm, -slope_y / norm, 1.0 / norm
    return (a, b, c, -(a * x + b * y + c * z))


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
        if not spec.thin
        and widths[i] < (min_width if spec.min_width is None else spec.min_width)
        and face.area > 0
    ]
    if not slivers:
        return face_spec

    # How much boundary each pair of faces shares. The sliver joins whichever
    # neighbour it mostly abuts, which is the one it has to be flush with to
    # disappear.
    #
    # Picking the *widest* neighbour instead is the obvious rule and it is
    # wrong, because a neighbour's own size says nothing about how much of the
    # sliver touches it. A strip of roof clipped off a building, with grass
    # along 90% of its length and one short join to the building it came from,
    # went to the building -- staying at roof height with open ground on both
    # sides, which is a brick fin standing in a garden. 54 of 60 such offcuts
    # on this tile chose the building over the grass they were surrounded by.
    shared: dict[int, dict[int, float]] = {}
    for (p, q), uses in edges.items():
        if len(uses) != 2:
            continue
        a, b = uses[0][0], uses[1][0]
        if a == b:
            continue
        length = math.dist(p, q)
        shared.setdefault(a, {}).setdefault(b, 0.0)
        shared.setdefault(b, {}).setdefault(a, 0.0)
        shared[a][b] += length
        shared[b][a] += length

    sliver_set = set(slivers)
    # Widest first, so a sliver that only touches other slivers is more likely
    # to find one that has already been resolved.
    for index in sorted(slivers, key=lambda i: -widths[i]):
        joins = shared.get(index, {})
        candidates = {n: length for n, length in joins.items() if n not in sliver_set}
        if not candidates:
            # Entirely surrounded by slivers: settle for any neighbour rather
            # than leaving it stranded at its own height.
            candidates = joins
        if not candidates:
            continue
        # Ties broken by the neighbour's own width, which is the old rule and a
        # reasonable second opinion.
        face_spec[index] = face_spec[max(candidates, key=lambda n: (candidates[n], widths[n]))]

    # A building must be closed off by other building faces, or it is not a
    # building -- it is a strip of roof standing on its own with the sky on
    # both sides. The pass above gets nearly all of them, but a long offcut
    # flanked by dozens of tiny ground slivers has no *resolved* neighbour to
    # join except the building it was cut from, so it stays a fin.
    #
    # Resolving to a fixed point fixes that: once the ground slivers around it
    # have taken the grass's spec, the offcut can see grass to join. Three
    # passes settle it; the loop stops early when nothing moves.
    for _pass in range(3):
        moved = False
        for index in slivers:
            spec = face_spec[index]
            if spec.min_width is None or widths[index] >= spec.min_width:
                continue
            joins = shared.get(index, {})
            if not joins:
                continue
            same = sum(length for n, length in joins.items() if face_spec[n] is spec)
            if same * 2 >= sum(joins.values()):
                continue  # genuinely part of a building: leave it flush
            other = {n: length for n, length in joins.items() if face_spec[n] is not spec}
            face_spec[index] = face_spec[max(other, key=lambda n: (other[n], widths[n]))]
            moved = True
        if not moved:
            break

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
