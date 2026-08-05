"""Push a built `site/` to the R2 bucket behind the web demo.

    python scripts/deploy-webdemo.py --site site

Separate from `build-webdemo.py` because the two happen at different rates:
a district build is minutes and rare, a deploy is seconds and follows every
launcher tweak.

The whole reason this is a script rather than one `rclone sync` is the two
pre-compressed files. `build-webdemo.py` writes `uzdoom.data.br` and
`freedoom2.wad.br` beside their originals, and they have to be uploaded
*under the uncompressed key* with `Content-Encoding: br` — the browser asks
for `/engine/uzdoom.data` and must get brotli bytes with a header that says
so. A plain `rclone sync` would do the opposite twice over: upload the raw
57 MB over the compressed object, and upload the `.br` as a second key nobody
requests. So sync is told to leave those keys alone and `copyto` maintains
them.

Why it matters: measured on the live site 2026-08-05, those two files were
86.4 MB of a ~102 MB cold load and arrived uncompressed, because Cloudflare
only auto-compresses a fixed content-type list and `application/octet-stream`
is not on it. Brotli q11 takes them to 23.9 MB.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

#: Keys served pre-compressed. Must match build-webdemo.py's PRECOMPRESS.
PRECOMPRESSED = ["engine/uzdoom.data", "engine/freedoom2.wad"]

DEFAULT_REMOTE = "r2:doommap"


def run(cmd: list[str], dry: bool) -> int:
    print("  " + " ".join(cmd))
    if dry:
        return 0
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy-webdemo.py", description=__doc__)
    parser.add_argument("--site", default="site", metavar="DIR")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, metavar="REMOTE:BUCKET")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rclone commands without running them",
    )
    args = parser.parse_args(argv)

    site = Path(args.site)
    if not (site / "index.html").exists():
        print(f"error: {site} does not look like a built site", file=sys.stderr)
        return 2
    if shutil.which("rclone") is None:
        print("error: rclone is not on PATH", file=sys.stderr)
        return 2

    # A missing sidecar is not fatal, but it silently costs every visitor 62 MB,
    # so it is worth saying out loud rather than deploying a slower site quietly.
    sidecars = [(k, site / (k + ".br")) for k in PRECOMPRESSED]
    missing = [k for k, p in sidecars if not p.exists()]
    if missing:
        print(
            f"warning: no .br for {', '.join(missing)} — run build-webdemo.py "
            f"without --no-precompress, or those files deploy uncompressed",
            file=sys.stderr,
        )

    print("sync (everything except the pre-compressed keys):")
    skips = ["--exclude", "*.br"]
    for key in PRECOMPRESSED:
        skips += ["--exclude", key]
    rc = run(["rclone", "sync", str(site), args.remote, *skips], args.dry_run)
    if rc != 0:
        print(f"error: rclone sync failed ({rc})", file=sys.stderr)
        return rc

    print("pre-compressed keys (Content-Encoding: br):")
    for key, path in sidecars:
        if not path.exists():
            continue
        rc = run(
            [
                "rclone",
                "copyto",
                str(path),
                f"{args.remote}/{key}",
                "--header-upload",
                "Content-Encoding: br",
                "--header-upload",
                "Content-Type: application/octet-stream",
            ],
            args.dry_run,
        )
        if rc != 0:
            print(f"error: uploading {key} failed ({rc})", file=sys.stderr)
            return rc

    if not args.dry_run:
        print(
            "\ndeployed. Verify the encoding actually survived — the failure mode "
            "is a 200 with no Content-Encoding, which looks fine and is 62 MB slower:\n"
            "  curl -sSI -H 'Accept-Encoding: br' "
            "https://doomearth.clawhangout.com/engine/uzdoom.data | grep -i content-\n"
            "Pages are on a short cache but tile PK3s are not: if tile contents "
            "changed, bump PK3_V in play.html or the new build reaches nobody."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
