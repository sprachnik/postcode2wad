import pytest

from postcode2wad import DEFAULT_TILE_SIZE_M
from postcode2wad.cli import build_parser, main


def test_defaults():
    args = build_parser().parse_args(["CT7 0XX"])
    assert args.postcode == "CT7 0XX"
    assert args.size == DEFAULT_TILE_SIZE_M
    assert args.out == "out.pk3"


def test_latlng_accepted_without_postcode():
    args = build_parser().parse_args(["--latlng", "51.376,1.302"])
    assert args.postcode is None
    assert args.latlng == "51.376,1.302"


def test_requires_a_location():
    with pytest.raises(SystemExit):
        main([])
