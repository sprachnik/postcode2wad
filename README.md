# postcode2wad

Type any UK postcode, get a playable GZDoom level of that real place — generated from
DEFRA LIDAR terrain and real building footprints.

> "Arnis, but for Doom."

```
postcode2wad "CT7 0XX" --size 400 --out birchington.pk3
gzdoom -iwad freedoom2.wad -file birchington.pk3 +map MAP01
```

## Status

**M0 (geometry proof), M1 (real data), M2 (dressing) and M4 (region packs) are done and
playable, and both data sources now read from local extracts so a whole county is in
reach.**
Give it a UK postcode and it produces a PK3 that loads clean in GZDoom 4.14 with
Freedoom Phase 2: real building footprints at LIDAR-measured heights, roads with kerbs,
terrain contoured from 1m Environment Agency LIDAR, land cover from OSM tags, hedges
and fences along property boundaries, and trees found in the LIDAR canopy.

Freedoom is a demon shooter's texture set — no daylight sky, no domestic brickwork and
above all no windows — so most of what you see is generated instead (`art.py`) and
shipped inside the PK3: the sky, twelve ground flats, hedge and fence materials, tree
billboards, and building facades with sash windows, sills, doors and eaves at real
storey heights.

```
postcode2wad "CT1 2EH" --size 400 --out birchington.pk3 --preview plan.png
```
```
CT1 2EH — Birchington, Thanet  (51.36099, 1.26650)  OSGB 627500E 167500N
tile bng400-1574-423  (400m square)
74 buildings, 90 road pieces, 0 water, 0 sea, 0 beach, 41 land parcels, 33 barriers,
574 trees, 12 terrain bands, terrain 12.0-20.0m
5346 sectors, 8366 linedefs
```

The terrain slopes continuously rather than stepping: every ground triangle carries a
floor plane fitted through its own corners, so adjacent faces meet exactly. Roads take
their height from a smoothed profile along the way's centreline instead of the 2-D
LIDAR field, because a pavement is an engineered surface — level across its width and
graded along its length — and fitting one to a noisy raster reproduces the noise.

A region pack builds a block of neighbouring tiles into one PK3 with a level change at
each tile edge, and a ZScript HUD carries a minimap, compass and live lat/lng:

```
postcode2wad "CT1 2EH" --size 800 --region 1 --out out/birchington-area.pk3
```

A whole local authority district goes in one PK3, built in parallel:

```
python scripts/build-district.py --district Thanet --size 800 --workers 8 --out out/thanet.pk3
```

Thanet is **200 tiles at 800m — 103 km² of Kent coast, 1.47M sectors, 111 MB —
in 28.5 minutes** on 8 workers, with no failures. Tiles come from the ONS district
polygon rather than a bounding box, one subprocess builds each one, and every finished
tile is a resumable artifact on disk, so a run measured in hours cannot lose everything
to a single bad tile. Packs of more than 99 maps are named `M0001`.. rather than
`MAP01`, which lifts the ceiling to 9,999.

**And all of it is playable in a browser: [doomearth.clawhangout.com](https://doomearth.clawhangout.com).**
Pick any of Thanet's 200 tiles off a minimap grid, choose a performance mode, spawn on
it, and walk to a tile edge to cross into the neighbour — the launcher reads the HUD's
telemetry off the engine's console stream, loads the next tile when you push into the
boundary, and hands your position across so you come out on the same street rather than
at the new tile's spawn point. Phones get on-screen controls; you start with your fists
rather than a gun (`?weapon=none` for empty hands). The engine is
[uzdoom-wasm](https://github.com/abootnet/uzdoom-wasm)
(UZDoom under Emscripten); the site is nothing but static files on Cloudflare R2.
`scripts/build-webdemo.py` assembles it from district artifacts, and
[`docs/web-demo.md`](docs/web-demo.md) records what it took — four boot failures that
all presented as the same black screen, and the FPS measurement that decided single
tiles over region packs.

Still to come: street-name signs, and a bulk DSM mirror for eastern Kent — the district
run above is network-bound, not CPU-bound, because the Environment Agency's bulk 1m DSM
is missing for the whole TR grid square (see [`docs/lidar-bulk.md`](docs/lidar-bulk.md)).

See [`TODO.md`](TODO.md) for the working list — including the known rough edges —
[`CLAUDE.md`](CLAUDE.md) for how to work on it without repeating old mistakes, and
[`docs/brief.md`](docs/brief.md) for the full brief and milestones.

## How it works

1. **postcodes.io** turns a postcode into OSGB eastings/northings (no key needed).
2. That picks a **British National Grid tile** — generation is a pure function of the
   tile ID, so neighbouring tiles agree along their shared edge.
3. **Environment Agency LIDAR** 1m DTM and DSM arrive as float32 GeoTIFF — read out
   of a local mirror of the EA's 5km grid squares if one is there, else over WCS.
4. **OpenStreetMap** supplies buildings, roads, water, coastline, land use and barriers,
   in a single query per tile — off a local `.osm.pbf` extract if there is one, else
   from the **Overpass API** (see "Building more than a few tiles" below).
5. The DTM is quantised into *absolute* elevation bands and polygonised into contours.
6. Everything is thrown into a **planar arrangement** (`geometry.py`) — the step that
   turns overlapping real-world polygons into the topology Doom actually requires.
7. DSM minus DTM, outside the mapped footprints, gives the tree canopy.
8. Out comes a UDMF `TEXTMAP`, wrapped in a WAD, zipped into a PK3 alongside the
   generated art and a MAPINFO that sets the sky and distance haze.

GZDoom builds the BSP nodes itself at load, so there is no ZDBSP in the pipeline.

## Constraints worth knowing

All of these are load-bearing, all were found the hard way, and all are easy to get
wrong again:

- **Doom will not step up more than 24 map units** — 0.75m at our scale. Contour steps
  at or above that are invisible walls: the terrain looks fine and the player simply
  cannot climb it. Default step is 0.5m.
- **On a two-sided line the lower texture is drawn on the side facing the lower
  sector**, but it is the *higher* sector that is standing up. Paint it from the wrong
  side and every building wall gets the terrain's texture.
- **`Line_Horizon` belongs only on the map's outer edge.** It renders as an infinite
  flat plane, so on any interior line it is a visible tear in the world. One-sided lines
  are not automatically the tile perimeter — dropped sliver faces orphan interior edges
  too.
- **A Doom sprite hangs downward from its top offset.** Pillow writes no `grAb` chunk,
  the engine reads the offset as zero, and the whole sprite renders below the floor.
- **A one-sided line away from the map edge is a wall from the ground to the sky.**
  Outdoor ceilings are ~130m, so an orphaned edge — one claimed by a single face rather
  than two — is not a hairline crack but a grass-textured blade the height of a tower
  block. Dropping a degenerate face orphans every edge it owned.
- **A texture pixel is one map unit.** A 128px brick texture therefore spans 4m of wall,
  making every brick course about 40cm — three times life size. Nothing announces this;
  it just makes buildings feel wrong.

## Building more than a few tiles

Overpass is donated infrastructure and rate-limits at around **nine tiles**. Kent at
800m is 5,800 tiles, so bulk generation reads OSM off a local extract instead:

```
curl -O https://download.geofabrik.de/europe/united-kingdom/england/kent-latest.osm.pbf
move kent-latest.osm.pbf data\
postcode2wad "CT1 2EH" --size 800 --out out/tile.pk3      # uses it automatically
```

The first run scans the file once (51s for Kent's 50MB, 762k ways) into an index under
`cache/osmpbf/`; every tile after that is a query against the index — **3ms median, 20ms
p95, 80ms worst case** on an 800m tile, against 131s over Overpass. The index is keyed on
the extract's path, size and mtime, so a fresh download rebuilds it rather than serving
stale roads.

`--osm-source` picks the reader: `auto` (default — the extract if one covers the tile,
Overpass otherwise), `local`, or `overpass`. `--osm-extract PATH` or `$POSTCODE2WAD_OSM_EXTRACT`
names the file; otherwise the first `data/*.osm.pbf` is used. Both readers produce
byte-identical PK3s — that is pinned by `tests/test_osmpbf.py`.

Terrain goes the same way. The EA publishes the same composite it serves over WCS as one
GeoTIFF per **5km National Grid square**, and the two are bit-identical, so a mirror
removes 2 round trips per tile — 11,686 of them for Kent:

```
python scripts/fetch-lidar.py --district Thanet          # 12 squares, 428 MB, 36s
postcode2wad "CT1 2EH" --size 800 --out out/tile.pk3     # uses it automatically
```

5000m is not a multiple of the tile size, so a map tile straddles up to four squares and
is mosaicked from windowed reads: **18ms median** against **2.3s** for the same square
over WCS, cold. `--lidar-source` is `auto` (mirror when every square a tile needs is
present, WCS otherwise), `local` (refuse the network), or `wcs`; `--lidar-dir` or
`$POSTCODE2WAD_LIDAR_DIR` moves the store. Falling back is shouted at stderr and counted,
because a county run that quietly went back to the network would look exactly like a
successful one until it had taken a week.

Two things to know before mirroring more:

- **The 1m last-return DSM has no bulk raster for the TR square** — all 72 tiles,
  covering Thanet, Canterbury and Dover, come back as metadata-only zips. It is an EA
  packaging defect, not missing survey: the WCS serves the same pixels. Building heights
  and trees therefore still come over the network east of the Medway.
- **A tile with no LIDAR at all is sea**, and is skipped and recorded rather than filled
  with flat ground. Over Kent's *land* the composite is gapless.

## Fixed technical decisions

| Decision | Choice |
| --- | --- |
| Engine target | GZDoom, UDMF map format |
| Scale | 32 map units ≈ 1 metre (player 56 units tall) |
| Assets | Freedoom IWAD — no `doom2.wad` dependency |
| Stack | Python 3.11+ — rasterio, shapely 2.x, pyproj, numpy, requests, pyosmium |
| Determinism | Generation is a pure function of a British National Grid tile ID |

## Development

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
python -m pyproj sync --file uk_os_OSTN15    # see below — do not skip
```

`scripts/playtest.ps1` launches a generated PK3 in GZDoom and screenshots it;
`scripts/lumps.py` checks that texture names actually exist in an IWAD.

GZDoom and Freedoom are not vendored. Drop a portable GZDoom in `tools/gzdoom/` and
Freedoom in `tools/freedoom/freedoom-0.13.0/`, or point the script at your own copies
with `-GzDoom` and `-Iwad`.

### The OSTN15 grid is not optional

Without it, pyproj silently falls back to a 7-parameter Helmert transform with ~2m of
error — enough to shift a building footprint off its own LIDAR pixels. With it, the
test postcode lands within 3cm of the ONS reference. `python -m pyproj sync --file
uk_os_OSTN15` installs it, and `TransformerGroup(...).unavailable_operations` should
come back empty.

## Data sources and attribution

- **Environment Agency** LIDAR composite 1m DTM/DSM — OGL v3.
  *© Environment Agency copyright and/or database right 2022. All rights reserved.*
- **postcodes.io** (ONS/OS/Royal Mail) — OGL v3.
  *Contains OS data © Crown copyright and database right.*
- **OpenStreetMap** — ODbL. *© OpenStreetMap contributors.* A generated level is a
  **Produced Work** under ODbL §4.5(b) — made to be played, not queried — so it needs
  attribution but **not** share-alike. Every PK3 ships an `ATTRIBUTION.txt` and shows a
  credit on the HUD, which the OSMF Attribution Guidelines accept for games. Because the
  pipeline also uses LIDAR, the Trivial Transformations exemption does not apply and
  §4.6 is engaged: the OSM extract behind any tile is reproducible from this source plus
  the tile ID, since generation is a pure function of it.
- **Freedoom** — modified BSD, credits retained.
- **GZDoom** — GPLv3 (any engine fork is published as source alongside binaries).

No id Software assets are ever redistributed.

## Licence

MIT — see [LICENSE](LICENSE). A future GZDoom fork lives in its own repo under GPLv3.
