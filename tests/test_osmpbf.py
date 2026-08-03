"""The local .osm.pbf reader, against a scene whose answer is known by hand.

There are now two things that can answer "which ways touch this tile", and the
only failure mode that matters is the two of them disagreeing. A disagreement
does not crash, does not look wrong in isolation, and shows up as a hedge or a
whole field that one build has and the other does not — on tile 4,000 of a
county, long after anyone was looking.

So the scene here is built once and asked twice: written to a .osm.pbf for the
local reader, and written to the response cache as the JSON Overpass would have
returned for the same query, so `overpass.fetch_tile` answers it for real
without touching the network. The two answers have to be identical, bucket for
bucket and id for id.

The Overpass fixture is *hand-written ground truth*, not derived from the code
under test: it contains exactly the ways whose linework meets the query
rectangle, which is Overpass's own bbox rule. In particular it does not contain
the field that swallows the tile whole without touching its edge — a bbox-only
index returns that one, and it would carpet a real tile in a single land parcel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from postcode2wad.sources import cache, osm, osmpbf, overpass

osmium = pytest.importorskip("osmium")

#: The query. Chosen so its eastern edge needs rounding to six decimals, which
#: is the precision `overpass.build_query` actually asks about.
BBOX = (51.000000, 1.000000, 51.0100004, 1.0100004)

#: (id, tags, [(lon, lat)...]). Everything the reader has to get right, once.
SCENE = [
    # Inside, closed: a building.
    (101, {"building": "house"}, [(1.002, 51.002), (1.003, 51.002), (1.003, 51.003), (1.002, 51.003), (1.002, 51.002)]),
    # Crosses the whole box west to east with no node anywhere inside it. A
    # "does any node fall in the bbox" test drops this road entirely.
    (102, {"highway": "residential"}, [(0.990, 51.005), (1.020, 51.005)]),
    # Contains the box completely and touches none of it. Must NOT be returned.
    (103, {"landuse": "farmland"}, [(0.900, 50.900), (1.100, 50.900), (1.100, 51.100), (0.900, 51.100), (0.900, 50.900)]),
    # Well outside, in every direction that matters.
    (104, {"building": "yes"}, [(1.200, 51.200), (1.201, 51.200), (1.201, 51.201), (1.200, 51.201), (1.200, 51.200)]),
    # A stream: `waterway` is only asked for as `waterway=riverbank`, so this
    # must not even be indexed.
    (105, {"waterway": "stream"}, [(1.004, 51.001), (1.004, 51.008)]),
    (106, {"waterway": "riverbank"}, [(1.006, 51.006), (1.007, 51.006), (1.007, 51.007), (1.006, 51.007), (1.006, 51.006)]),
    (107, {"barrier": "hedge"}, [(1.001, 51.008), (1.008, 51.008)]),
    (108, {"natural": "coastline"}, [(1.001, 51.009), (1.009, 51.009)]),
    # Its only node inside the box sits 3e-7 degrees — about 2 cm — past the
    # six-decimal eastern edge Overpass is given, so Overpass does not see it.
    (109, {"highway": "path"}, [(1.0200000, 51.0050000), (1.0100003, 51.0050000)]),
]

#: What Overpass returns for BBOX: every way whose *linework* meets the
#: rectangle, and which one of `TILE_SELECTORS` asks for. Written out by hand.
OVERPASS_IDS = [101, 102, 106, 107, 108]


def write_pbf(path: Path) -> Path:
    writer = osmium.SimpleWriter(str(path))
    node_id = 1
    ways = []
    for osm_id, tags, coords in SCENE:
        refs = []
        for lon, lat in coords:
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=(lon, lat)))
            refs.append(node_id)
            node_id += 1
        if coords[0] == coords[-1]:  # a ring shares its first and last node
            refs[-1] = refs[0]
        ways.append(osmium.osm.mutable.Way(id=osm_id, nodes=refs, tags=tags))
    for way in ways:
        writer.add_way(way)
    writer.close()
    return path


def write_overpass_cache(cache_dir: Path) -> None:
    """The response Overpass would give, into the on-disk response cache."""
    by_id = {osm_id: (tags, coords) for osm_id, tags, coords in SCENE}
    elements = []
    for osm_id in OVERPASS_IDS:
        tags, coords = by_id[osm_id]
        elements.append(
            {
                "type": "way",
                "id": osm_id,
                "tags": tags,
                "geometry": [{"lon": lon, "lat": lat} for lon, lat in coords],
            }
        )
    query = overpass.build_query(BBOX, overpass.TILE_SELECTORS)
    cache.store_json("overpass", query, {"elements": elements}, cache_dir)


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    directory = tmp_path_factory.mktemp("osmpbf")
    pbf = write_pbf(directory / "scene.osm.pbf")
    cache_dir = directory / "cache"
    write_overpass_cache(cache_dir)
    return pbf, cache_dir


def buckets(features) -> dict[str, list[int]]:
    return {
        name: [f.osm_id for f in getattr(features, name)]
        for name in ("buildings", "roads", "water", "landuse", "coastline", "barriers")
    }


def test_agrees_with_overpass_bucket_for_bucket(scene):
    pbf, cache_dir = scene
    local = osmpbf.fetch_tile(BBOX, cache_dir, pbf=pbf)
    remote = overpass.fetch_tile(BBOX, cache_dir)
    assert buckets(local) == buckets(remote)
    # And the answer is the one written down by hand, not just self-consistent.
    assert buckets(local) == {
        "buildings": [101],
        "roads": [102],
        "water": [106],
        "landuse": [],
        "coastline": [108],
        "barriers": [107],
    }


def test_way_crossing_without_a_node_inside_is_kept(scene):
    pbf, cache_dir = scene
    local = osmpbf.fetch_tile(BBOX, cache_dir, pbf=pbf)
    assert [f.osm_id for f in local.roads] == [102]


def test_area_containing_the_tile_is_not_returned(scene):
    """The bbox index proposes way 103; only the linework test rejects it."""
    pbf, cache_dir = scene
    local = osmpbf.fetch_tile(BBOX, cache_dir, pbf=pbf)
    assert 103 not in [f.osm_id for f in local.landuse]


def test_coordinates_and_tags_survive_the_round_trip(scene):
    """1e-7 degrees exactly: OSM's own precision, and Overpass's seven decimals."""
    pbf, cache_dir = scene
    local = osmpbf.fetch_tile(BBOX, cache_dir, pbf=pbf)
    remote = overpass.fetch_tile(BBOX, cache_dir)
    mine = {f.osm_id: f for f in local.buildings + local.roads + local.barriers}
    theirs = {f.osm_id: f for f in remote.buildings + remote.roads + remote.barriers}
    for osm_id, feature in theirs.items():
        assert mine[osm_id].coords == feature.coords
        assert mine[osm_id].tags == feature.tags
        assert mine[osm_id].closed == feature.closed


def test_value_selectors_are_not_widened_to_the_whole_key(scene):
    """`waterway=riverbank` is asked for; `waterway=stream` is not."""
    pbf, cache_dir = scene
    index = osmpbf.open_index(pbf, cache_dir)
    indexed = {index.record(i)[0] for i in range(len(index.offsets) - 1)}
    assert 106 in indexed
    assert 105 not in indexed


def test_index_refuses_a_selector_it_was_not_built_for(scene):
    pbf, cache_dir = scene
    with pytest.raises(osmpbf.ExtractError):
        osmpbf.fetch_tile(BBOX, cache_dir, selectors=['way["aeroway"]'], pbf=pbf)


def test_unreadable_selector_is_rejected_rather_than_ignored():
    with pytest.raises(osmpbf.ExtractError):
        osmpbf.parse_selectors(['way(if:count_tags() > 0)'])


def test_a_query_outside_the_extract_falls_back_rather_than_returning_nothing(scene):
    """The worst answer here is an empty map with no complaint.

    This scene's .pbf carries no header bounding box — `osmium` writes one only
    if asked — so coverage has to come from the extent the index recorded.
    """
    pbf, cache_dir = scene
    osmpbf.open_index(pbf, cache_dir)
    osmpbf.extract_bbox.cache_clear()
    elsewhere = (51.480, -3.182, 51.484, -3.176)  # Cardiff
    assert osm.choose(elsewhere, "auto", pbf, cache_dir) == ("overpass", None)
    assert osm.choose(BBOX, "auto", pbf, cache_dir) == ("local", pbf)
    with pytest.raises(osmpbf.ExtractError):
        osm.choose(elsewhere, "local", pbf, cache_dir)


def test_overpass_can_always_be_insisted_on(scene):
    pbf, cache_dir = scene
    assert osm.choose(BBOX, "overpass", pbf, cache_dir) == ("overpass", None)


def test_index_records_where_its_data_actually_is(scene):
    """Used to decide whether an extract can answer a query at all."""
    pbf, cache_dir = scene
    meta = json.loads((osmpbf.index_dir(pbf, cache_dir) / "meta.json").read_text())
    south, west, north, east = meta["data_bbox"]
    assert (south, west) == pytest.approx((50.9, 0.9))
    assert (north, east) == pytest.approx((51.201, 1.201))
    assert meta["incomplete_ways"] == 0
