# Postcode → WAD — Project Brief

**Date:** 2026-08-02 · **Status:** current, except the positioning in "Why this
is worth doing", which was revised on 5 Aug against
[`prior-art.md`](prior-art.md).

**One-liner:** Type a UK postcode, get a playable GZDoom level of that real
place, generated from Environment Agency LIDAR terrain and OpenStreetMap
building footprints.

## Why this is worth doing

Real geodata has been turned into game worlds before, including from a UK
postcode using UK LIDAR ([geocraft](https://github.com/rob-murray/geocraft),
2015) and into another BSP-format shooter
([osm2vmf](https://github.com/lewa-j/osm2vmf), 2023). The distinctions that
hold up are narrower and technical:

- one **measured** height per footprint, rather than per raster column;
- **sector-partition geometry**, rather than a voxel grid or a triangle mesh;
- **measured walkability** — one-sided-line sweeps, reachability by enclosure,
  scripted walktests.

[`prior-art.md`](prior-art.md) has the evidence for each, and the list of
claims that did not survive.

> *Original, 2 Aug:* "The 'can it run Doom' space has no real-world-geodata
> entry. All existing Doom map generation is procedural (OBLIGE/Obsidian
> lineage). Minecraft has Build the Earth and Arnis; Doom has nothing.
> First-mover claim on the whole 'Doom Earth' category." Written before the
> prior-art search; superseded by it.

Golden demo, unchanged: **"Play Doom in Birchington"** — spawn at a real Thanet
postcode.

**Pre-announce TODO:** search the Doomworld and ZDoom forums by hand. They are
poorly indexed by Google and Cloudflare-blocked to automated agents, so that
corpus is unsearched, and it is where a 2010s "I converted OSM into a WAD"
thread would sit. Search terms: "real world", "OSM", "OpenStreetMap", "lidar",
"my house", "my street" in the WADs & Mods and Editing sections.

## Deliverable shape

CLI first, web later:

```
postcode2wad "CT7 0XX" --size 400 --out birchington.pk3
gzdoom -iwad freedoom2.wad -file birchington.pk3 +map MAP01
```

## Milestones

- **M0 — Geometry proof.** Synthetic heightmap + two hand-defined building polygons → valid UDMF TEXTMAP in a PK3 that loads in GZDoom and can be walked around. No real data yet. *This de-risks the whole project — do it first.*
- **M1 — Real data.** Postcode → LIDAR tile + footprints → walkable terrain with solid buildings at real heights. Player start at postcode centroid.
- **M2 — Dressing.** Roads/pavements, land-cover flats, tree sprites from DSM−DTM blobs, texture themes from OSM tags (church/pub/shop), generated street-name sign textures, skybox/fog/streetlight dynamic lights, basic monster + item placement, keys on landmarks. **Geolocation HUD**: live lat/lng + OS grid ref of the player, via per-tile georeference baked into the map (affine: origin easting/northing @ 32 units/m) read by a ZScript HUD element; use a precomputed local linear OSGB→WGS84 approximation per tile (sub-cm over 400m) rather than full datum conversion at runtime. Also accept `--latlng` as input, and a share key printing a maps link to console.
- **M3 — Ship.** Web page: postcode in, PK3 out; stretch goal in-browser play via wasm GZDoom.
- **M4 — World mode baseline.** Hub-linked tile grid with edge transitions; tune out the screen wipe and **measure the travel hitch** on real tiles. This is the benchmark the fork must beat.
- **M5 — Engine fork: streaming prototype.** Fork GZDoom (GPL — fork stays GPL, fine) and build `SeamlessTravel`: async-preload the neighbouring tile as the player approaches an edge, then swap maps preserving exact player state (position, velocity, view angles, inventory) with no wipe. Acceptance: walk across a tile boundary at full run with continuous velocity and no perceptible hitch. *(Originally "goal claim: the first streaming Doom source port" — dropped, unverified against the same unsearched forum corpus noted above.)*

## Pipeline architecture

1. **Postcode → coordinates.** postcodes.io (free API, no key) or OS Code-Point Open for offline. Output lat/lon → reproject to OSGB EPSG:27700 (pyproj) since all UK data lives there.
2. **Terrain.** DEFRA National LIDAR Programme, 1m DTM + DSM (first/last return), GeoTIFF. Clip to bbox (default 400m square, configurable).
3. **Vectors.** OSM via Overpass API: `building`, `highway`, `landuse`, `natural=water`, `amenity` tags. Optionally OS OpenMap Local / OS NGD as higher-quality UK alternative — evaluate both.
4. **Terrain sectorisation.** Quantise DTM into contour steps (start ~1m steps), polygonise (`rasterio.features.shapes`), simplify + merge aggressively. This is the hard part — sector count explodes if naive. Budget: < ~15–20k sectors per map.
5. **Buildings.** Footprint polygon → sector with floor raised to terrain + building height, where height = P90(DSM − DTM) inside the footprint (median underestimates pitched roofs). Buildings are solid (impassable raised sectors) in M1; enterable interiors are a later stretch.
6. **Roads.** Buffer highway centrelines by tagged width → flat sectors, asphalt texture; pavements as parallel sectors raised ~8 units with kerb step.
7. **Geometry hygiene.** `shapely.make_valid`, `simplify(tolerance)`, snap vertices to integer grid, drop slivers below area threshold, ensure no self-intersections or duplicate vertices — UDMF tolerates a lot but degenerate geometry still breaks node building/rendering.
8. **Emit UDMF.** Write `TEXTMAP` lump (plain text: vertices, linedefs, sidedefs, sectors, things), `namespace = "zdoom"`. Package as PK3 (a zip): `maps/MAP01.wad` (marker + TEXTMAP + ENDMAP) plus MAPINFO, textures.
9. **Things.** Player 1 start at postcode centroid (thing type 1), monsters/items per M2 rules.

## Fixed technical decisions

- **Engine target: GZDoom, UDMF format.** Vanilla/Boom is a non-starter: no slopes, visplane/seg limits, 16-bit coordinate ceilings. UDMF is text (easy to emit and diff) and GZDoom-native.
- **Scale: 32 map units ≈ 1 metre.** Player is 56 units tall (~1.75m), radius 16 — real streets read correctly. A 400m tile = 12,800 units, comfortably inside range.
- **Assets: Freedoom IWAD** so the whole thing ships FOSS with no doom2.wad dependency. Custom UK texture pack (brick, pebbledash, shopfront) added in M2.
- **Stack: Python 3.11+** — rasterio, shapely 2.x, pyproj, numpy, requests. No GIS monoliths (no QGIS/GDAL CLI dependency beyond what rasterio bundles).
- **Tile-grid determinism (design in from M0).** Generation is a pure function of a global tile ID on the British National Grid (default 400m cells) — a postcode only selects the starting tile. Same tile ID → byte-identical map. Contour quantisation uses globally fixed absolute elevation bands (not per-tile min/max) and geometry is clamped identically at tile borders, so adjacent tiles' shared edges always match.

## World mode (M4) and engine fork (M5)

Doom cannot stream or hot-load geometry — sectors are fixed at map load. Two-stage plan:

**Baseline (M4), stock GZDoom:**
- Region pack: one PK3 containing N×N tiles as MAPxx lumps in a MAPINFO **hub cluster** (preserves health/inventory/keys; skips intermission).
- Edge transitions: ACS on border linedefs fires `Teleport_NewMap` to the neighbour's matching landing spot. Small tiles load in tens of ms — measure the perceived hitch; this sets the bar.
- Line/sector **portals** can seamlessly stitch discontiguous regions *within* one map — useful tool, doesn't beat the map-size ceiling.

**Streaming fork (M5), DECIDED — we are forking:**
- Fork github.com/ZDoom/gzdoom. Windows build: CMake + vcpkg, needs ZMusic. Keep the fork rebased on upstream; isolate changes behind a `seamless_travel` cvar.
- Approach: on nearing a tile edge, load the neighbour map's data structures in the background, then perform the map switch as a state transplant — carry over player position (re-based to the new tile's coordinate frame), velocity, view angles, pending input, and interpolation state, with no screen wipe. Tiles are tiny, so the swap should cost a frame or two even without full async.
- Key lead: GZDoom ~4.0 refactored global level state into per-instance `FLevelLocals` (`primaryLevel`) explicitly as groundwork for multiple-levels-in-memory. Investigate how complete that groundwork is — it may make this far cheaper than expected.
- Fallback if the fork stalls: polished M4 hubs are ~90% of the feel.

## Research areas (verify before building — do not trust this brief blindly)

1. **UDMF spec + minimal viable TEXTMAP.** udmf spec v1.1 + ZDoom extensions (doomwiki.org/wiki/UDMF, zdoom.org wiki). Confirm the smallest map GZDoom will load.
2. **Node building.** Confirm current GZDoom builds nodes internally for UDMF maps at load, or whether ZDBSP/ZDRay must run in the pipeline. This changes packaging.
3. **DEFRA LIDAR programmatic access.** The Defra Survey Data Download portal is interactive; find the programmatic route — Defra Data Services Platform WCS/WMS, the ArcGIS REST services for "LIDAR Composite DTM/DSM 1m", or bulk tile URLs. Check licence (expect OGL) and coverage gaps.
4. **OS data route.** Code-Point Open download + licence; OS NGD/OpenMap building footprints vs OSM completeness for Kent as a test case.
5. **Overpass etiquette.** Rate limits, mirror endpoints, bbox query patterns; cache responses locally.
6. **Sector decimation strategy.** Research approaches: contour-band polygonisation vs quadtree terrain vs Delaunay-then-merge. Also check GZDoom's practical sector/linedef performance ceiling on a mid PC.
7. **Slopes.** UDMF/GZDoom slope mechanics (plane_align linedef special, vertex slopes) — could replace many contour steps with far fewer sloped sectors. Big potential win; assess complexity.
8. **PK3 structure + MAPINFO.** Correct layout for a map-in-wad-in-pk3 with custom textures/flats (TEXTURES lump vs raw PNGs in textures/ + flats/).
9. **wasm GZDoom.** Viability of in-browser play (existing GZDoom Emscripten ports, their UDMF support and licence). If weak, fall back to "download PK3" for M3.
10. **Hubs + edge transitions.** MAPINFO hub cluster syntax, `Teleport_NewMap`/ACS edge triggers, landing-spot placement, max maps per PK3, practical single-map size ceiling in current GZDoom, and any state that hubs don't preserve.
11. **ZScript HUD + map metadata.** How to bake per-tile georeference constants into the PK3 (UDMF custom fields vs generated ZScript lump vs LOADACS), ZScript custom StatusBar/overlay basics, per-tick player position access, console printing for the share key.
12. **Engine internals for streaming.** Map the load/transition path in the GZDoom source: `P_SetupLevel`, `G_ChangeLevel`, hub state serialisation in `FLevelLocals`; how far the primaryLevel/multi-level refactor goes; what player/camera/interpolation state must transplant for a hitchless swap; whether background loading is feasible given the engine's threading model, or a synchronous frame-budgeted load suffices for tiny tiles. Also: runtime file mounting — can the fork additionally hot-mount new tile PK3s so the world extends without restart?
13. **Fork hygiene.** Windows 11 build chain (CMake + vcpkg + ZMusic), upstream rebase strategy, GPL compliance for distribution (publish source with binaries).
14. **Dynamic texture generation.** Rendering street-name/village-sign textures at build time (Pillow → PNG in PK3) — font licensing for a UK road-sign look (Transport typeface alternatives).

## Gotchas already known

- DSM includes trees and cars — only use DSM−DTM *inside* building footprints for heights; blobs *outside* footprints become tree sprite candidates.
- Real footprints contain self-intersections, holes (courtyards), and shared walls — shared walls need deduplicated linedefs or you get double-sided weirdness.
- LIDAR has NODATA holes (water, absorption) — fill before quantising.
- Keep all coordinates integer-snapped; UDMF accepts floats but integers avoid slime trails and speed up dedup.
- Freedoom monster/thing editor numbers match Doom 2's — safe to use standard DoomEdNums.

## Distribution (DECIDED)

Fully open source, released free on itch.io.

- Engine fork: GPLv3 (inherited), source published alongside binaries — satisfied automatically by open-sourcing everything.
- Generator + tooling: open source (pick licence at repo creation; MIT unless a reason emerges).
- Release checklist: attribution for DEFRA/OS data (OGL), OpenStreetMap (ODbL "© OpenStreetMap contributors"), Freedoom credits, GZDoom upstream credits. No id Software assets ever redistributed; avoid "Doom" in the product name itself (trademark) — descriptive use in copy is fine.

## Acceptance test

`postcode2wad` on a residential postcode produces a PK3 that loads in current GZDoom with Freedoom Phase 2, spawns the player on the street at the postcode centroid, terrain visibly matches the real village (streets, church, coastline recognisable on the automap), stable 60fps+, no console errors.
