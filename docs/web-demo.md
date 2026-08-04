# The browser demo — how it works and what it cost to learn

Live at **https://doomearth.clawhangout.com** (Cloudflare R2, custom domain).
Built by `scripts/build-webdemo.py` from district artifacts; page templates in
`scripts/webdemo/`. This file records the engineering findings, because every
one of them was paid for on 2026-08-04.

## The engine

**[uzdoom-wasm](https://github.com/abootnet/uzdoom-wasm)** — an Emscripten port
of [UZDoom](https://github.com/UZDoom/UZDoom) (the GZDoom fork from the
Nov 2025 team split), GPLv3, one maintainer, 3 stars. Verified by symbol
inspection of the shipped binary before adoption: UDMF parsing, plane-equation
slopes (`floorplane_a..d`), ZScript to 4.15.1 (we pin 4.10), `netevent`, PK3
loading, WebGL2 via GLES. The ZScript JIT is x86-64-only and off in wasm;
scripts run interpreted.

Proven in browser: a 26k-sector tile, the 140k-sector Margate 3×3 (inside the
build's 2 GB wasm memory ceiling), tile-edge transitions, the HUD with live
lat/lng, telemetry, and ~7 minutes of continuous play with zero stuck events.

## Four failures, all presenting as "black screen / wrong game"

Each looked like the map was broken. None was the map.

1. **The upstream site's "bundled Freedoom" button loads `freedoom1.wad`** —
   Doom 1 format, episodic `E1M1` naming. Our PK3s ship `MAP01`, unreachable
   from a Doom 1 menu, so the engine silently plays stock Freedoom instead.
   The IWAD must be **freedoom2**.
2. **Python's `http.server` ignores `Range`**, answering a 1 KB range request
   with 200 and the whole 57 MB `uzdoom.data`. Emscripten's loader fails in a
   retry loop whose error names the file but not the cause.
3. **Emscripten's own streaming fetch of `uzdoom.data` is fragile**; a plain
   `arrayBuffer()` fetch of the same URL always worked. The launcher fetches
   the package itself and hands it over via `Module.getPreloadedPackage`,
   which is checked before the engine's fetch path runs.
4. **The build bakes in `noInitialRun`** (`shouldRunNow=false`):
   `Module.arguments` is ignored and the runtime initializes then does
   *nothing* until an explicit `Module.callMain(argv)`. Symptom: black canvas,
   zero CPU, no errors, "Running..." as the last log line. Diagnosed by
   measuring CPU across all browser processes — idle meant blocked, not slow,
   which killed the "it's building BSP nodes" theory in one measurement.

Also: browsers eat the backquote key, so the in-game console is unreachable —
anything console-shaped goes in as `+cmd` argv via URL params (`?fps=1`,
`?scale=0.5`, `?cmd=netevent walktest`). And the engine must boot behind a
user gesture (the performance-mode buttons are that gesture).

## Performance: measured, CPU-bound

On the Margate 3×3 in Chrome (Ryzen 3800X / RTX 3090): **9 FPS at full
resolution, 10 FPS at half** (`vid_scalefactor 0.5`). Fill rate is irrelevant;
the wasm main thread is saturated submitting draws for open terrain that
defeats Doom's wall-based BSP occlusion. Consequences:

- **The web demo ships single tiles** (~8k median sectors — "not amazing but
  not terrible" on the same machine), not region packs. Regions stay as
  downloads for native play.
- **Sector decimation (TODO #6) is promoted** from polish to the path to
  browser-playable regions. Fewer sectors also helps native FPS, build time
  and file size.
- The performance modes (Quality/Balanced/Performance = render scale) exist
  for weaker GPUs, where fill *may* bind before the CPU does. On a machine
  that is CPU-bound they will not help, and that is expected.
- Native-GZDoom FPS on the same map is unmeasured; without it the wasm tax
  cannot be split from the sector cost. Measure before optimising either.

## The site layout

```
site/
  index.html          coverage map: district tabs, minimap grid, click → play
  play.html           launcher: fetch, mode chooser, callMain
  _headers            COOP/COEP for Netlify (R2 uses Transform Rules instead)
  serve.py            local preview (http.server + Range + the two headers)
  districts.json      accumulates districts across build runs
  engine/             uzdoom-wasm + freedoom2.wad (~108 MB, cached once)
  <district>/
    manifest.json     tiles: id, ix/iy, title, sectors, buildings, trees, bytes
    common.pk3        every member byte-identical across all tiles (~1.1 MB)
    tiles/*.pk3       one standalone map each (0.5–2.4 MB)
    minimaps/*.png    grid thumbnails
```

**`common.pk3`** exists because a standalone tile is ~1.2 MB of generated art
plus a map often smaller than the art: 200 tiles shipped 342 MB, of which
~230 MB was the same art 200 times. Membership is empirical — identical
(size, CRC) in every tile — so per-tile members (TEXTMAP, MAPINFO, minimap,
ATTRIBUTION.txt, the ZScript with baked coordinates) exclude themselves. The
launcher loads it before the tile; later files win. Validated in native
GZDoom: `common.pk3 + tile.pk3` loads MAP01 with zero missing-lump lines.

**Districts accumulate.** Each `build-webdemo.py` run replaces one district
under `site/<slug>/` and updates `districts.json`; the index grows tabs when
there is more than one. The rest of Kent is `build-district.py` then
`build-webdemo.py` per district, plus an rclone sync.

## Hosting (verified 2026-08-04, official docs)

| | per-file | headers | bandwidth |
| --- | --- | --- | --- |
| **Cloudflare R2 + custom domain** ✅ | 5 TiB | Transform Rules | **egress free, always** |
| Netlify free | none documented | `_headers` ✓ | ~15 GB/mo then **hard suspension** (~140 visitors at our 110 MB first load) |
| Cloudflare Pages | **25 MiB — disqualified** (uzdoom.data is 57.6) | ✓ | free |
| GitHub Pages | 100 MiB | **none** — needs coi-serviceworker hack | 100 GB soft |

R2 traffic economics: storage 0.25 GB of a free 10; egress free; reads
$0.36/million past 10M/month (~50k visitors, mostly the 200 minimap fetches
per index view). The realistic viral-month bill is single-digit dollars, and a
cache rule on the zone would cut reads further. The `r2.dev` preview URL is
rate-limited and cannot take the header rules — the custom domain is
mandatory, and the two Transform Rules (COOP/COEP headers; `/` →
`/index.html`, which R2 does not do itself) live on the *zone*, not the
bucket.

## Licensing

The site distributes GPLv3 binaries (UZDoom via uzdoom-wasm) — the footer
links both source repos, which is the practical GPL§6 position for an
unmodified binary; if we ever patch the engine, the fork gets published.
Freedoom is modified BSD (credited). Every tile PK3 carries its own
ATTRIBUTION.txt (OSM ODbL, EA OGL, OS/ONS OGL) plus the HUD credit.
