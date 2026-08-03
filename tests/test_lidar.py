"""The local LIDAR mirror: grid arithmetic, mosaicking, and the two fallbacks.

The only failure mode that matters here is silence. A resolver that mosaics four
5km squares in the wrong order still produces an 800x800 float32 raster with no
holes in it — it just puts the wrong hill in the wrong field, on a tile nobody
opens until much later. So the mosaic is checked against a ground truth computed
straight from OSGB coordinates rather than from anything the resolver does.

The fixture stores are real 5000x5000 1m GeoTIFFs on the real grid, because the
resolver's whole job is the arithmetic between the 5km publication grid and the
800m map grid, and a scaled-down fixture would not exercise it. They are cheap
because only the corner each tile actually contributes carries data; the rest is
NODATA, which LZW compresses to nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

from postcode2wad.sources import lidar

rasterio = pytest.importorskip("rasterio")

NODATA = np.float32(-3.4028235e38)

#: A pixel's elevation encodes its own OSGB position, so a misplaced patch is
#: visible as a wrong *value* and not merely as a hole. Both terms stay whole
#: and the total stays under 2^24, so every value is exact in float32.
def ground_truth(easting: float, northing: float) -> float:
    return (easting - 620000) * 1000.0 + (northing - 160000)


#: The four 5km squares meeting at E 625000, N 165000.
CORNER_TILES = {
    "TR2060": (620000, 160000),
    "TR2560": (625000, 160000),
    "TR2065": (620000, 165000),
    "TR2565": (625000, 165000),
}

#: A map tile centred on that corner, so it needs all four.
STRADDLE_BBOX = (624600, 164600, 625400, 165400)


def _write_square(directory, coverage, tile, origin_e, origin_n, filled_m=1000):
    """One 5km GeoTIFF, with data only in the corner nearest E625000/N165000."""
    from rasterio.transform import from_origin

    size = lidar.GRID_TILE_M
    values = np.full((size, size), NODATA, dtype="float32")

    # Which corner of this square faces the meeting point.
    east_side = origin_e < 625000  # data lives at the square's eastern edge
    north_side = origin_n < 165000  # ...and its northern edge
    col0 = size - filled_m if east_side else 0
    row0 = 0 if north_side else size - filled_m

    cols = origin_e + col0 + np.arange(filled_m, dtype="float64")
    # Row r covers northing [top - r - 1, top - r); label it by its south edge.
    rows = (origin_n + size) - row0 - 1 - np.arange(filled_m, dtype="float64")
    values[row0 : row0 + filled_m, col0 : col0 + filled_m] = ground_truth(
        cols[None, :], rows[:, None]
    ).astype("float32")

    path = lidar.coverage_tile_path(coverage, tile, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:27700",
        transform=from_origin(origin_e, origin_n + size, 1, 1),
        compress="lzw",
        tiled=True,
        nodata=float(NODATA),
    ) as dst:
        dst.write(values, 1)
    return path


@pytest.fixture
def store(tmp_path):
    for tile, (east, north) in CORNER_TILES.items():
        _write_square(tmp_path, lidar.DTM, tile, east, north)
    return tmp_path


def test_grid_tile_is_named_by_its_south_west_corner():
    # TR2565 is E 625000-630000, N 165000-170000. Birchington sits in it.
    assert lidar.grid_tile_id(625000, 165000) == "TR2565"
    assert lidar.grid_tile_id(627500, 167500) == "TR2565"
    assert lidar.grid_tile_id(629999.9, 169999.9) == "TR2565"
    assert lidar.grid_tile_id(630000, 165000) == "TR3065"
    # The other square Kent occupies, and one from the report's own sample.
    assert lidar.grid_tile_id(570000, 155000) == "TQ7055"
    assert lidar.grid_square(542131, 116569) == "TQ"


def test_grid_cells_are_half_open_on_the_north_and_east_edges():
    """5000 is not a multiple of 800, so this is where the mosaic is decided.

    A bbox whose eastern edge lands *exactly* on a 5km boundary does not reach
    into the next square: the pixel at E 630000 belongs to the tile that starts
    there, and the window stops one pixel short of it. Counting it anyway costs
    a file open per tile forever, and — when the extra square is offshore and so
    was never mirrored — a spurious fallback to the network on every tile along
    the seam.
    """
    assert lidar.grid_tiles_for_bbox((625000, 165000, 630000, 170000)) == ["TR2565"]
    assert lidar.grid_tiles_for_bbox((629200, 168800, 629600, 169200)) == ["TR2565"]
    assert lidar.grid_tiles_for_bbox((629600, 168800, 630400, 169600)) == ["TR2565", "TR3065"]
    assert sorted(lidar.grid_tiles_for_bbox(STRADDLE_BBOX)) == sorted(CORNER_TILES)


def test_mosaic_stitches_four_squares_into_one_window(store):
    raster = lidar.fetch_dtm(STRADDLE_BBOX, source="local", directory=store)

    min_e, min_n, max_e, max_n = STRADDLE_BBOX
    assert raster.values.shape == (max_n - min_n, max_e - min_e)
    assert (raster.min_e, raster.min_n, raster.max_n) == (min_e, min_n, max_n)
    assert raster.coverage == 1.0

    rows = np.arange(raster.values.shape[0])
    cols = np.arange(raster.values.shape[1])
    expected = ground_truth(min_e + cols[None, :], max_n - rows[:, None] - 1)
    # Exact, not close: a windowed read is a copy, so anything but equality
    # means the patch landed in the wrong place.
    assert np.array_equal(raster.values, expected.astype("float32"))


def test_a_missing_square_falls_back_whole_rather_than_half(store, capsys):
    """Half off disk and half over the network would be *correct* — the two are
    bit-identical — and that is exactly why it must not happen quietly: a
    half-finished mirror would then look like a finished one forever."""
    lidar.coverage_tile_path(lidar.DTM, "TR2565", store).unlink()
    lidar.TALLY.reset()

    assert lidar._mosaic_local(lidar.DTM, STRADDLE_BBOX, store) is None
    assert "TR2565" in capsys.readouterr().err

    with pytest.raises(RuntimeError, match="not in"):
        lidar.fetch_dtm(STRADDLE_BBOX, source="local", directory=store)


def test_the_dsm_gap_over_TR_is_named_not_just_missed(tmp_path, capsys):
    """The EA publishes no 1m last-return DSM raster for any TR tile — 72 of
    Kent's 191 squares, including all of Thanet, Canterbury and Dover. Trees
    come from DSM minus DTM, so a mirror that treated that as an ordinary
    absence would hand back tree-less maps for half the county without a word.
    """
    lidar.TALLY.reset()
    assert lidar._mosaic_local(lidar.DSM, STRADDLE_BBOX, tmp_path) is None

    shouted = capsys.readouterr().err
    assert "TR" in shouted
    assert "packaging defect" in shouted
    # The DTM has no such defect and must not borrow the excuse.
    assert "TR" in lidar.DTM.broken_squares or not lidar.DTM.broken_squares


def test_a_wrongly_placed_file_is_refused(store):
    """A square filed under the wrong name reads perfectly and is wrong
    everywhere, so the georeference is checked against the name."""
    good = lidar.coverage_tile_path(lidar.DTM, "TR2565", store)
    good.replace(lidar.coverage_tile_path(lidar.DTM, "TR3065", store))
    bbox = (630000, 165000, 630800, 165800)
    with pytest.raises(ValueError, match="says its corner is"):
        lidar.fetch_dtm(bbox, source="local", directory=store)


def test_an_all_nodata_tile_is_sea_and_says_so():
    empty = lidar.Raster(
        values=np.full((4, 4), np.nan, dtype="float32"), min_e=0, min_n=0, max_n=4
    )
    with pytest.raises(lidar.NoCoverageError, match="tile bng800-1-2"):
        lidar.fill_holes(empty, where="tile bng800-1-2")

    # A partly-dry tile is a different thing: it keeps the median fill.
    values = np.array([[1.0, 2.0], [3.0, np.nan]], dtype="float32")
    filled = lidar.fill_holes(lidar.Raster(values=values, min_e=0, min_n=0, max_n=2))
    assert filled.values[1][1] == pytest.approx(2.0)


def test_cli_offers_exactly_the_sources_the_resolver_accepts():
    """`cli.py` spells the choices out rather than importing them, to keep numpy
    out of `--help`. This is the thread that stops the two drifting apart."""
    from postcode2wad.cli import build_parser

    action = next(
        a for a in build_parser()._actions if "--lidar-source" in (a.option_strings or [])
    )
    assert tuple(action.choices) == lidar.SOURCES
