# postcode2wad

Type any UK postcode, get a playable GZDoom level of that real place — generated from
DEFRA LIDAR terrain and real building footprints.

> "Arnis, but for Doom."

```
postcode2wad "CT7 0XX" --size 400 --out birchington.pk3
gzdoom -iwad freedoom2.wad -file birchington.pk3 +map MAP01
```

## Status

Greenfield. Working towards **M0 — geometry proof**: a synthetic heightmap plus two
hand-defined building polygons emitted as a valid UDMF `TEXTMAP` in a PK3 that loads in
GZDoom and can be walked around. No real data yet — this de-risks everything else.

See [`docs/brief.md`](docs/brief.md) for the full brief, milestones, pipeline
architecture and open research areas.

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
```

## Data sources and attribution

Not yet wired up, but the release checklist requires:

- DEFRA National LIDAR Programme / Ordnance Survey — Open Government Licence
- OpenStreetMap — ODbL, "© OpenStreetMap contributors"
- Freedoom — BSD-style, credits retained
- GZDoom — GPLv3 (any engine fork is published as source alongside binaries)

No id Software assets are ever redistributed.

## Licence

MIT — see [LICENSE](LICENSE). A future GZDoom fork lives in its own repo under GPLv3.
