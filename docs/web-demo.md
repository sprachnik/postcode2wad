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

## The second wave: findings from the first real player

- **Pointer lock must be requested, per gesture, by us.** Without it the OS
  cursor stays live and mouse-look stops dead at the window edge. Click the
  canvas captures; Esc releases (browser-enforced); click recaptures. The
  upstream loader has the same handler with a comment describing the exact
  symptom.
- **The engine's fullscreen shows only the canvas element**, so every DOM
  overlay — banners, buttons, hints — is invisible by construction. Anything
  that must be visible in fullscreen has to be drawn by the engine itself
  (i.e. the ZScript HUD), or not matter.
- **Quitting from the engine menu ends the wasm runtime**; without a
  `Module.onExit` hook the page is just dead. It now shows
  back-to-map / play-again, and `onAbort` routes to the same overlay.
- **The console echo is hidden** (`+con_notifylines 0`): the HUD's telemetry
  printed to the top of the screen once a second. Display only — the lines
  still reach stdout.
- **Stale pages look like broken deployments.** `play.html` changed four
  times on launch day, and twice the report of a bug was a browser cache
  serving the previous version. Pages/manifests cache 15 min at the edge,
  5 min in browsers, precisely so this window stays small.

## Crossing tile edges

Single-tile PK3s cannot transition in-engine — the neighbour is a different
file, and the engine cannot be handed a new file mid-run. (This confused even
us on launch day: transitions "worked locally" because the local test was
`margate.pk3`, a 9-map region pack. Same engine, different content.)

The replacement uses a channel that already existed: the HUD prints
`P2W TLM map=… x=… y=… z=… ang=…` telemetry to the console, and the launcher
*is* the console. It watches the stream against the district manifest: within
25m of an edge whose neighbour exists, a banner names what's ahead; within 4m
the page navigates to the neighbour itself. Armed only after the player has
been seen clear of every edge once, so arriving on a tile whose spawn sits
near a border cannot ping-pong straight back. Telemetry built for "I got
stuck here" bug reports became the navigation system.

**And the position comes with you** (2026-08-04, second pass). It did not at
first — you arrived at the neighbour's spawn point, which is a different
street in a different part of the tile and reads as being teleported rather
than as walking. The in-engine region transition has always carried the
player across, keeping the coordinate parallel to the seam and landing 6m
inside the far edge; what it keeps it in are *fields on the handler*, and a
page navigation ends the process those fields live in.

So the same four numbers go out through the command line instead. The
launcher appends them to the neighbour's URL, and the next page passes them
as `+set p2w_entry_x …` (declared in a `CVARINFO` lump, read once in
`WorldLoaded`, latched so a later respawn cannot re-trigger it). From there
it is the *existing* arrival code — same mirroring, same `TryPlace` floor and
occupancy checks, same 16m spiral when you would have landed inside a tree.

Two things are deliberate. The launcher sends the raw departure position and
a direction of travel, **not** a finished arrival point: the mirroring rule
then exists once, in the language that owns `REENTRY`, instead of once in
each and free to drift. And `P2W ARRIVE` is printed after placement rather
than before, because the spiral can move you 16m and the fallback ignores
both checks — the requested point is not evidence that anything worked.

Measured in native GZDoom (the cvar path is core engine, not wasm):
`+set p2w_entry_x 12345 +set p2w_entry_y 9000 +set p2w_entry_z 500 +set
p2w_entry_ang 270 +set p2w_entry_dir 1` on `bng800-782-208` logged
`P2W ARRIVE x=12345 y=192 z=511 ang=270` — the along-seam coordinate kept,
`y` mirrored to `REENTRY` inside the south edge, the floor found 11 units
from the departure height, facing unchanged. All four directions were then
measured separately, because the bug below only showed in two of them.

### A negative value does not survive GZDoom's command line

This cost an hour and produced a *correct-looking* crossing in half the
compass. GZDoom splits its command line at every argument beginning with `-`
or `+`, so a negative **value** is read as the start of a new parameter:
`+set p2w_entry_z -400` is a `set` with no value, followed by an unknown
`-400`. The cvar keeps its default and **nothing is logged** — no warning, no
error, no clue. Measured directly: that line left the cvar at `-1`, and
`+set p2w_entry_dy -1` left it at `0`.

The consequence was not a failure, which is the dangerous part. Crossing
*north* or *east* sends a positive `dy`/`dx` and worked perfectly; crossing
*south* or *west* sends a negative one, which vanished, so the mirroring
never happened and the player arrived at the raw departure coordinate — on
the same side of the new tile as the seam they had just left, one step from
crossing straight back. Two of four directions right is exactly the kind of
half-working that reads as working.

So nothing sent that way may be negative:

- **the crossing direction is a compass index** (1 = north … 4 = west), not
  the `(dx, dy)` pair the in-engine transition uses;
- **z is biased by 65536** on the way out and unbiased on the way in. z is
  the one entry value that legitimately goes below zero, because floors here
  are at absolute ordnance-datum height.

Both ends of both rules are pinned by `tests/test_webdemo.py`, which is the
only thing standing between this and a silent regression: the two languages
cannot check each other, and the failure has no symptom at the point of the
mistake.

## Phones

The engine reads keyboard and mouse. SDL's touch handlers are compiled in —
they are visible in `uzdoom.js` — but a desktop Doom has nothing bound to a
finger, and the first phone report was that the tile boots and then cannot be
played. The launcher therefore synthesises the input the engine is already
listening for: `KeyboardEvent`s with the right `code` (SDL maps a key by
`code`, not `keyCode`) for a floating left-hand stick, and `mousemove` for a
right-hand look drag. All of it on GZDoom's *default* bindings, because the
console is unreachable in a browser and nothing can be rebound.

- `movementX` in a `MouseEvent` constructor is a legacy extension rather than
  part of `MouseEventInit`, so it is feature-detected; where it is missing,
  looking falls back to holding the turn keys. Both `movementX` and a moving
  `clientX` are sent, because without a pointer lock — and a phone cannot
  grant one — SDL derives motion from the absolute position instead.
- `freelook 1` is forced on touch: with it off, vertical mouse motion is
  read as walking, so a drag to look up would march you down the street.
- `play.html` had **no viewport meta tag at all**, so a phone laid it out at
  980px and scaled it down. That alone made the canvas a postage stamp.
- Detection is coarse-pointer **and** a touch digitiser: a touchscreen laptop
  has the second without being a phone, and it still has a real mouse.

Untested on a real handset at the time of writing — this is the one part of
the change with no measurement behind it. The performance question is open
too, and pessimistic: a tile is CPU-bound in the wasm main thread on a
desktop, so the chooser recommends PERFORMANCE on touch and does not decide.

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
`build-webdemo.py` per district, then `deploy-webdemo.py`.

## Hosting (verified 2026-08-04, official docs)

| | per-file | headers | bandwidth |
| --- | --- | --- | --- |
| **Cloudflare R2 + custom domain** ✅ | 5 TiB | Transform Rules | **egress free, always** |
| Netlify free | none documented | `_headers` ✓ | ~15 GB/mo then **hard suspension** (~140 visitors at our 110 MB first load) |
| Cloudflare Pages | **25 MiB — disqualified** (uzdoom.data is 57.6) | ✓ | free |
| GitHub Pages | 100 MiB | **none** — needs coi-serviceworker hack | 100 GB soft |

R2 traffic economics: storage 0.25 GB of a free 10; egress free; reads
$0.36/million past 10M/month (~47k visitors uncached, mostly the 200 minimap
fetches per index view). The realistic viral-month bill is single-digit
dollars. The `r2.dev` preview URL is rate-limited and cannot take the header
rules — the custom domain is mandatory, and all the rules live on the *zone*,
not the bucket:

- **Transform Rules** ×2: the COOP/COEP/CORP headers, and `/` →
  `/index.html` (R2 has no index documents).
- **Cache Rules** ×2, order matters — *last matching rule wins*, so the
  host-wide short rule (15 min edge / 5 min browser, so deploys land) comes
  first and the long rule for `/engine/`, `/tiles/`, `/minimaps/`,
  `common.pk3` (30 days / 1 day) comes second. Verified `cf-cache-status:
  HIT` on the 57 MB data package: repeat traffic never touches the bucket,
  so no meter on the site scales with popularity.

Deploying is `python scripts/build-webdemo.py …` then `python
scripts/deploy-webdemo.py` (rclone remote configured against the bucket's S3
endpoint with an R2 API token scoped to the one bucket).

**It is not a bare `rclone sync` any more, and a bare sync would silently make
the site slower.** Measured 2026-08-05: `uzdoom.data` (57.6 MB) and
`freedoom2.wad` (28.8 MB) were arriving *uncompressed* — 86.4 MB of a ~102 MB
cold load — because Cloudflare only auto-compresses a fixed content-type list.
`.wasm` and `.js` are on it and arrive zstd'd; `application/octet-stream` is
not. The PK3s are zip archives already (gzip takes `common.pk3` to 99%), so
those two raw containers were the whole of the loss. Brotli q11 takes them to
23.9 MB, cutting the cold load to ~40 MB.

They are therefore stored *pre-compressed*, under the uncompressed key, with
`Content-Encoding: br` set on the object — R2 does no content negotiation of
its own. `build-webdemo.py` writes the `.br` sidecars beside the originals
(the originals stay: `serve.py` and `--engine-dir site/engine` both want real
bytes) and `deploy-webdemo.py` excludes those two keys from the sync and
maintains them with `copyto`. A plain sync would undo it twice over — upload
the raw 57 MB over the compressed object, *and* upload the `.br` as a second
key nobody requests.

Safe because `grabRaw` in `play.html` is `fetch()` + `arrayBuffer()`, which
decodes transparently, and the progress bar counts files rather than bytes.
Every browser that can run this (SharedArrayBuffer + WebGL2) has had brotli
for years. Verify after a deploy — the failure mode is a 200 with no
`Content-Encoding`, which looks perfectly fine and is 62 MB slower:

```
curl -sSI -H 'Accept-Encoding: br' \
  https://doomearth.clawhangout.com/engine/uzdoom.data | grep -i content-
```

**And then bump `PK3_V` in `play.html`.** The long cache rule is what makes
repeat traffic free, and it is also a 30-day trap: tile PK3s and
`common.pk3` are cached 30 days at the edge under URLs a rebuild *reuses*. A
deploy that changes tile contents is therefore invisible to any edge holding
the old copy — for a month — while `rclone check` reports the bucket as
perfectly in sync, because the bucket is. The launcher now appends
`?v=<PK3_V>` to those two fetches, and `play.html` is on the short rule, so
bumping the constant is a 15-minute rollout with no cache purge needed.
The same trap in miniature caught the launcher itself twice on launch day:
"the edge transitions have stopped working" was, both times, a browser or an
edge holding the previous `play.html`. Check with a cache-buster
(`curl "…/play.html?x=$RANDOM"`) before believing any report about a
deployed change — including your own.

## Licensing

The site distributes GPLv3 binaries (UZDoom via uzdoom-wasm) — the footer
links both source repos, which is the practical GPL§6 position for an
unmodified binary; if we ever patch the engine, the fork gets published.
Freedoom is modified BSD (credited). Every tile PK3 carries its own
ATTRIBUTION.txt (OSM ODbL, EA OGL, OS/ONS OGL) plus the HUD credit.
