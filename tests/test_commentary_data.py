"""Tests for the bundled opening commentary data (data/commentary.json).

Validates structure, content, and that every key is a legal EPD position
parseable by python-chess (the app's canonical position producer).
"""
import json
import os

import chess
import pytest

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "commentary.json",
)


@pytest.fixture(scope="module")
def commentary():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_loads_as_dict(commentary):
    assert isinstance(commentary, dict)


def test_min_entry_count(commentary):
    assert len(commentary) >= 120, f"only {len(commentary)} entries"


def test_values_have_nonempty_san_and_text(commentary):
    for epd, value in commentary.items():
        assert isinstance(value, dict), f"value for {epd!r} is not a dict"
        san = value.get("san")
        text = value.get("text")
        assert isinstance(san, str) and san.strip(), f"bad san for {epd!r}"
        assert isinstance(text, str) and text.strip(), f"bad text for {epd!r}"


def test_keys_are_legal_epd_positions(commentary):
    for epd in commentary:
        board = chess.Board()
        # set_epd accepts the 4-field EPD (no clocks) and raises on illegality.
        board.set_epd(epd)
        assert board.is_valid(), f"invalid position for EPD {epd!r}"


def test_epd_roundtrips_to_same_key(commentary):
    """Each key must equal board.epd() of the position it encodes -> the same
    canonical producer the server uses for lookups."""
    for epd in commentary:
        board = chess.Board()
        board.set_epd(epd)
        assert board.epd() == epd, f"EPD not canonical: {epd!r}"
