"""Assemble a static web demo from district build artifacts.

    python scripts/build-webdemo.py --work work/thanet-800m --out site \
        --engine-dir <dir with uzdoom.js/wasm/data + freedoom2.wad>

The output is a directory of nothing but static files: the uzdoom-wasm engine,
one *standalone single-tile PK3 per built tile*, each tile's minimap as a
plain PNG, per-district manifests, and two pages — a coverage map (click a
tile, pick a performance mode, spawn on it) and a launcher. Upload the
directory to any static host that can set two response headers; `_headers`
carries them for Netlify.

Districts accumulate: each run adds (or replaces) one district under
`site/<slug>/` and updates `districts.json`, so Thanet is just the first
entry and the rest of Kent lands beside it, run by run, sharing one engine.

Why single tiles rather than the district pack: measured in the browser on
2026-08-04, the 140k-sector Margate 3x3 ran at 9 FPS and halving the render
resolution moved it to 10 — CPU-bound, so the fix is fewer sectors per map,
not renderer settings. One 800m tile (~8k median sectors) is the playable
unit. The full district PK3 stays a download for native GZDoom.

Why `common.pk3`: a standalone tile PK3 is ~1.2 MB of generated art plus a
map that is often smaller than the art. 200 tiles shipped 342 MB, of which
~240 MB was the same art 200 times. Every member that is byte-identical
across all of a district's tiles goes into one `common.pk3` the launcher
loads before the tile (later files win, so per-tile members still override).
The membership test is empirical — identical (size, CRC) in every tile —
so anything genuinely per-tile (TEXTMAP, MAPINFO titles, minimap,
ATTRIBUTION.txt with its tile id, the ZScript with baked coordinates)
excludes itself.

The per-tile PK3 is the same assembly `build-district.py` does, with one map:
`build_zscript` documents a single tile as the degenerate one-entry region,
so the HUD, telemetry and attribution all come along unchanged. Edge
transitions have no neighbour and do nothing, which is the already-tested
behaviour at a region's outer boundary.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from postcode2wad.tiles import Tile

TEXTMAP_MEMBER = "TEXTMAP"
MINIMAP_MEMBER = "minimap.png"
META_MEMBER = "meta.json"

#: Engine files the launcher fetches at runtime. uzdoom.data is not listed:
#: emscripten resolves it relative to uzdoom.js itself, and the launcher
#: intercepts that fetch via Module.getPreloadedPackage.
ENGINE_FILES = [
    "uzdoom.js",
    "uzdoom.wasm",
    "uzdoom.data",
    "uzdoom.pk3",
    "game_support.pk3",
    "brightmaps.pk3",
    "lights.pk3",
    "game_widescreen_gfx.pk3",
]

IWAD = "freedoom2.wad"

TEMPLATE_DIR = Path(__file__).resolve().parent / "webdemo"
TEMPLATE_FILES = ["index.html", "play.html", "_headers", "serve.py"]


def build_tile_pk3(artifact: Path, out: Path) -> dict | None:
    """One standalone PK3 from one district artifact. Returns its manifest row."""
    from postcode2wad.hud import HANDLER, ZSCRIPT_LUMP, build_zscript, minimap_lump
    from postcode2wad.pk3 import FOG_DENSITY, write_region_pk3

    with zipfile.ZipFile(artifact) as zf:
        meta = json.loads(zf.read(META_MEMBER))
        if meta.get("outcome") != "built":
            return None
        textmap = zf.read(TEXTMAP_MEMBER).decode()
        minimap = zf.read(MINIMAP_MEMBER)

    tile = Tile(meta["ix"], meta["iy"], meta["size_m"])
    title = meta.get("title") or tile.id

    extra = {
        f"graphics/{minimap_lump(0)}.png": minimap,
        ZSCRIPT_LUMP: build_zscript([tile], ["MAP01"]).encode(),
    }
    write_region_pk3(
        out,
        [("MAP01", title, textmap)],
        fog_density=FOG_DENSITY,
        extra_files=extra,
        event_handler=HANDLER,
        tile_id=tile.id,
    )
    return {
        "id": tile.id,
        "ix": meta["ix"],
        "iy": meta["iy"],
        "title": title,
        "sectors": meta.get("sectors", 0),
        "buildings": meta.get("buildings", 0),
        "trees": meta.get("trees", 0),
    }


def split_common(pk3s: list[Path], common_out: Path) -> int:
    """Factor byte-identical members out of every PK3 into one shared file.

    Membership is decided by (size, CRC) agreeing across *all* tiles, then the
    bytes are taken from the first. Anything per-tile disagrees on CRC and
    stays put. Returns the number of members moved.
    """
    shared: dict[str, tuple[int, int]] | None = None
    for p in pk3s:
        with zipfile.ZipFile(p) as zf:
            entries = {i.filename: (i.file_size, i.CRC) for i in zf.infolist()}
        if shared is None:
            shared = entries
        else:
            shared = {
                name: sig
                for name, sig in shared.items()
                if entries.get(name) == sig
            }
    if not shared:
        return 0

    with (
        zipfile.ZipFile(pk3s[0]) as zf,
        zipfile.ZipFile(common_out, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for name in shared:
            out.writestr(name, zf.read(name))

    for p in pk3s:
        tmp = p.with_suffix(".tmp")
        with zipfile.ZipFile(p) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename not in shared:
                    dst.writestr(info.filename, src.read(info.filename))
        tmp.replace(p)
    return len(shared)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-webdemo.py", description=__doc__)
    parser.add_argument("--work", required=True, metavar="DIR", help="district work dir")
    parser.add_argument("--out", default="site", metavar="DIR")
    parser.add_argument(
        "--engine-dir",
        required=True,
        metavar="DIR",
        help=f"directory holding {ENGINE_FILES[0]}..{ENGINE_FILES[-1]} and {IWAD}",
    )
    args = parser.parse_args(argv)

    work = Path(args.work)
    out = Path(args.out)
    engine_src = Path(args.engine_dir)

    plan = json.loads((work / "plan.json").read_text())
    district = plan.get("district", "district")
    slug = re.sub(r"[^a-z0-9]+", "-", district.lower()).strip("-")

    artifacts = sorted((work / "tiles").glob("*.zip"))
    if not artifacts:
        print(f"error: no artifacts under {work / 'tiles'}", file=sys.stderr)
        return 2

    missing = [f for f in [*ENGINE_FILES, IWAD] if not (engine_src / f).exists()]
    if missing:
        print(f"error: {engine_src} is missing {', '.join(missing)}", file=sys.stderr)
        return 2

    ddir = out / slug
    if ddir.exists():
        shutil.rmtree(ddir)  # a rebuild replaces the district wholesale
    (ddir / "tiles").mkdir(parents=True)
    (ddir / "minimaps").mkdir()
    (out / "engine").mkdir(exist_ok=True)

    rows = []
    pk3s = []
    for artifact in artifacts:
        pk3 = ddir / "tiles" / f"{artifact.stem}.pk3"
        row = build_tile_pk3(artifact, pk3)
        if row is None:
            continue
        with zipfile.ZipFile(artifact) as zf:
            (ddir / "minimaps" / f"{artifact.stem}.png").write_bytes(zf.read(MINIMAP_MEMBER))
        rows.append(row)
        pk3s.append(pk3)

    if not rows:
        print("error: no built tiles found", file=sys.stderr)
        return 3

    moved = split_common(pk3s, ddir / "common.pk3")
    for row, pk3 in zip(rows, pk3s):
        row["bytes"] = pk3.stat().st_size

    manifest = {
        "district": district,
        "size_m": plan.get("size_m", 800),
        "common": "common.pk3",
        "common_bytes": (ddir / "common.pk3").stat().st_size,
        "tiles": rows,
    }
    (ddir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # districts.json accumulates across runs — this is how the rest of Kent
    # (and anywhere else with LIDAR) joins the same site later.
    dj_path = out / "districts.json"
    listing = json.loads(dj_path.read_text()) if dj_path.exists() else {"districts": []}
    listing["districts"] = [d for d in listing["districts"] if d["slug"] != slug]
    listing["districts"].append({"slug": slug, "name": district, "tiles": len(rows)})
    listing["districts"].sort(key=lambda d: d["name"])
    dj_path.write_text(json.dumps(listing, indent=1), encoding="utf-8")

    for f in [*ENGINE_FILES, IWAD]:
        shutil.copyfile(engine_src / f, out / "engine" / f)
    for f in TEMPLATE_FILES:
        shutil.copyfile(TEMPLATE_DIR / f, out / f)

    total = sum(r["bytes"] for r in rows)
    biggest = max(rows, key=lambda r: r["bytes"])
    print(
        f"\n{district}: {len(rows)} tiles, {total / 1e6:.0f} MB of PK3s "
        f"(largest {biggest['id']} at {biggest['bytes'] / 1e6:.1f} MB), "
        f"{moved} shared members -> common.pk3 "
        f"({manifest['common_bytes'] / 1e6:.1f} MB)"
    )
    print(
        f"site: engine {sum((out / 'engine' / f).stat().st_size for f in ENGINE_FILES) / 1e6:.0f} MB, "
        f"iwad {(out / 'engine' / IWAD).stat().st_size / 1e6:.0f} MB, "
        f"{len(listing['districts'])} district(s): "
        + ", ".join(d["name"] for d in listing["districts"])
    )
    print(f"wrote {out}/ — preview with: python {out}/serve.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
