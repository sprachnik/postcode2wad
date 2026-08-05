"""Assemble a static web demo from district build artifacts.

    python scripts/build-webdemo.py --work work/thanet-800m --out site \
        --engine-dir <dir with uzdoom.js/wasm/data + freedoom2.wad>

The output is a directory of nothing but static files: the uzdoom-wasm engine,
one *standalone single-tile PK3 per built tile*, each tile's minimap as a
plain PNG, per-district manifests, and two pages — a coverage map (click a
tile, pick a performance mode, spawn on it) and a launcher. Upload the
directory to any static host that can set two response headers; `_headers`
carries them for Netlify. (On the live R2 host `_headers` is inert — it is
just an uploaded file there, and the headers come from a Cloudflare Transform
Rule instead. The launcher's own error text still points at it, which is
misleading if you are debugging the deployed site rather than a Netlify one.)

The run ends by writing `.br` sidecars for the two files no CDN will compress
for you, and printing the exact rclone lines that upload them under the
uncompressed keys — see `precompress`.

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
TEMPLATE_FILES = ["index.html", "play.html", "favicon.svg", "_headers", "serve.py"]

#: Files pre-compressed for upload, and why only these two. Cloudflare
#: auto-compresses a fixed content-type list: `.wasm` and `.js` are on it and
#: arrive zstd'd, but everything else here is `application/octet-stream`, which
#: is not, so it arrives raw. Measured on the live site 2026-08-05: uzdoom.data
#: and freedoom2.wad were 86.4 MB of the ~102 MB cold load, both uncompressed.
#: The other octet-streams are PK3s — zip archives already, and gzip only takes
#: common.pk3 to 99%, so compressing them buys nothing. These two are raw
#: lump/asset containers and go to ~28%.
PRECOMPRESS = ["uzdoom.data", IWAD]

#: q11 costs ~70s for the IWAD and ~3min for the data package, against q9's 8s.
#: Worth it: q11 takes the IWAD to 28% where q9 stops at 34%, this runs once per
#: engine change (the `.br` is reused while it is newer than its source), and
#: the alternative is every visitor paying the difference on a cold load.
BROTLI_QUALITY = 11


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
        # Trimmed on the way into the manifest, which every visitor downloads
        # before they play anything: a full breakdown is eight keys per tile and
        # most of them round to nothing. Anything at or below 0.5% is dropped —
        # it would display as "0%" anyway.
        "cover": {k: v for k, v in meta.get("cover_pct", {}).items() if v > 0.5},
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


def _compressor():
    """(suffix, Content-Encoding, compress) — brotli if installed, else gzip.

    Brotli is not in the dependencies because it is needed only to deploy, not
    to build a WAD, and a missing wheel should not stop a build. The fallback
    is honest rather than silent: gzip still takes the two files from 86.4 MB
    to 32.0 MB where brotli reaches ~25 MB, and the caller prints which ran.
    """
    try:
        import brotli
    except ImportError:
        import gzip

        return ".gz", "gzip", lambda b: gzip.compress(b, 9)
    return ".br", "br", lambda b: brotli.compress(b, quality=BROTLI_QUALITY)


def precompress(paths: list[Path]) -> tuple[str, str, list[tuple[Path, int, int]]]:
    """Write `<name><suffix>` beside each path for upload with Content-Encoding.

    The compressed file sits *beside* the original rather than replacing it:
    `serve.py` and `--engine-dir site/engine` both want the real bytes, and a
    file whose name says `.data` but whose contents are brotli is exactly the
    kind of thing that renders fine and fails later. The deploy step uploads
    the `.br` under the *uncompressed* key, so the browser sees one URL.

    Skipped when the compressed file is newer than its source, because q11 on
    the data package is minutes and the engine changes about never.
    """
    suffix, encoding, compress = _compressor()
    done = []
    for src in paths:
        dst = src.with_name(src.name + suffix)
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            done.append((dst, src.stat().st_size, dst.stat().st_size))
            continue
        print(f"  compressing {src.name} ({src.stat().st_size / 1e6:.0f} MB)…", flush=True)
        dst.write_bytes(compress(src.read_bytes()))
        done.append((dst, src.stat().st_size, dst.stat().st_size))
    return suffix, encoding, done


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
    parser.add_argument(
        "--no-precompress",
        action="store_true",
        help="skip building the .br sidecars (minutes); leaves any existing ones alone",
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
    listing["districts"].append(
        {
            "slug": slug,
            "name": district,
            "tiles": len(rows),
            "area_km2": round(len(rows) * manifest["size_m"] ** 2 / 1e6, 2),
        }
    )
    listing["districts"].sort(key=lambda d: d["name"])

    # The headline: how much ground has actually been built, everywhere, so far.
    #
    # Computed here and never written by hand, because the moment it is a number
    # in a README it starts drifting from what shipped -- and a size claim that
    # is quietly wrong is worse than none.
    #
    # It counts *generated map*: tiles built, times tile area. That is
    # deliberately not the same as the area of the districts covered, and the
    # two must not be confused. A district's tiles are the grid squares that
    # intersect its ONS polygon, so they overhang its edges: Thanet is a 103 km²
    # district and its 200 tiles are 128 km² of map. The playable world is the
    # bigger number; the place it depicts is the smaller one. Say which you mean.
    listing["total_km2"] = round(sum(d["area_km2"] for d in listing["districts"]), 2)
    listing["tiles_total"] = sum(d["tiles"] for d in listing["districts"])
    dj_path.write_text(json.dumps(listing, indent=1), encoding="utf-8")

    # `--engine-dir site/engine` is the obvious thing to type on a rebuild --
    # the engine is already there and it is 103 MB -- and copying a file onto
    # itself raises. It used to raise *after* the tiles were rebuilt and before
    # the page templates were copied, which leaves the site subtly stale rather
    # than failing outright: new PK3s, old launcher.
    for f in [*ENGINE_FILES, IWAD]:
        src, dst = engine_src / f, out / "engine" / f
        if not (dst.exists() and src.resolve() == dst.resolve()):
            shutil.copyfile(src, dst)
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
    # Printed on every build so the headline figure is stated by the thing that
    # produced it, and a release note can quote a number that was measured
    # rather than remembered.
    breakdown = ", ".join(
        "{} {:,.2f}".format(d["name"], d["area_km2"]) for d in listing["districts"]
    )
    print(
        f"\nWORLD SO FAR: {listing['total_km2']:,.2f} km² of generated map "
        f"across {listing['tiles_total']:,} tiles ({breakdown})"
    )
    if not args.no_precompress:
        _suffix, encoding, made = precompress([out / "engine" / f for f in PRECOMPRESS])
        raw = sum(o for _, o, _ in made)
        packed = sum(c for _, _, c in made)
        print(
            f"precompressed ({encoding}): {raw / 1e6:.0f} MB -> {packed / 1e6:.0f} MB "
            f"({packed * 100 // raw}%), saving {(raw - packed) / 1e6:.0f} MB per cold load"
        )
        # Deliberately does *not* print the rclone lines. These files have to be
        # uploaded under the uncompressed key with a Content-Encoding header,
        # and a bare `rclone sync` undoes that twice over — so there is one
        # place that knows how, and this points at it rather than tempting
        # anyone to copy half of it.
        print(f"deploy with: python scripts/deploy-webdemo.py --site {out}")

    print(f"wrote {out}/ — preview with: python {out}/serve.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
