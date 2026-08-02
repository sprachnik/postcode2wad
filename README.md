# postcode2wad

Type any UK postcode, get a playable GZDoom level of that real place — generated from
DEFRA LIDAR terrain and real building footprints.

> "Arnis, but for Doom."

```
postcode2wad "CT7 0XX" --size 400 --out birchington.pk3
gzdoom -iwad freedoom2.wad -file birchington.pk3 +map MAP01
```

## Status

**M0 (geometry proof) and M1 (real data) are done and playable.** Give it a UK postcode
and it produces a PK3 that loads clean in GZDoom 4.14 with Freedoom Phase 2: real
building footprints at LIDAR-measured heights, roads with kerbs, and terrain contoured
from 1m Environment Agency LIDAR.

```
postcode2wad "CT1 2EH" --size 400 --out birchington.pk3 --preview plan.png
```
```
CT1 2EH — Birchington, Thanet  (51.36099, 1.26650)  OSGB 627500E 167500N
tile bng400-1574-423  (400m square)
74 buildings, 218 road pieces, 0 water, 48 terrain bands, terrain 13.0-22.0m
1504 sectors, 5800 linedefs
```

Next up is M2 (dressing: land cover, trees, street signs, geolocation HUD). See
[`docs/brief.md`](docs/brief.md) for the full brief, milestones and open research.

## How it works

1. **postcodes.io** turns a postcode into OSGB eastings/northings (no key needed).
2. That picks a **British National Grid tile** — generation is a pure function of the
   tile ID, so neighbouring tiles agree along their shared edge.
3. **Environment Agency LIDAR** 1m DTM and DSM arrive as GeoTIFF over WCS.
4. **Overpass** supplies buildings, roads and water in a single cached query per tile.
5. The DTM is quantised into *absolute* elevation bands and polygonised into contours.
6. Everything is thrown into a **planar arrangement** (`geometry.py`) — the step that
   turns overlapping real-world polygons into the topology Doom actually requires.
7. Out comes a UDMF `TEXTMAP`, wrapped in a WAD, zipped into a PK3.

GZDoom builds the BSP nodes itself at load, so there is no ZDBSP in the pipeline.

## Two constraints worth knowing

Both are load-bearing, and both are easy to get wrong:

- **Doom will not step up more than 24 map units** — 0.75m at our scale. Contour steps
  at or above that are invisible walls: the terrain looks fine and the player simply
  cannot climb it. Default step is 0.5m.
- **On a two-sided line the lower texture is drawn on the side facing the lower
  sector**, but it is the *higher* sector that is standing up. Paint it from the wrong
  side and every building wall gets the terrain's texture.

## Fixed technical decisions

| Decision | Choice |
| --- | --- |
| Engine target | GZDoom, UDMF map format |
| Scale | 32 map units ≈ 1 metre (player 56 units tall) |
| Assets | Freedoom IWAD — no `doom2.wad` dependency |
| Stack | Python 3.11+ — rasterio, shapely 2.x, pyproj, numpy, requests |
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
  *© Environment Agency copyright and/or database right. All rights reserved.*
- **postcodes.io** (ONS/OS/Royal Mail) — OGL v3.
  *Contains OS data © Crown copyright and database right.*
- **OpenStreetMap** — ODbL. *© OpenStreetMap contributors.* Note ODbL is share-alike
  and that obligation propagates to distributed derived geometry.
- **Freedoom** — modified BSD, credits retained.
- **GZDoom** — GPLv3 (any engine fork is published as source alongside binaries).

No id Software assets are ever redistributed.

## Licence

MIT — see [LICENSE](LICENSE). A future GZDoom fork lives in its own repo under GPLv3.
