"""A showcase map: one labelled plot per surface and per building type.

    python scripts/build-sandbox.py --out out/sandbox.pk3

Nothing here comes from OSM or LIDAR. It is a synthetic street of specimens
laid out in a known order so a change to the art can be *looked at* rather than
inferred from a real tile, where finding an example of any given surface means
knowing where one is and walking to it.

That is the whole point. Every art change so far has been verified either by
reading the generator or by hunting for an instance in the field — which is how
a glasshouse spent weeks wearing sash windows and a front door without anyone
noticing. A specimen board makes the wrong thing obvious in one screenshot.

**Labelling.** There is no font, so the plots are numbered by *position*: they
run west to east in the order this file lists them, in two rows — ground flats
along the south, buildings along the north — and the legend is printed to the
console on build and written into the PK3 as SANDBOX.txt. Walk east and count.
Each plot is separated by a bare-earth gap so the boundary is unambiguous.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from postcode2wad import UNITS_PER_METRE, art, pk3, textures
from postcode2wad.geometry import SectorSpec, Thing, build_geometry
from postcode2wad.udmf import emit_textmap

M = UNITS_PER_METRE

#: Plot geometry, in metres.
PLOT = 12.0
GAP = 4.0
ROW_GAP = 14.0
GROUND_Y = 6.0
BUILDING_Y = GROUND_Y + PLOT + ROW_GAP

#: Every ground flat that art.py generates, in the order they appear on the map.
GROUNDS = [g.name for g in art.GROUNDS]

#: One specimen per façade style. Height is chosen to exercise the storey maths:
#: the tower must be tall enough to trip TALL_M and show that it *tiles* rather
#: than stretching, which is the whole reason it exists.
BUILDINGS = [
    ("house (domestic brick)", {"building": "house"}, 7.0, 100.0),
    ("semi (domestic, other seed)", {"building": "semidetached_house"}, 7.0, 70.0),
    ("render (domestic, no courses)", {"building": "detached"}, 7.5, 120.0),
    ("church (stone)", {"building": "church"}, 12.0, 300.0),
    ("school (brick)", {"building": "school"}, 9.0, 800.0),
    ("warehouse (clad, shutter)", {"building": "warehouse"}, 9.0, 2000.0),
    ("untagged, 1200m2 -> industrial", {"building": "yes"}, 8.0, 1200.0),
    ("untagged, 90m2 -> domestic", {"building": "yes"}, 6.5, 90.0),
    ("glasshouse (Thanet Earth)", {"building": "greenhouse"}, 6.0, 90000.0),
    ("tower 24m (tiles, 8 floors)", {"building": "office"}, 24.0, 900.0),
    ("tower 96m (Big Ben height)", {"building": "office"}, 96.0, 900.0),
    ("tower 310m (Shard height)", {"building": "commercial"}, 310.0, 2500.0),
]

#: Historic and civic specimens. These route through the `historic` /
#: place_of_worship branch rather than the building= table, which is how
#: Canterbury Cathedral gets a cathedral's façade without anyone naming it:
#: it is tagged, so it is styled. Worth its own row because that branch is easy
#: to break and impossible to notice breaking on a real tile.
HISTORIC = [
    ("cathedral (tagged building=)", {"building": "cathedral"}, 32.0, 4000.0),
    ("church, place_of_worship", {"amenity": "place_of_worship"}, 14.0, 400.0),
    ("chapel", {"building": "chapel"}, 8.0, 150.0),
    ("historic=* (any)", {"building": "yes", "historic": "yes"}, 10.0, 200.0),
    ("castle", {"building": "yes", "historic": "castle"}, 22.0, 1500.0),
    ("ruins", {"building": "yes", "historic": "ruins"}, 5.0, 120.0),
    ("windmill", {"building": "yes", "historic": "windmill"}, 16.0, 60.0),
    ("lighthouse", {"building": "yes", "historic": "lighthouse"}, 26.0, 50.0),
]


#: Barrier specimens — the extruded, thin things. A barrier is 15cm wide in
#: life, so these are drawn at a legible 0.4m and flagged `thin` exactly as the
#: real ones are, or sliver absorption would swallow them.
BARRIERS = [
    ("hedge", {"barrier": "hedge"}, 1.8),
    ("fence", {"barrier": "fence"}, 1.4),
    ("brick wall", {"barrier": "wall"}, 2.0),
    ("dry stone wall", {"barrier": "dry_stone_wall"}, 1.2),
]

#: Terrain steps, in metres. Doom's climb limit is 24 units (0.75m), so the
#: first two are walkable and the rest are not — which is the point. A 10m step
#: is what a chalk cliff actually is, and its riser is currently painted with
#: the same flat as the ground, which is the thing to look at.
STEPS = [0.5, 0.75, 2.0, 5.0, 10.0, 25.0]

#: Tree heights in metres. `vegetation` scales one actor across this whole
#: range, so a specimen row is the only way to see whether the scaling reads.
TREE_HEIGHTS = [3.0, 6.0, 10.0, 16.0, 25.0]


def build() -> tuple[str, str]:
    """Returns (TEXTMAP, legend)."""
    specs: list[SectorSpec] = []
    legend: list[str] = []

    # Wide enough for the longest row, deep enough for all six.
    width = (max(len(GROUNDS), len(BUILDINGS), len(HISTORIC), len(STEPS)) + 1) * (PLOT + GAP) + PLOT
    depth = BUILDING_Y + 12 * (PLOT + ROW_GAP) + 100.0
    sky = int(400.0 * M)  # clears the 310m specimen with room to spare

    # A bare-earth base under everything, so the gaps between plots read as
    # deliberate rather than as holes.
    specs.append(
        SectorSpec(
            polygon=box(0, 0, width * M, depth * M),
            floor=0,
            ceiling=sky,
            floor_tex=textures.GRAVEL,
            wall_tex=textures.KERB,
            light=208,
        )
    )

    legend.append("ROW 1 — GROUND FLATS (behind spawn), west to east:")
    for index, name in enumerate(GROUNDS):
        x = GAP + index * (PLOT + GAP)
        specs.append(
            SectorSpec(
                polygon=box(x * M, GROUND_Y * M, (x + PLOT) * M, (GROUND_Y + PLOT) * M),
                floor=0,
                ceiling=sky,
                floor_tex=name,
                wall_tex=textures.KERB,
                light=208,
            )
        )
        legend.append(f"  {index + 1:2d}. {name}")

    legend.append("")
    legend.append("ROW 2 — BUILDINGS (you face these), west to east:")
    for index, (label, tags, height_m, footprint_m2) in enumerate(BUILDINGS):
        x = GAP + index * (PLOT + GAP)
        wall, scale_y = textures.building_facade(tags, index, height_m, footprint_m2)
        specs.append(
            SectorSpec(
                polygon=box(
                    x * M, BUILDING_Y * M, (x + PLOT) * M, (BUILDING_Y + PLOT) * M
                ),
                floor=round(height_m * M),
                ceiling=sky,
                floor_tex=textures.ROOF,
                wall_tex=wall,
                wall_scale_y=scale_y,
                light=216,
            )
        )
        legend.append(f"  {index + 1:2d}. {label}  [{wall}, {height_m:g}m]")

    # --- row 2b: historic and civic ----------------------------------------
    historic_y = BUILDING_Y + PLOT + ROW_GAP
    legend.append("")
    legend.append("ROW 3 — HISTORIC / CIVIC, west to east:")
    for index, (label, tags, height_m, footprint_m2) in enumerate(HISTORIC):
        x = GAP + index * (PLOT + GAP)
        wall, scale_y = textures.building_facade(tags, index + 40, height_m, footprint_m2)
        specs.append(
            SectorSpec(
                polygon=box(
                    x * M, historic_y * M, (x + PLOT) * M, (historic_y + PLOT) * M
                ),
                floor=round(height_m * M),
                ceiling=sky,
                floor_tex=textures.ROOF,
                wall_tex=wall,
                wall_scale_y=scale_y,
                light=216,
            )
        )
        legend.append(f"  {index + 1:2d}. {label}  [{wall}, {height_m:g}m]")

    # --- row 3: barriers, extruded thin ------------------------------------
    barrier_y = historic_y + PLOT + ROW_GAP
    legend.append("")
    legend.append("ROW 4 — BARRIERS, west to east:")
    for index, (label, tags, height_m) in enumerate(BARRIERS):
        x = GAP + index * (PLOT + GAP)
        specs.append(
            SectorSpec(
                polygon=box(x * M, barrier_y * M, (x + PLOT) * M, (barrier_y + 0.4) * M),
                floor=round(height_m * M),
                ceiling=sky,
                floor_tex=textures.HEDGE_TOP
                if tags.get("barrier") == "hedge"
                else textures.CONCRETE,
                wall_tex=textures.barrier_wall(tags),
                light=208,
                thin=True,
            )
        )
        legend.append(f"  {index + 1:2d}. {label}  [{height_m:g}m]")

    # --- row 4: terrain steps, walkable and not ----------------------------
    step_y = barrier_y + PLOT
    legend.append("")
    legend.append("ROW 5 — TERRAIN STEPS, west to east (climb limit 0.75m):")
    for index, height_m in enumerate(STEPS):
        x = GAP + index * (PLOT + GAP)
        specs.append(
            SectorSpec(
                polygon=box(x * M, step_y * M, (x + PLOT) * M, (step_y + PLOT) * M),
                floor=round(height_m * M),
                ceiling=sky,
                floor_tex=textures.GRASS,
                wall_tex=textures.TERRAIN_SIDE,
                light=208,
            )
        )
        climb = "walkable" if height_m <= 0.75 else "blocked"
        legend.append(f"  {index + 1:2d}. {height_m:g}m step — {climb}")

    # --- row 5: water, sea and ballast, since these carry their own depths --
    water_y = step_y + PLOT + ROW_GAP
    legend.append("")
    legend.append("ROW 6 — SURFACES WITH DEPTH, west to east:")
    for index, (label, flat, drop_m) in enumerate(
        [
            ("water (2m below grade)", textures.WATER, -2.0),
            ("sea (at datum, 24u down)", textures.WATER, -0.75),
            ("railway ballast", textures.BALLAST, -0.25),
            ("road with kerb", textures.ROAD, -0.25),
            ("pavement", textures.PAVEMENT, 0.0),
        ]
    ):
        x = GAP + index * (PLOT + GAP)
        specs.append(
            SectorSpec(
                polygon=box(x * M, water_y * M, (x + PLOT) * M, (water_y + PLOT) * M),
                floor=round(drop_m * M),
                ceiling=sky,
                floor_tex=flat,
                wall_tex=textures.KERB,
                light=208,
            )
        )
        legend.append(f"  {index + 1:2d}. {label}")

    # --- row 6: the bridge problem, shown rather than described ------------
    #
    # Not a working bridge — there is no such thing here yet. This is the
    # failure laid out so it can be seen: a raised deck crossing a road, which
    # is what OSM asks for and what a Doom sector cannot express. One floor
    # height per point means the deck and the carriageway under it cannot both
    # exist, so what you get is a wall across the road. It is in the sandbox
    # because "bridges render as dips" took two builds and two screenshots to
    # understand, and the next person should be able to see it in ten seconds.
    bridge_y = water_y + PLOT + ROW_GAP
    legend.append("")
    legend.append("ROW 7 — BRIDGE (known limitation):")
    specs.append(
        SectorSpec(
            polygon=box(GAP * M, bridge_y * M, (GAP + 60) * M, (bridge_y + 8) * M),
            floor=round(-0.25 * M),
            ceiling=sky,
            floor_tex=textures.ROAD,
            wall_tex=textures.KERB,
            light=200,
        )
    )
    specs.append(
        SectorSpec(
            polygon=box(
                (GAP + 24) * M, (bridge_y - 10) * M, (GAP + 36) * M, (bridge_y + 18) * M
            ),
            floor=round(6.0 * M),
            ceiling=sky,
            floor_tex=textures.ROAD,
            wall_tex=textures.KERB,
            light=200,
        )
    )
    legend.append("   1. a 6m deck crossing a road — note it BLOCKS the road")
    legend.append("      rather than passing over it. Doom sectors have one")
    legend.append("      floor height per point; this needs GZDoom 3D floors.")

    # --- row 7: roads at their real widths, with kerbs ---------------------
    #
    # Laid as running strips rather than square plots, because a carriageway is
    # a thing you judge by walking down it: the kerb reveal, how a pavement sits
    # against it, and whether the width feels like the road it claims to be.
    road_y = bridge_y + 24.0
    legend.append("")
    legend.append("ROW 8 — ROADS at real widths, running east-west:")
    for index, (label, flat, width_m, drop_m) in enumerate(
        [
            ("motorway 22m", textures.ROAD, 22.0, -0.25),
            ("primary 10m", textures.ROAD, 10.0, -0.25),
            ("residential 6m", textures.ROAD, 6.0, -0.25),
            ("pavement 2m", textures.PAVEMENT, 2.0, 0.0),
            ("footway 1.5m", textures.PAVEMENT, 1.5, 0.0),
            ("railway 9m (ballast)", textures.BALLAST, 9.0, -0.25),
        ]
    ):
        y = road_y + index * 6.0
        specs.append(
            SectorSpec(
                polygon=box(GAP * M, y * M, (GAP + 90) * M, (y + width_m) * M),
                floor=round(drop_m * M),
                ceiling=sky,
                floor_tex=flat,
                wall_tex=textures.KERB,
                light=200,
            )
        )
        legend.append(f"  {index + 1:2d}. {label}")

    # --- row 8: sloped ground, which is how all real terrain renders --------
    #
    # Everything above is a flat sector, and almost nothing in a real tile is:
    # ground is cut into triangles carrying per-vertex heights, and the whole
    # continuity mechanism is that adjacent triangles share two corners so their
    # planes agree exactly along the shared edge. A specimen board of flat
    # squares would therefore miss the surface the player actually walks on.
    slope_y = road_y + 6 * 6.0 + ROW_GAP
    legend.append("")
    legend.append("ROW 9 — SLOPED GROUND, west to east (gradient):")
    for index, (label, rise_m) in enumerate(
        [("1 in 20 (gentle)", 1.5), ("1 in 8 (a hill)", 3.75),
         ("1 in 4 (steep bank)", 7.5), ("1 in 2 (very steep)", 15.0)]
    ):
        x = GAP + index * (PLOT + GAP)
        run = PLOT * M

        def ramp(px, py, x0=x * M, rise=rise_m * M, run=run):
            # Linear in x across the plot, which gives a plane the fitter can
            # reproduce exactly — so any stepping seen here is a real defect.
            return rise * max(0.0, min(1.0, (px - x0) / run))

        specs.append(
            SectorSpec(
                polygon=box(x * M, slope_y * M, (x + PLOT) * M, (slope_y + PLOT) * M),
                floor=0,
                ceiling=sky,
                floor_tex=textures.GRASS,
                wall_tex=textures.TERRAIN_SIDE,
                light=208,
                sloped=True,
                height_at=ramp,
            )
        )
        legend.append(f"  {index + 1:2d}. {label} — rises {rise_m:g}m over {PLOT:g}m")

    # --- trees, in their own row -------------------------------------------
    #
    # These were first placed along the far south edge, behind a spawn that
    # faces north — so the answer to "are there trees?" was "yes, and you will
    # never see them". A specimen only counts if it is in the walk.
    tree_y = slope_y + PLOT + ROW_GAP
    legend.append("")
    legend.append("ROW 10 — TREES, west to east (one actor, scaled):")
    things: list[Thing] = []
    for index, height_m in enumerate(TREE_HEIGHTS):
        x = GAP + index * (PLOT + GAP) + PLOT / 2
        things.append(
            Thing(
                x=round(x * M),
                y=round(tree_y * M),
                type=art.TREE_DOOMEDNUM,
                # The same scaling `vegetation` uses, so what stands here is
                # what stands on a real tile.
                scale=height_m / art.TREE_HEIGHT_M,
            )
        )
        legend.append(f"  {index + 1:2d}. {height_m:g}m tree")

    # Player start in the middle of the gap between the rows, facing north at
    # the buildings — the row that changes most often.
    start_x = width * M / 2
    start_y = (GROUND_Y + PLOT + ROW_GAP / 2) * M
    things.append(Thing(x=round(start_x), y=round(start_y), angle=90, type=1))

    geo = build_geometry(specs, things=things)
    return emit_textmap(geo, comment="postcode2wad sandbox"), "\n".join(legend)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-sandbox.py", description=__doc__)
    parser.add_argument("--out", default="out/sandbox.pk3", metavar="PATH")
    args = parser.parse_args(argv)

    textmap, legend = build()
    pk3.write_pk3(
        args.out,
        textmap,
        map_name="MAP01",
        title="postcode2wad sandbox",
        extra_files={"SANDBOX.txt": legend.encode()},
    )
    print(legend)
    print()
    print(f"wrote {args.out}")
    print("You spawn between the rows facing the buildings. Ground flats are")
    print("behind you. Both rows run west to east in the order listed above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
