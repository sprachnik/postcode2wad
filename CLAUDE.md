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
.venv/Scripts/python.exe -m pytest -q                          # 69 tests
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

## Where to pick up (as of 3 Aug 2026)

Fuller list in [`TODO.md`](TODO.md); this is the ordering and the reasoning.

**1. Mirror the 1m DSM for the TR square — this is what actually blocks
eastern Kent, and it is not the harness.**

`scripts/build-district.py` exists and works: Thanet, 200 tiles at 800m, 28.5
minutes, 0 failures, 111 MB, `M0001`..`M0200`, 0 one-sided lines across all 200
maps. Subprocess per tile, resumable from artifacts on disk, progress and
failure logs.

But **the Thanet run was network-bound, not CPU-bound**, and the "~70s of
geometry per tile" figure this file used to carry was wrong. Measured by
building the same 16 tiles twice — density-spread from 37 to 32,290 sectors,
same 8 workers, once cold and once with the LIDAR cache warm:

| | cold | warm |
| --- | --- | --- |
| median tile | ~66s | ~5s |
| mean | 71s | 11.4s |
| densest (32,290 sectors) | 139s | 75.5s |

So ~55s of every 66s tile was the WCS DSM fetch. One serial WCS request is
2-3s, but eight concurrent ones queue to ~8s each, which is why 200 tiles took
28.5 minutes (200 x 8s ≈ 27) while the geometry in them was about 5. **Adding
cores will not speed up TR.** The EA's bulk DSM is a metadata-only zip for
every TR tile — Thanet, Canterbury, Dover — so those three districts fetch DSM
over the network one round trip per tile. `docs/lidar-bulk.md` §5 has the three
ways out; mirroring the DSM from the WCS in 5km blocks preserves output
exactly.

Geometry *is* superlinear in sector count (37 sectors → 1.4s, 32,290 → 75.5s),
so dense town tiles are genuinely CPU-heavy. It is the median tile that is not.

TQ has a working bulk DSM, so western Kent should be CPU-bound and much
faster — but that is an inference from the packaging defect's extent, not a
measurement. Measure a TQ district before sizing Kent.

**2. Download the rest of Kent's LIDAR** (~10 GB; only Thanet's 12 squares and
2 TQ squares are mirrored). Left as a human decision deliberately.

**3. The beach is the choppiest surface on the map and nothing has touched it.**
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

**4. Multiplayer** is untested and needs no code — see
[`docs/multiplayer-test.md`](docs/multiplayer-test.md), half an hour. The real
risk is desync, not load: netplay is deterministic lockstep and sloped floor
planes are the only floating-point geometry emitted, so `--no-slopes` is the
control experiment. Note all players share one level, so the region-of-tiles
design drags everyone along with whoever reaches a tile edge first.

**Known rough edges left standing** (all logged, none urgent): one 10cm blade on
MAP03 with a 9.9m step; one pavement junction stepping 28.8 units, past the
climb limit, and pre-existing; no automatic regression test for road flatness;
multipolygon relations dropped by both OSM readers (deliberate, so the two
agree); `covers()` uses the extract's header bbox not Geofabrik's `.poly`, so a
tile at the county edge generates empty rather than falling back.

## Environment

Windows. `.venv/Scripts/python.exe`, CLI at `.venv/Scripts/postcode2wad.exe`
(`python -m postcode2wad` does **not** work — no `__main__`). GZDoom in
`tools/gzdoom/`, Freedoom in `tools/freedoom/`, neither vendored.

`scripts/playtest.ps1 -Pk3 out/x.pk3 -Map MAP05 -Interactive` leaves the game
running for a human. Without `-Interactive` it screenshots and kills after 10s —
useful for checking a map loads, useless for handing to a player.

Do not skip `python -m pyproj sync --file uk_os_OSTN15`; without it coordinates
drift ~2m, enough to shift a footprint off its own LIDAR pixels.
