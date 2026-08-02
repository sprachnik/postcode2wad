"""Generate playable GZDoom levels of real UK places from a postcode."""

__version__ = "0.0.1"

#: Map units per real-world metre. Player is 56 units tall (~1.75m), radius 16.
UNITS_PER_METRE = 32

#: Default tile edge length in metres. Tiles align to the British National Grid,
#: so the same tile ID always produces a byte-identical map.
DEFAULT_TILE_SIZE_M = 400

#: All UK source data lives in OSGB36 / British National Grid.
CRS_OSGB = "EPSG:27700"
CRS_WGS84 = "EPSG:4326"
