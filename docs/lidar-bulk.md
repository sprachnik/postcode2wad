# Bulk LIDAR for Kent — reconnaissance

**Date:** 2026-08-03 · **Status:** findings only, nothing implemented, nothing bulk-downloaded.

This answers "what does it cost to hold all of Kent's terrain on disk, and how do we get
it". Every number below is either measured here or quoted from an authoritative page;
anything extrapolated says so. Sample downloads totalled about 1.6 GB and were deleted.

---

## 0. First, a correction to the premise

The task came in with "~22 MB per 400m tile, so ~512 GB for Kent". That is wrong by about
28x, and it is worth killing before it drives a decision.

Measured, from this repo's own `cache/lidar` directory (254 cached WCS responses):

| statistic | value |
|---|---|
| files | 254 |
| total | 201.6 MB |
| median file | **0.64 MB** |
| distribution | 246 files @ 0.6 MB, 6 @ 4.2 MB, 2 @ 9.4 MB |

0.64 MB is exactly what a 400x400 float32 uncompressed GeoTIFF weighs
(400 x 400 x 4 = 640,000 bytes plus header). The 4.2 MB and 9.4 MB outliers are larger
bboxes, not larger tiles. So the WCS path costs 0.64 MB per 400m tile *per coverage*, or
1.28 MB for DTM+DSM.

The WCS cost is purely area-proportional — the tile size cancels out:

    3,739 km² x 1 sample/m² x 4 bytes x 2 coverages = 29.9 GB uncompressed

So the WCS-cache route for all of Kent is roughly **30 GB, not 512 GB**. Bulk download is
still the right move, but for round-trip count and robustness, not to escape a
half-terabyte. (5,843 x 800m tiles x 2 coverages = 11,686 WCS round trips; measured
latency for a single 5km WCS request was 3m02s, and for a 400m one, sub-second.)

---

## 1. What the Environment Agency actually publishes

### The portal

`https://environment.data.gov.uk/survey` — a Next.js SPA ("Defra Data Services
Platform"). The old `DefraDataDownload` ArcGIS GP job API is gone. The SPA drives a
private REST API, reverse-engineered from its JS chunks
(`tilesService` in `/_next/static/chunks/0dwns0.f40f-x.js`).

### Search endpoint — enumerate what exists over a polygon

```
POST https://environment.data.gov.uk/tiles/collections/survey/search?subscription-key=dspui
Content-Type: application/geo+json

{"type":"Polygon","coordinates":[[[lon,lat], ...]]}     # WGS84
```

Returns `{"count": N, "results": [{product, year, resolution, tile, label, uri}, ...]}`.
Both `subscription-key=dspui` and `subscription-key=public` work on *search*; only
`dspui` works on *download*. Without a key the API returns
`401 {"message":"'Ocp-Apim-Subscription-Key' header required"}`.

### Download endpoint — one zip per product/year/resolution/tile

```
GET https://environment.data.gov.uk/tiles/collections/survey/{product}/{year}/{res}/{tile}?subscription-key=dspui
```

e.g. `.../survey/lidar_composite_dtm/2022/1/TR1055?subscription-key=dspui`.
Response is `Transfer-Encoding: chunked` — **no `Content-Length`, no `Range` support, and
`HEAD` returns 405**. You cannot learn a file's size without downloading it. The zip is
assembled server-side on demand.

### Products offered (measured, from a search over the Kent bbox)

The full result set covered 284 grid tiles and 9,037 product/year/resolution/tile rows.
The DTM-relevant ones:

| product id | resolutions | vintage over Kent |
|---|---|---|
| `lidar_composite_dtm` | 1m, 2m | single 2022 composite, 277 of 284 bbox tiles |
| `lidar_composite_last_return_dsm` | 1m, 2m | single 2022 composite, 278 tiles |
| `lidar_composite_first_return_dsm` | 1m, 2m | single 2022 composite, 276 tiles |
| `national_lidar_programme_dtm` | 1m | fragmented by survey year: 2016 (19 tiles), 2017 (46), 2018 (147), 2019 (47), 2020 (66), 2021 (24) |
| `lidar_tiles_dtm` | 0.25m–2m | the pre-composite time-stamped archive, ~30 vintages 1998–2024 |

**The National LIDAR Programme is *not* the right product here.** It is the raw survey
programme, tiled per survey block and per flight year — you would have to mosaic six
different vintages to cover Kent, with seams and overlaps to resolve. The 2022 **LIDAR
Composite DTM** is the mosaic the EA already built from the NLP surveys plus the older
time-stamped archive. It is one vintage, one file per grid square, complete.

25cm and 50cm composite resolutions have been withdrawn; 1m and 2m are what exists.

### Tiling scheme (verified from the GeoTIFF geotransforms)

5 km x 5 km tiles on the OS National Grid. Tile id is `{100km square}{E km}{N km}` of the
SW corner, both two digits, both multiples of 5 — so `TR1055` is E 610000–615000,
N 155000–160000, and its human label is `TR15nw` (NW quadrant of the 10km square TR15).

Zip contents for `lidar_composite_dtm/2022/1/TR1055`:

```
TR15nw_DTM_1m.tfw                 89
TR15nw_DTM_1m.tif           70,550,944
TR15nw_DTM_1m.tif.xml           20,550
TR15nw_DTM_1m.tif.aux.xml        2,694
TR15nw_DTM_1m_Metadata.gpkg    167,936
```

GeoTIFF properties, read with rasterio:
5000 x 5000, `float32`, `EPSG:27700`, nodata `-3.4028235e+38`, **LZW compressed and
internally tiled** (so windowed reads are cheap — no need to decompress 25M pixels to get
an 800m square).

### The bulk files are bit-identical to the WCS

I pulled the same 5km window over WCS and compared against the bulk tile:

```
WCS GetCoverage E(610000,615000) N(155000,160000)  ->  104,858,819 bytes, 3m02s
bulk zip lidar_composite_dtm/2022/1/TR1055        ->   69,721,617 bytes, 29s
np.allclose(wcs, bulk) == True, max abs diff 0.0
```

So switching to local files changes nothing about generated output. It is 1.5x smaller on
the wire and 6x faster, because the WCS emits uncompressed float32 and the bulk tile is
LZW.

---

## 2. Download volume for Kent

### Kent's extent — verified, not assumed

ONS Counties and Unitary Authorities (Dec 2023) BFC boundaries, `Kent` (E10000016) union
`Medway` (E06000035) — the ceremonial county:

- area **3,739.2 km²** (matches the 3,736 km² in the brief)
- OSGB bounds **E 542131–640170, N 116569–179539**
- 100 km squares intersected: **TQ and TR only.** Confirmed. (TQ holds 2,362 km² of
  Kent, TR 1,377 km².)
- 800m tiles at that area: 3739.2 / 0.64 = **5,843** (brief said 5,837 — same thing)
- **191** 5 km grid tiles intersect Kent. Their combined footprint is 4,775 km²; the land
  inside them (union of all ONS county polygons, coastline-clipped) is 4,187 km².

### Measured tile sizes — 1m composite DTM

14 tiles downloaded, chosen for spatial spread across Kent plus four deliberately at the
edge of the county. Sizes are the bytes actually transferred.

| tile | MB | valid-data fraction |
|---|---|---|
| TQ4035 | 74.2 | 1.000 |
| TQ4545 | 66.3 | 1.000 |
| TQ6030 | 52.0 | 1.000 |
| TQ6050 | 45.9 | 1.000 |
| TQ7055 | 48.1 | 1.000 |
| TQ7060 | 61.2 | not measured |
| TQ8060 | 48.7 | 1.000 |
| TQ9530 | 66.1 | 1.000 |
| TR0535 | 43.4 | 1.000 |
| TR0565 | 36.4 | 0.904 (Whitstable/Swale coast) |
| TR1055 | 69.7 | 1.000 |
| TR1550 | 73.1 | 1.000 |
| TR3065 | 44.7 | 1.000 |
| TR4070 | 7.2 | 0.118 (mostly sea, off Thanet) |

mean **52.6 MB**, median 50.4, sd 17.9, range 7.2–74.2.
Normalised: **2.27 MB per km² of valid data** (median 2.26, range 1.61–2.97). The spread
tracks terrain roughness — flat marsh compresses, chalk downland does not.

### Kent total — this is an extrapolation

Three ways of extrapolating from the 14-tile sample (7.3% of the 191 tiles) to all of
Kent, deliberately computed differently so they can disagree:

| method | result |
|---|---|
| 191 tiles x mean measured tile (52.6 MB) | **10.06 GB** |
| land area in those tiles (4,187 km²) x 2.27 MB/km² | **9.51 GB** |
| full tile footprint (4,775 km²) x 2.27 MB/km² | **10.85 GB** |

**Call it 10 GB ± 1 GB for the 1m composite DTM over Kent.** The three methods agree to
within 13%, which is the honest precision here. What I did *not* do is download all 191
tiles, so this is a sample extrapolation, not a manifest total — and there is no manifest
to read, because the API exposes no file sizes at all (see the `HEAD`/`Range` note above).

### Other resolutions and products

**2m composite DTM**, 6 tiles measured (15.8–21.9 MB, mean 19.3 MB). Ratio to the
matching 1m tile is remarkably stable: 0.306, 0.361, 0.303, 0.301, 0.299, 0.353 — mean
**0.321**. Extrapolated Kent total: **~3.2 GB**.

**1m composite DSM**, 2 tiles measured: `TQ4545` last-return 67.5 MB (1.02x its DTM),
`TR1550` first-return 52.4 MB (0.72x its DTM). Too few to extrapolate tightly; expect the
DSM to cost roughly the same as the DTM, so **~8–11 GB**, i.e. DTM+DSM at 1m is
**~20 GB**.

### Wall-clock

The 12-tile batch: 606.2 MB in 222.3 s = **2.73 MB/s, 18.5 s per tile** — dominated by
server-side zip assembly, not bandwidth (a 7 MB tile still took 2.2 s; a 74 MB one took
39.8 s). Sequential Kent DTM ≈ **1 hour**. With four workers the scan of all 191 tiles ran
comfortably, so a parallel fetch of ~20–30 minutes looks achievable, but I did not measure
sustained parallel throughput.

---

## 3. Coverage and gaps

### DTM: complete over Kent

I probed all **191** Kent tiles for `lidar_composite_dtm/2022/1` by opening the download
stream and reading only the first zip local-file header (~128 bytes), then aborting.

    raster present (first entry .tfw/.tif): 191 / 191
    metadata-only zip:                        0
    404 or error after 3 retries:             0

Caveat: the probe proves a `.tfw` leads the archive, which in every one of the 14 tiles I
fully downloaded meant a real `.tif` followed. I did not verify the raster is non-degenerate
for the other 177.

Within-tile NODATA, measured on 12 tiles: **1.000 for all ten inland tiles**, 0.904 for a
coastal tile, 0.118 for an offshore one. The published claim is ~99% of England; over
Kent's land the composite appears to be gapless. **NODATA in Kent means sea or estuary,
essentially never missing land.**

### DSM: a real, systematic gap in the bulk product

Same probe over all 191 tiles for `lidar_composite_last_return_dsm/2022/1`:

    raster present:      119 / 191   (every TQ tile)
    metadata-only zip:    72 / 191   (every TR tile)

The 72 failures are exactly the TR-square tiles. A full download of one confirms it:
`lidar_composite_last_return_dsm/2022/1/TR1550` is an 11,953-byte zip containing only
`TR15se_LZ_DSM_1m_Metadata.gpkg` — no raster, no world file. The same URL intermittently
returns HTTP 404 instead.

This is a **packaging defect on the EA side, not missing survey data**. Proof: the WCS
serves that exact data fine —

```
WCS LZ_DSM_1m over E(615000,615400) N(150000,150400)
-> 400x400, valid fraction 1.000, elevations 130.1–158.6 m
```

And the defect is narrow. Spot-checked on TR1055, TR3050 and TQ7055, all of these are
present and correctly packaged:

- `lidar_composite_dtm` at 1m and 2m
- `lidar_composite_first_return_dsm` at 1m and 2m
- `lidar_composite_last_return_dsm` at **2m**

Only last-return DSM at 1m in the TR square is broken. Since the repo currently fetches
exactly that coverage (`...LZ_DSM_1m`), a naive bulk mirror would silently lose DSM for
the whole eastern half of Kent — Canterbury, Thanet, Dover, Folkestone, i.e. the demo
postcode's own half of the county. See the plan below for the three ways out.

---

## 4. Licence and attribution

Both dataset records state, verbatim:

- Licence: **Open Government Licence v3.0**,
  `https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/`
- Attribution statement: **"© Environment Agency copyright and/or database right 2022.
  All rights reserved."**
- Use constraints: "There are no public access constraints to this data."

Sources:
`https://environment.data.gov.uk/dataset/13787b9a-26a4-4775-8523-806d13af58fc` (DTM 1m)
and `https://environment.data.gov.uk/dataset/9ba4d5ac-d596-445a-9056-dae3ddec0178`
(DSM 1m) — the same two UUIDs already hardcoded as WCS coverage ids in
`src/postcode2wad/sources/lidar.py`.

### Does bulk use change anything?

**No.** OGL v3 grants copy, publish, distribute, adapt and commercial exploitation, with
attribution the only real condition. It draws no distinction between per-request and bulk
access, and imposes no share-alike. Mirroring 10 GB locally and shipping derived geometry
is squarely inside it. Nothing in the licence needs to change.

### Does the shipped text need changing?

`src/postcode2wad/attribution.py` currently says:

    (c) Environment Agency copyright and/or database right. All rights reserved.

The official statement is *"© Environment Agency copyright and/or database right **2022**.
All rights reserved."* — the year is part of it and the file drops it.
`src/postcode2wad/sources/lidar.py:30` has the year right; `attribution.py` does not.

Two small edits worth making, neither urgent, neither blocking:

1. Add `2022` to the EA line in `attribution.py` so it matches the published statement
   exactly. (`ATTRIBUTION` in `lidar.py` is already correct and is the string to copy.)
2. `attribution.py` describes the source as "1m composite DTM and DSM" — accurate today.
   If the build switches to 2m, or to first-return DSM, that line needs to follow. It is
   the kind of thing that silently goes stale.

No new attribution is required by moving to bulk. The two lines above are corrections to
what is already there, not consequences of the change.

---

## 5. Recommendation

### Product

**LIDAR Composite DTM, 2022, 1m** — `lidar_composite_dtm/2022/1`. One vintage, one file
per grid square, complete over Kent, bit-identical to what the WCS already returns. Not
the National LIDAR Programme: that would mean mosaicking six survey years.

For DSM, the current `lidar_composite_last_return_dsm/2022/1` **cannot be bulk-fetched for
TR**. Three options, in preference order:

> ### Resolved 5 Aug 2026: take option 1
>
> The A/B was run. `lidar_composite_first_return_dsm/2022/1` **does** have bulk
> rasters for TR, where the last-return product has none — probed TR3065
> (41.9 MB), TR3570 (19.6 MB) and TR2565 (41.6 MB), all with a real `.tif`,
> against metadata-only zips for the same three squares. So eastern Kent can be
> mirrored, and does not need one WCS round trip per tile.
>
> *(TR2565 first returned a 0.6 MB unreadable zip. That was a truncated
> download, not a defect — it retried clean at 41.6 MB. `fetch-lidar.py` reports
> `BAD ZIP` as a terminal outcome, so a 191-tile run will record transient
> truncations as data defects. Worth a retry there before the bulk fetch.)*
>
> **What changes.** Measured on Birchington (`bng800-787-211`), 500 buildings:
>
> | | last return | first return | difference |
> | --- | --- | --- | --- |
> | building height, mean | 6.36 m | 6.40 m | **+0.034 m** |
> | building height, median | 6.63 m | 6.66 m | +0.048 m |
> | uncapped tree count | 3,227 | 3,588 | **+11.2%** |
> | canopy, % of tile | 22.83 | 25.09 | +2.3 pts |
>
> Building heights are unchanged for practical purposes — 3 cm on the mean, and
> the whole distribution moves less than a tenth of Doom's 24-unit step. 50 of
> the 500 buildings (10%) differ by more than 0.5 m, worst 5.26 m, which is the
> expected artefact: first return sits on canopy overhanging a roof rather than
> passing through it. That is the residual risk and it is directional, so it is
> worth a look if a specific building ever reads too tall.
>
> The canopy difference is real but modest, and mostly invisible here: this tile
> saturates the 900-tree cap either way. Note the first measurement of it read
> "900 vs 900" and said nothing at all — that is the cap, not the signal, and it
> had to be re-measured with the cap lifted.
>
> Verdict: ~5 hours of per-tile WCS round trips over a full Kent build, traded
> for 3 cm of building height and 11% more trees. Take it.

1. **Switch to `lidar_composite_first_return_dsm/2022/1`.** Available for both squares.
   First return is the *upper* surface — for building heights (P90 of DSM−DTM inside a
   footprint) that is arguably more correct than last return anyway, though it will read
   tree canopy more aggressively. Needs a visual A/B on one tile before committing.
2. **Use `lidar_composite_last_return_dsm/2022/2` (2m)** for TR and 1m for TQ. Keeps the
   last-return semantics; costs a resolution split in the code.
3. **Keep DSM on the WCS** and bulk-mirror only the DTM. Simplest, and DSM is only sampled
   inside building footprints, so the request volume is not the problem the DTM is.

Do not resolve this from the desk — it is a one-tile experiment.

### Disk budget

| set | size | basis |
|---|---|---|
| Kent DTM 1m | **~10 GB** | extrapolated from 14 measured tiles |
| Kent DTM 2m | ~3.2 GB | extrapolated via measured 0.321 size ratio |
| Kent DTM + DSM, both 1m | ~20 GB | DSM from 2 measured tiles, weak |
| sidecar `.xml` / `.gpkg` | ~35 MB | 191 x ~180 KB, discardable |

Store the `.tif` and `.tfw` only; drop the XML and GPKG on unpack. The tif is already LZW
so unpacked disk ≈ download bytes — no decompression blow-up. **Budget 12 GB for
DTM-only, 25 GB if DSM comes along.** If that is uncomfortable, 2m DTM at ~3.2 GB is
worth a look: an 800m tile is 400x400 samples at 2m, and terrain is quantised into ~1m
contour bands anyway, so the visible loss may be nil. Test before deciding.

Suggested layout, keyed by grid tile so lookup is arithmetic rather than a manifest:

```
data/lidar/dtm_1m/TQ/TQ4545.tif
data/lidar/dtm_1m/TR/TR1055.tif
```

Add `data/lidar/` to `.gitignore` and ship a fetch script, not the data.

### How `sources/lidar.py` should change

The shape of the change, not the change itself:

1. **Add a local-first resolver.** A new function that maps an OSGB bbox to the set of 5km
   tile ids covering it (`{square}{E//1000:02d}{N//1000:02d}`, snapped to multiples of 5),
   checks for those files under a configurable `lidar_dir`, and returns them if all are
   present. Note 5000 is not a multiple of 800, so an 800m tile can straddle up to four
   5km tiles — mosaicking is required, not optional, and this is the main new code.
2. **Read windows, not whole tiles.** The tifs are internally tiled, so
   `rasterio.open(...).read(1, window=from_bounds(...))` pulls an 800m square without
   touching the other 24.4 million pixels. Keep returning the existing `Raster` dataclass
   so nothing downstream changes.
3. **Keep `_fetch` as the fallback.** Order should be: local file → existing WCS+cache →
   error. Same `Raster` out of all paths. The WCS path is proven bit-identical, so falling
   back cannot change output — worth a note in the docstring so nobody assumes a seam.
4. **Make the fallback loud.** Log which path served each request, and count fallbacks.
   During a 5,843-tile Kent run, silently falling back to WCS for every tile would be
   indistinguishable from success until it took a week.
5. **Do not soften the NODATA error.** `fill_holes` currently raises on an all-NODATA
   raster and that is right. Measured: NODATA over Kent's *land* is ~0. The correct
   fallback for an all-NODATA 800m tile is to **skip the tile as sea**, recorded as a
   deliberate outcome, not to fill it with flat ground and not to crash the run. A
   half-NODATA coastal tile should keep the current median fill.
6. **Fetch script, separate module.** `scripts/fetch-lidar.py`, taking a bbox or a county
   name, enumerating tiles, POSTing the search endpoint to confirm availability, then
   downloading with a few workers, resume-on-restart, and a check that each zip contains a
   `.tif` and not just a `.gpkg` — that check is not hypothetical, it is 72 of 191 tiles
   for the DSM today.

### One caveat to carry forward

`subscription-key=dspui` is the web UI's own key, hardcoded in its JavaScript. This API is
**not published on the Defra API portal** (`environment.data.gov.uk/apiportal` lists 11
APIs; tiles/survey download is not among them). It is undocumented and unversioned, and it
can be changed or rate-limited without notice. That is an argument for fetching Kent once
into a local store — which is what this proposes — rather than depending on the endpoint at
build time. The WCS, by contrast, is a documented standard OGC service, which is why it
should stay as the fallback rather than being ripped out.
