"""Tree scatter, and the difference between a budget and a crop.

`vegetation` caps how many billboards a tile may carry. The cap used to be
applied by returning out of the blob loop the moment it was reached, and
because `rasterio.features.shapes` yields blobs in raster scan order -- which
runs north to south -- that did not cap the count so much as crop the tile.
A wooded tile spent its whole allowance on its northern strip and the rest
came out bare, with the woodland stopping at an invisible east-west line.

It renders perfectly. Nothing errors, the count is a plausible round number,
and the only way to see it is to stand in the south of the tile or to measure
where the trees actually are -- which is what this does.
"""

import numpy as np
import pytest

from postcode2wad import terrain
from postcode2wad.sources.lidar import Raster
from postcode2wad.tiles import Tile


def _wooded_tile(size_m: int = 800, canopy_height: float = 10.0, stripes: int = 0):
    """A tile of canopy, optionally as separate woods spread north to south.

    `stripes` matters more than it looks. The defect this file exists for is
    that `shapes` yields *blobs* in raster scan order, north to south, and the
    cap was applied by leaving that loop -- so the northern woods were kept and
    the southern ones never reached. A single solid blob cannot show that: with
    one blob there is no blob order, and an early return crops whatever order
    `_scatter` happens to emit points in instead, which is a different bug that
    merely looks similar. Separate stripes reproduce the real mechanism.
    """
    tile = Tile(ix=100, iy=200, size_m=size_m)
    values = np.full((size_m, size_m), canopy_height, dtype="float32")
    if stripes:
        # Bands of canopy separated by clear ground, so each is its own blob.
        band = size_m // (stripes * 2)
        values[:] = 0.0
        for k in range(stripes):
            values[k * 2 * band : k * 2 * band + band, :] = canopy_height
    common = {
        "min_e": tile.ix * size_m,
        "min_n": tile.iy * size_m,
        "max_n": (tile.iy + 1) * size_m,
    }
    dtm = Raster(values=np.zeros((size_m, size_m), dtype="float32"), **common)
    dsm = Raster(values=values, **common)
    return dsm, dtm, tile


def test_the_cap_thins_the_whole_tile_rather_than_cropping_it():
    # Eight separate woods down the tile, which is the arrangement the bug
    # actually mangled: keep the northern ones, never reach the southern ones.
    max_trees = 300
    dsm, dtm, tile = _wooded_tile(stripes=8)
    trees = terrain.vegetation(dsm, dtm, tile, max_trees=max_trees)

    assert len(trees) == max_trees, "a solid-canopy tile should saturate the cap"

    span = tile.size_units
    ys = [t.y for t in trees]

    # The measurement that named the bug on real output: how far up the tile the
    # trees reach, and how much of the southern half gets any at all. On Thanet
    # a capped tile had all 900 in the northern 32%, median 84% up.
    covered = (max(ys) - min(ys)) / span
    southern = sum(1 for y in ys if y < 0.5 * span) / len(ys)

    assert covered > 0.85, f"trees only cover {covered:.0%} of the tile height"
    assert 0.35 < southern < 0.65, (
        f"{southern:.0%} of trees in the southern half — the cap is cropping, "
        f"not thinning"
    )

    # Every wood should be represented, not just the ones scanned first.
    bands = {int(y / span * 8) for y in ys}
    assert len(bands) >= 7, f"trees only reached {len(bands)} of 8 woods"


def test_the_same_tile_always_grows_the_same_wood():
    """The rng is tile-seeded, and the thinning draws from it too -- so it must
    not have made the output depend on call order."""
    dsm, dtm, tile = _wooded_tile()
    first = terrain.vegetation(dsm, dtm, tile, max_trees=120)
    second = terrain.vegetation(dsm, dtm, tile, max_trees=120)
    assert [(t.x, t.y) for t in first] == [(t.x, t.y) for t in second]


def test_a_tile_under_the_cap_keeps_everything():
    """Thinning must be reachable only when there is something to thin."""
    dsm, dtm, tile = _wooded_tile(size_m=100)
    plenty = 10_000
    trees = terrain.vegetation(dsm, dtm, tile, max_trees=plenty)
    assert 0 < len(trees) < plenty


@pytest.mark.parametrize("height", [1.0, 40.0])
def test_canopy_outside_the_height_band_is_not_a_tree(height):
    """Guards the band the scatter runs inside: a kerb is not a sapling and a
    crane is not an oak."""
    dsm, dtm, tile = _wooded_tile(canopy_height=height)
    assert terrain.vegetation(dsm, dtm, tile) == []
