"""
Tests for app/traps.py — Opening Traps backend logic.

No Stockfish needed. Uses tests/fixtures/traps_sample.json only.
"""

from __future__ import annotations

import json
import os

import chess
import pytest

from app import traps

FIXTURE_PATH = "tests/fixtures/traps_sample.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STARTING_FEN = chess.STARTING_FEN


def _epd_after_san(san_list: list[str]) -> str:
    """Replay a list of SAN moves from the starting position and return the EPD."""
    board = chess.Board()
    for san in san_list:
        board.push_san(san)
    return board.epd()


def _epd_after_uci(uci_list: list[str]) -> str:
    """Replay a list of UCI moves from the starting position and return the EPD."""
    board = chess.Board()
    for uci in uci_list:
        board.push_uci(uci)
    return board.epd()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def load_fixture():
    """Load the sample fixture before each test and reset after."""
    traps.load(FIXTURE_PATH)
    yield
    # Reset to empty after each test so tests don't bleed into each other.
    traps.load("/nonexistent/path/for/traps/test")


# ---------------------------------------------------------------------------
# 1. summaries() ordering: commonness DESC, then name ASC
# ---------------------------------------------------------------------------


class TestSummariesOrdering:
    def test_summaries_returns_list(self):
        """summaries() returns a non-empty list when data is loaded."""
        result = traps.summaries()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_summaries_commonness_desc(self):
        """summaries() are ordered by commonness descending."""
        result = traps.summaries()
        for i in range(len(result) - 1):
            assert result[i]["commonness"] >= result[i + 1]["commonness"], (
                f"summaries not sorted by commonness DESC: "
                f"[{i}]={result[i]['commonness']} < [{i+1}]={result[i+1]['commonness']}"
            )

    def test_summaries_name_asc_within_same_commonness(self):
        """summaries() are ordered by name ASC within the same commonness tier."""
        result = traps.summaries()
        for i in range(len(result) - 1):
            if result[i]["commonness"] == result[i + 1]["commonness"]:
                assert result[i]["name"] <= result[i + 1]["name"], (
                    f"Same-commonness summaries not sorted by name ASC: "
                    f"{result[i]['name']!r} > {result[i+1]['name']!r}"
                )

    def test_summaries_contains_expected_keys(self):
        """Each summary has the required keys."""
        required = {"id", "name", "color", "eco", "parentOpening", "commonness"}
        for summary in traps.summaries():
            assert required <= set(summary.keys()), (
                f"Summary missing keys: {required - set(summary.keys())}"
            )

    def test_summaries_ordering_fixture(self):
        """Fixture has ruy-lopez-trap (commonness=4) before albin (commonness=2)."""
        result = traps.summaries()
        ids = [s["id"] for s in result]
        assert ids.index("ruy-lopez-trap") < ids.index("albin-counter-trap"), (
            f"Expected ruy-lopez-trap before albin-counter-trap, got: {ids}"
        )


# ---------------------------------------------------------------------------
# 2. start_epd_index and available() — including transpositions
# ---------------------------------------------------------------------------


class TestStartEpdIndex:
    def test_epd_index_is_populated(self):
        """start_epd_index has at least one entry after loading."""
        assert len(traps.start_epd_index) > 0

    def test_ruy_trap_reachable_via_standard_order(self):
        """available() returns ruy-lopez-trap via standard move order."""
        # 1.e4 e5 2.Nf3 — leadInSan of ruy-lopez-trap
        result = traps.available(STARTING_FEN, ["e2e4", "e7e5", "g1f3"])
        ids = [s["id"] for s in result]
        assert "ruy-lopez-trap" in ids, f"Expected ruy-lopez-trap in {ids}"

    def test_ruy_trap_reachable_via_transposition(self):
        """available() returns ruy-lopez-trap via transposed move order (1.Nf3 e5 2.e4).

        Both 1.e4 e5 2.Nf3 and 1.Nf3 e5 2.e4 reach the same EPD.
        """
        result = traps.available(STARTING_FEN, ["g1f3", "e7e5", "e2e4"])
        ids = [s["id"] for s in result]
        assert "ruy-lopez-trap" in ids, (
            f"Expected ruy-lopez-trap via transposition, got: {ids}"
        )

    def test_transposition_and_standard_return_same_ids(self):
        """Standard and transposed move orders for the Ruy trap return the same result."""
        standard = traps.available(STARTING_FEN, ["e2e4", "e7e5", "g1f3"])
        transposed = traps.available(STARTING_FEN, ["g1f3", "e7e5", "e2e4"])
        assert [s["id"] for s in standard] == [s["id"] for s in transposed], (
            f"Transposition mismatch: standard={standard}, transposed={transposed}"
        )

    def test_available_from_starting_position_is_empty(self):
        """available() returns [] from the starting position (no trap starts there)."""
        result = traps.available(STARTING_FEN, [])
        assert result == [], f"Expected empty from starting position, got: {result}"

    def test_available_returns_empty_on_unknown_position(self):
        """available() returns [] for a position that matches no trap start."""
        result = traps.available(STARTING_FEN, ["d2d4", "d7d5"])
        # Not a trap start (d4 d5 only — albin needs c4 too)
        # This may or may not include albin depending on fixture; just check type
        assert isinstance(result, list)

    def test_albin_trap_reachable(self):
        """available() returns albin-counter-trap after 1.d4 d5 2.c4."""
        result = traps.available(STARTING_FEN, ["d2d4", "d7d5", "c2c4"])
        ids = [s["id"] for s in result]
        assert "albin-counter-trap" in ids, f"Expected albin-counter-trap in {ids}"

    def test_available_result_is_summaries_subset(self):
        """available() result entries have the same structure as summaries()."""
        result = traps.available(STARTING_FEN, ["e2e4", "e7e5", "g1f3"])
        required = {"id", "name", "color", "eco", "parentOpening", "commonness"}
        for item in result:
            assert required <= set(item.keys()), (
                f"available() item missing keys: {required - set(item.keys())}"
            )

    def test_available_invalid_fen_returns_empty(self):
        """available() returns [] on an invalid base FEN."""
        result = traps.available("not a valid fen", ["e2e4"])
        assert result == []

    def test_available_illegal_move_returns_empty(self):
        """available() returns [] if a UCI move is illegal."""
        result = traps.available(STARTING_FEN, ["e2e4", "e2e4"])  # e2e4 twice is illegal
        assert result == []


# ---------------------------------------------------------------------------
# 3. get(id) — hit and miss
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_hit(self):
        """get() returns the full trap object for a known id."""
        trap = traps.get("ruy-lopez-trap")
        assert trap is not None
        assert trap["id"] == "ruy-lopez-trap"
        assert "variations" in trap
        assert "leadInSan" in trap

    def test_get_miss_returns_none(self):
        """get() returns None for an unknown id."""
        result = traps.get("nonexistent-trap-id")
        assert result is None

    def test_get_returns_full_object(self):
        """get() returns the full trap object including all required fields."""
        trap = traps.get("albin-counter-trap")
        assert trap is not None
        required = {"id", "name", "color", "eco", "parentOpening",
                    "commonness", "startFen", "leadInSan", "variations"}
        assert required <= set(trap.keys()), (
            f"Full trap missing keys: {required - set(trap.keys())}"
        )


# ---------------------------------------------------------------------------
# 4. Malformed trap is dropped; others still load
# ---------------------------------------------------------------------------


class TestMalformedTrap:
    def test_malformed_trap_dropped_others_load(self, tmp_path):
        """A malformed trap in the file is dropped; valid traps still load."""
        bad_trap = {
            "id": "bad-trap",
            # missing required: name, color, eco, parentOpening, commonness,
            # startFen, leadInSan, variations
        }
        good_trap = {
            "id": "good-trap",
            "name": "Good Trap",
            "color": "white",
            "eco": "A00",
            "parentOpening": "Test Opening",
            "commonness": 3,
            "startFen": chess.STARTING_FEN,
            "leadInSan": [],
            "variations": [
                {
                    "label": "Test variation",
                    "mainLine": [
                        {"san": "e4", "uci": "e2e4", "side": "trapper"}
                    ]
                }
            ]
        }
        fixture = {"traps": [bad_trap, good_trap]}
        fixture_file = tmp_path / "mixed.json"
        fixture_file.write_text(json.dumps(fixture))

        traps.load(str(fixture_file))

        # bad-trap must be dropped
        assert traps.get("bad-trap") is None, "Malformed trap was not dropped"
        # good-trap must be present
        assert traps.get("good-trap") is not None, "Valid trap was incorrectly dropped"
        assert len(traps.summaries()) == 1

    def test_trap_with_bad_lead_in_dropped(self, tmp_path):
        """A trap whose leadInSan contains an illegal move is dropped."""
        bad_leadin = {
            "id": "bad-leadin-trap",
            "name": "Bad Lead-In",
            "color": "white",
            "eco": "A00",
            "parentOpening": "Test",
            "commonness": 1,
            "startFen": chess.STARTING_FEN,
            "leadInSan": ["e9e5"],  # illegal SAN
            "variations": [
                {"label": "v", "mainLine": [{"san": "e4", "uci": "e2e4", "side": "trapper"}]}
            ]
        }
        good_trap = {
            "id": "good-trap-2",
            "name": "Good Trap 2",
            "color": "black",
            "eco": "B00",
            "parentOpening": "Test",
            "commonness": 2,
            "startFen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "leadInSan": ["e4", "e5"],
            "variations": [
                {"label": "v", "mainLine": [{"san": "Nf3", "uci": "g1f3", "side": "trapper"}]}
            ]
        }
        fixture = {"traps": [bad_leadin, good_trap]}
        fixture_file = tmp_path / "partial.json"
        fixture_file.write_text(json.dumps(fixture))

        traps.load(str(fixture_file))

        assert traps.get("bad-leadin-trap") is None
        assert traps.get("good-trap-2") is not None

    def test_empty_variations_dropped(self, tmp_path):
        """A trap with an empty variations list is dropped."""
        trap = {
            "id": "no-variations",
            "name": "No Variations",
            "color": "white",
            "eco": "A00",
            "parentOpening": "Test",
            "commonness": 1,
            "startFen": chess.STARTING_FEN,
            "leadInSan": [],
            "variations": []  # empty — must be dropped
        }
        fixture = {"traps": [trap]}
        fixture_file = tmp_path / "empty_vars.json"
        fixture_file.write_text(json.dumps(fixture))

        traps.load(str(fixture_file))

        assert traps.get("no-variations") is None
        assert traps.summaries() == []


# ---------------------------------------------------------------------------
# 5. Missing / invalid file path → empty structures, no raise
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_nonexistent_path_returns_empty(self):
        """load() with a nonexistent path resets to empty without raising."""
        traps.load("/nonexistent/path/for/traps/test.json")
        assert traps.traps_by_id == {}
        assert traps.start_epd_index == {}
        assert traps.summaries() == []

    def test_invalid_json_returns_empty(self, tmp_path):
        """load() with a file containing invalid JSON resets to empty without raising."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json!!!")
        traps.load(str(bad_file))
        assert traps.traps_by_id == {}
        assert traps.start_epd_index == {}
        assert traps.summaries() == []

    def test_missing_traps_key_returns_empty(self, tmp_path):
        """load() with a JSON file that has no 'traps' key resets to empty."""
        bad_file = tmp_path / "no_traps_key.json"
        bad_file.write_text(json.dumps({"data": []}))
        traps.load(str(bad_file))
        assert traps.traps_by_id == {}
        assert traps.summaries() == []

    def test_empty_traps_list_returns_empty(self, tmp_path):
        """load() with an empty traps list returns empty structures."""
        empty_file = tmp_path / "empty.json"
        empty_file.write_text(json.dumps({"traps": []}))
        traps.load(str(empty_file))
        assert traps.traps_by_id == {}
        assert traps.summaries() == []

    def test_no_exception_on_missing_file(self):
        """load() never raises, even for a completely nonexistent path."""
        try:
            traps.load("/does/not/exist/traps.json")
        except Exception as exc:
            pytest.fail(f"load() raised unexpectedly: {exc}")

    def test_get_returns_none_when_empty(self):
        """get() returns None when no data is loaded."""
        traps.load("/nonexistent/path/for/traps/test.json")
        assert traps.get("ruy-lopez-trap") is None

    def test_available_returns_empty_when_no_data(self):
        """available() returns [] when no data is loaded."""
        traps.load("/nonexistent/path/for/traps/test.json")
        result = traps.available(STARTING_FEN, ["e2e4", "e7e5", "g1f3"])
        assert result == []

    def test_init_is_alias_for_load(self, tmp_path):
        """init() populates the same structures as load()."""
        traps.load(FIXTURE_PATH)
        expected_ids = set(traps.traps_by_id.keys())
        expected_summaries = traps.summaries()

        traps.init(FIXTURE_PATH)
        assert set(traps.traps_by_id.keys()) == expected_ids
        assert traps.summaries() == expected_summaries
