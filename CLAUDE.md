# Working on postcode2wad

Read [`README.md`](README.md) for what this is and [`TODO.md`](TODO.md) for the
working list. This file is about *how to work on it safely*, and every line of
it was paid for.

## The one thing to internalise

**Every bug that has cost real time on this project rendered perfectly.**

A floor plane whose normal pointed down looked identical to one pointing up,
and the player even stood on it at the right height — but the physics treated
everything above it as solid and refused all horizontal movement. A leaked loop
variable flattened the entire map with no error anywhere. Plane normals
formatted with `"%.3f"` became `-0.000`, turning every slope into a level
triangle with a crack at each edge, which from a distance still looked like a
hillside. `String.ToInt()` auto-detects base, so `"08"` and `"09"` parsed as
octal, failed, and silently took the HUD and edge transitions with them on two
of nine tiles.

Screenshots cannot catch physics or precision bugs. Neither can "it looks
right". Measure the symptom on real generated output.

## The second thing: measure the right population

More costly than any single bug has been a **clean measurement of the wrong
thing**, which reads exactly like success. Real examples from one day:

- Ground-to-ground joins measured 0.00m and were reported as "spires fixed",
  while the actual defect was in *one-sided* lines, which have no second side
  and so could never appear in that number. 219 sky-high blades survived.
- Wall heights were measured across all materials, burying the real signal
  under fence and hedge tops that are *meant* to stand 1.8m proud.
- A LIDAR cache of 193 MB was divided by 9 tiles to get "22 MB/tile" — but the
  cache held 254 files from every build in the session. The estimate came out
  28x too high and sent an agent off to size a problem that did not exist.
- Plane steepness was used as a proxy for "spire". It is the wrong signal: a
  steep plane that meets its neighbours exactly is a *bank*, and banks are
  correct.

Before trusting a number, ask what it excludes and whether the defect could
hide there.

## Verification tools

Run these before claiming anything works:

```
.venv/Scripts/python.exe -m pytest -q                          # 89 tests
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe scripts/onesided.py out/REGION.pk3    # must be 0
.venv/Scripts/python.exe scripts/reachability.py "CT1 2EH"
.venv/Scripts/python.exe scripts/flatness.py out/REGION.pk3
.venv/Scripts/python.exe scripts/whystuck.py out/R.pk3 MAP06 12405 6941
```

`tests/test_smoke.py` asserts invariants on a *whole generated tile* from
cached real data — read its module docstring first. It is the gate before bulk
generation: a sign error found after 5,000 tiles is 5,000 tiles.

**`netevent walktest`** in the GZDoom console shoves the player four ways with
no human input and logs displacement. ~280 per direction is healthy ground,
single digits is a blockage. It is the only check that catches a *physics* bug.

Telemetry lands in the GZDoom log as `P2W TLM` / `P2W STUCK` / `P2W MARK`, with
lat/lng — so a player's "I got stuck here" becomes coordinates.

**Whole-region sweeps beat per-tile tests.** `test_no_interior_one_sided_lines`
passed for weeks because it ran on the one tile that happened to be clean,
while a neighbour in the same region had 134 offenders.

## If you add a test, mutation-test it

Reintroduce the bug it names, confirm the test fails, revert, confirm it
passes. Several tests here were asserting things that could never fail.

## Geometry invariants that must hold

- **A floor plane's `c` must be positive.** Normal points up. Negative renders
  identically and makes the map unwalkable.
- **Exact-corner plane fits are the entire continuity mechanism.** Three points
  define a plane; adjacent triangles share two, so their planes agree along the
  shared edge exactly. *Any* rule that adjusts a plane after fitting breaks this
  and steps the face's whole perimeter. Three separate "salvage" heuristics were
  tried and all three made things worse — the history is in the docstrings of
  `_floor_planes`. Do not add a fourth without measuring.
- **Rejection is contagious.** A face that refuses its fit takes a neighbour's
  plane, which then misses its own corners, and *those* joins step in turn.
- **A one-sided line away from the map edge is a wall from the ground to the
  sky** (~130m). There is no length at which an orphaned edge is acceptable.
- **`face_spec[i]`, not `owned[i][1]`.** `_absorb_slivers` may replace a face's
  spec; reading the original gave 128 water and building faces terrain planes.
- **Doom steps up 24 units max** (0.75m). Player radius 16, so a gap under 33
  units is impassable.

## Reading reachability

Unreachable ground is **not** a score to minimise. A courtyard ringed by
houses, a hedged garden and a walled yard are all *correctly* unreachable — the
largest pocket on Birchington is 4,000 m² enclosed by 57 building faces, which
is right. What matters is what *encloses* a pocket: real obstacles mean the map
works, open ground means a street is walled off by an artefact. Treating the
raw percentage as a target once led to changes that made the map worse while
making the number better.

## House style

- Comments explain **why**, especially where the obvious approach was tried and
  failed. The docstrings carrying that history are load-bearing; do not
  compress them away.
- Commit messages explain reasoning and cite measurements. See `git log`.
- Report honestly: if a number did not move, say so. If an approach made things
  worse, say so and revert it. Never state a figure you have not just measured,
  and label extrapolations as extrapolations.

## Where to pick up (as of 7 Aug 2026)

Fuller list in [`TODO.md`](TODO.md); this is the ordering and the reasoning.

**Kent is done and deployed.** All 13 districts, built 6–7 Aug in 16h58m:
6,888 tiles generated, **6,195 unique** (districts share border squares),
3,964.80 km², 54.0M sectors, 399k buildings, 4.77M trees, 8.9s per tile. Live
at doomearth.clawhangout.com — 13,817 objects, 4.63 GB in R2. The 13 district
PK3s are 4.48 GB for native GZDoom.

That closes the two items this section used to lead with, and the reasoning is
worth keeping because it is what made a county practical. The Thanet run was
**network-bound, not CPU-bound**: measured by building the same 16 tiles twice,
cold and with the LIDAR cache warm, the median tile went ~66s → ~5s, so ~55s of
every tile was the WCS DSM fetch. The EA's *bulk* 1m last-return DSM is a
metadata-only zip for every TR square, so eastern Kent was mirrored from the
OGC WCS in 5km blocks instead (bit-identical output — `docs/lidar-bulk.md`).
With the mirror in place the whole county ran off disk and 8.9s/tile is
geometry.

Geometry *is* superlinear in sector count (37 sectors → 1.4s, 32,290 → 75.5s),
so dense town tiles are genuinely CPU-heavy. It is the median tile that is not.

**Two measurement traps this run set, both now fixed — expect more of the same
shape.** Districts *share* their border squares, so summing per-district areas
double-counted 670 of them and overstated Kent by 444 km² (11%); the page's own
fallback had the same bug independently. And `build-webdemo.py` regenerates the
full 1.16 MB art set **per tile** (1.55s each) which `split_common` then
factors straight back out — ~3 hours of the run was generating byte-identical
art 6,888 times, single-threaded on 16 cores. An `lru_cache` on
`art.pk3_assets` should take site assembly from ~3h to minutes; not done,
because it was spotted mid-run and is not worth hot-swapping into a live job.
**Do this before the next county**, and diff a district byte-for-byte after.

**1. The beach is the choppiest surface on the map and nothing has touched it.**
Confirmed at district scale, so it is not a Birchington artefact: across all 200
Thanet tiles DMSAND is p50 4.6°, p95 29.1°, p99 55.9° on 21,758 joins — the
worst p50 *and* p95 of any surface. (Birchington alone read p50 6.9, p95 31.8.)
Sand is not an engineered surface so the centreline trick does not apply. This
is the one part of a player's seafront report that remains unanswered, and
Thanet is nothing but seafront. Measure with `scripts/flatness.py`.

The same run confirmed the road work generalises: DMTARMAC p50 0.1°, p95 3.6°
across 197,607 joins over 200 tiles, having only ever been measured on one
region before. Note every material has a max above 75° — a thin district-wide
tail of degenerate joins, consistent with the known sliver defect. It is a
tail, not a median; do not chase it with a rule that moves the median.

**2. Multiplayer** is untested and needs no code — see
[`docs/multiplayer-test.md`](docs/multiplayer-test.md), half an hour. The real
risk is desync, not load: netplay is deterministic lockstep and sloped floor
planes are the only floating-point geometry emitted, so `--no-slopes` is the
control experiment. Note all players share one level, so the region-of-tiles
design drags everyone along with whoever reaches a tile edge first.

**Known rough edges left standing** (all logged, none urgent): one 10cm blade on
MAP03 with a 9.9m step; one pavement junction stepping 28.8 units, past the
climb limit, and pre-existing; no automatic regression test for road flatness;
multipolygon relations dropped by both OSM readers (deliberate, so the two
agree). `covers()` reading the header bbox rather than Geofabrik's `.poly` is
fixed — a border tile went 2/22/4 features to 7/40/7, matching Overpass
exactly, with an interior control tile byte-identical.

## Environment

Windows. `.venv/Scripts/python.exe`, CLI at `.venv/Scripts/postcode2wad.exe`
(`python -m postcode2wad` does **not** work — no `__main__`). GZDoom in
`tools/gzdoom/`, Freedoom in `tools/freedoom/`, neither vendored.

`scripts/playtest.ps1 -Pk3 out/x.pk3 -Map MAP05 -Interactive` leaves the game
running for a human. Without `-Interactive` it screenshots and kills after 10s —
useful for checking a map loads, useless for handing to a player.

Do not skip `python -m pyproj sync --file uk_os_OSTN15`; without it coordinates
drift ~2m, enough to shift a footprint off its own LIDAR pixels.
