# Prior art

**Re-verified 4 August 2026.** This supersedes the prior-art paragraph in commit
`3acc8fc` and the entry that stood in the old working list, both of which were
wrong in a way that mattered: they cleared the project to make claims it cannot make.

Three independent adversarial sweeps, each instructed to *refute* novelty rather than
confirm it. The correction that changed the conclusion came from reading a competitor's
source rather than its README.

## The precedent: geocraft (2015)

[github.com/cgutteridge/geocraft](https://github.com/cgutteridge/geocraft) — Chris
Gutteridge, created 12 June 2015, Perl, still pushed in 2024.

> Generate minecraft maps of real locations using UK LIDAR data (laser scans of terrain)
> and information from OpenStreetMap.

```
./generate-world --postcode SO171BJ --size 1000 Highfield
```

That is this project's CLI, country and data sources, eleven years earlier. It handles
OSGB eastings/northings and tracks DEFRA's `.asc`→GeoTIFF migration.

**It also measures building heights from LIDAR.** `lib/Elevation/UKDEFRA.pm` fetches both
models (`foreach my $model ( "DSM","DTM" )`), and `lib/Minecraft/Projection.pm:233` is:

```perl
$feature_height = int($dsm-$dtm);
```

A first sweep reported that geocraft used a flat block height and that measured heights
were therefore our moat. That reading came from a blog post and a config default; the
source says otherwise. Measured heights are **not** an innovation here.

## Claims that are dead

Do not make any of these. Each has prior art, most of it several times over.

| Claim | Killed by |
| --- | --- |
| Postcode as input is new | geocraft, `--postcode`, 2015 |
| UK LIDAR in a game world is new | geocraft; [The London Project](https://www.planetminecraft.com/project/true-london-1-1-scale-lidar-replica-of-central-london-uk/) |
| DSM−DTM measured heights are new | geocraft; [nadnerb, Sept 2015](https://blog.nadnerb.co.uk/?p=209) (EA LIDAR + OS footprints); *Information* 14(7):394, 2023 |
| First OSM game level | [Arnis](https://github.com/louis-e/arnis) (17.2k stars), Osmundi, Cesium OSM Buildings (2020) |
| Nobody put OSM into a retro FPS BSP format | [osm2vmf](https://github.com/lewa-j/osm2vmf) → Source VMF and Quake/GoldSrc `.map`, 2023 |
| First real place auto-converted to Doom | [Doomba](https://richwhitehouse.com/index.php?postid=72), Dec 2018 — Roomba SLAM scan of a house |
| First-mover on "Doom Earth" | see above; the brief's original framing was wrong |

`doom-osm-godmode`, cited confidently in `3acc8fc` and the old TODO entry as the single
direct hit, **could not be found by any sweep**. An agent told explicitly to hunt it ran
99 tool calls without surfacing it; GitHub repo searches for `openstreetmap doom wad` and
`doom osm map generator` return zero. Treat it as fabricated until someone produces the
package.

## What survives

Narrower and more technical than the old entry claimed, but checkable:

1. **One measured height per OSM footprint, not per raster column.** geocraft samples
   DSM−DTM at every block column independently, so its roofs inherit raster noise — its
   own README complains of this, and The London Project has the same artefact. Assigning
   a single measured height per footprint is what gives flat roofs and clean vertical
   walls. This is the same distinction against every rival, which is what makes it worth
   leading on.
2. **BSP sector geometry.** Nothing converts real geographic data into a sector
   partition. Every rival targets a voxel grid or a triangle mesh, both forgiving in ways
   a 1993 engine is not — no room-over-room, a 24-unit step limit, one texture pixel per
   map unit. The [OSM wiki's Games page](https://wiki.openstreetmap.org/wiki/Games) lists
   ~20 titles and contains no Doom, Quake or Source entry.
3. **Measured walkability.** No rival reports one-sided-line sweeps, reachability by
   enclosure, or a scripted walktest. The competition ships screenshots.

## Nearest neighbours worth knowing

- **MSFS 2024 + World Update XVII (UK & Ireland)** — real UK, LIDAR-derived terrain
  (50–100cm around London), and genuinely walkable on foot via Shift+C. Building heights
  are **inferred from 2D satellite imagery** by blackshark.ai, not measured. The proof is
  the Fawkner tower: in August 2020 an OSM contributor typo'd `building:levels=212` on a
  suburban house in Melbourne and MSFS rendered a 212-storey monolith
  ([The Register](https://www.theregister.com/2020/08/20/flight_simulator_bing_fawkner_tower/)).
  A pipeline that measures the roof off a DSM cannot make that error.
- **Cities: Skylines** — every OSM mod for it imports roads only. No footprints, no
  heights. Dismissable in a clause.
- **DD_Terrain** (jval1972) — greyscale heightmap → UDMF with sloped sectors. No geodata,
  no OSM, no buildings. Establishes heightmap→GZDoom-slopes as a solved step.
- **FlightGear osm2city / osm2xp / blosm / StreetMap for Unreal** — all OSM *tag* heights
  with probabilistic fallbacks. StreetMap's own README concedes heights "may be missing
  or incorrect in many cities".

## The gap that is still open

**Doomworld and the ZDoom forums are Cloudflare-blocked to every agent.** That corpus is
unsearched, and it is exactly where a 2010s "I converted OSM into a WAD" thread would
sit. A human posting there is a better prior-art probe than any search run so far, and it
is the last hole big enough to be embarrassing. Do it before announcing.

Also unclosed: whether any Build The Earth UK team fed EA LIDAR into
[terraplusplus](https://github.com/BuildTheEarth/terraplusplus)'s classified-LiDAR path,
and The London Project's page, which 403s.
