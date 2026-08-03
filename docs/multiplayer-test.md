# Multiplayer test plan

Does a generated map work in GZDoom netplay? Nothing here needs a code change —
the maps are ordinary UDMF — so this is a half-hour experiment that tells you
whether multiplayer is worth pursuing before any effort goes into it.

Run it on one machine with two instances first. That rules out the map before
latency, routers or a second PC can confuse the result.

## What could actually break

In rough order of likelihood, so stop early if one fails:

1. **Load.** 26,449 sectors in an 800m tile. Single-player loads it fine; netplay
   also has to build nodes identically on both ends before play starts.
2. **Desync.** Doom netplay is deterministic lockstep: both machines simulate the
   whole world and only exchange inputs. Sloped floor planes are floating-point,
   and if the two ends ever disagree by one bit the game desyncs. This is the
   real risk and the reason to test at all.
3. **The shared level.** All players are always in the same level. Our region
   design changes level at each tile edge, so one player walking off the north
   edge takes everyone with them.
4. **ZScript in netplay.** The HUD, tile transitions and telemetry are all
   ZScript. `RenderOverlay` is per-client so the minimap should be fine, but the
   transition handler runs in play scope and must reach the same decision on
   every machine.

## Setup

Two console windows, same machine. Host first:

```
tools\gzdoom\gzdoom.exe -iwad tools\freedoom\freedoom-0.13.0\freedoom2.wad ^
  -file out\tile800.pk3 -host 2 -netmode 1 +map MAP01 ^
  -width 1280 -height 720 +vid_fullscreen 0 +logfile out\mp-host.log
```

Then the client:

```
tools\gzdoom\gzdoom.exe -iwad tools\freedoom\freedoom-0.13.0\freedoom2.wad ^
  -file out\tile800.pk3 -join 127.0.0.1 ^
  -width 1280 -height 720 +vid_fullscreen 0 +logfile out\mp-client.log
```

`-netmode 1` is packet-server (host relays); `-netmode 0` is peer-to-peer. Try 1
first — it is more forgiving. Both sides must load byte-identical PK3s.

Start on `out\tile800.pk3` — a single tile, no transitions, so test 1 and 2 are
isolated from test 3. Only move to `out\birchington-area.pk3` once a single tile
is known good.

## What to check

| # | Check | Pass looks like |
|---|---|---|
| 1 | Both instances reach the map | Player 2 spawns; neither hangs on "Waiting for players" |
| 2 | Walk apart for 2–3 minutes | No "desync" or "consistency failure" in either log |
| 3 | Walk a slope, both players | Both end at the same height; neither is pushed through terrain |
| 4 | HUD on both | Minimap, compass and lat/lng draw for each player independently |
| 5 | Watch each other | Player 2's position on player 1's screen matches where they are |

Then on the region PK3:

| # | Check | Expected |
|---|---|---|
| 6 | One player walks off a tile edge | **Both** change level. This is engine behaviour, not a bug |
| 7 | Both players near an edge at once | Should be fine, but two simultaneous `ChangeLevel` calls are worth watching |

## Reading the result

Search both logs for `desync`, `consistency`, `Different level`. A clean
two-player session with no consistency failures means the geometry is
netplay-safe and the only open question is design, not correctness.

If it desyncs on a slope, that points at the floor planes — they are the only
floating-point geometry we emit, and `--no-slopes` is the control experiment. If
it desyncs without slopes too, the cause is elsewhere.

## What this does not tell you

- **Internet play.** Lockstep means everyone runs at the slowest player's
  latency. LAN-clean says nothing about a 60ms link.
- **More than 2 players.** GZDoom supports 8; bandwidth and desync risk both
  grow.
- **Zandronum.** The port built for proper client/server play does not support
  ZScript at all, so the HUD, transitions and telemetry would need rewriting in
  ACS/SBARINFO. Whether its UDMF support covers our sloped floor planes is
  untested here and should not be assumed.

## If it works, the design question

A shared level means the region-of-tiles design does not suit multiplayer.
Options, cheapest first:

1. **One tile, no transitions.** An 800m or 1600m map is a good multiplayer
   space on its own, and costs only a build flag.
2. **Bigger tiles.** Fewer, larger maps so a session fits in one level. 800m is
   26k sectors, so 1600m is roughly 100k — load time and framerate unmeasured.
3. **Line portals** stitching neighbours seamlessly inside one map. The right
   answer and much the most work: the region becomes one enormous map.
