"""
Tests for app/openings.py — Opening Trainer backend logic.

No Stockfish needed. Uses tests/fixtures/openings_sample.tsv only.
"""

from __future__ import annotations

import chess
import pytest

from app import openings

FIXTURE_DIR = "tests/fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STARTING_FEN = chess.STARTING_FEN


def _epd_after(uci_str: str) -> str:
    """Replay a space-separated UCI string and return the final EPD."""
    board = chess.Board()
    for m in uci_str.split():
        board.push_uci(m)
    return board.epd()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def idx():
    """Load the sample fixture TSV once for the whole module."""
    return openings.load(FIXTURE_DIR)


# ---------------------------------------------------------------------------
# EPD-convention assertion (guards the canonical-producer requirement)
# ---------------------------------------------------------------------------


def test_epd_convention_matches_tsv(idx):
    """Replaying a fixture row's UCI and calling board.epd() must equal the TSV epd column.

    Samples the C01 French Defense: Exchange Variation row.
    """
    # C01 French Defense: Exchange Variation
    uci = "e2e4 e7e6 d2d4 d7d5 e4d5"
    expected_epd = "rnbqkbnr/ppp2ppp/4p3/3P4/3P4/8/PPP2PPP/RNBQKBNR b KQkq -"
    derived_epd = _epd_after(uci)
    assert derived_epd == expected_epd, (
        f"EPD mismatch: derived={derived_epd!r}, expected={expected_epd!r}"
    )
    # Also verify the EPD is in the index (meaning the TSV was parsed correctly).
    assert derived_epd in idx.name_by_epd


def test_ep_epd_convention(idx):
    """The EP-line row must produce an EPD with a real en-passant square (f6)."""
    # C44 King's Pawn Game: EP line — 1.e4 d5 2.e5 f5 -> ep square f6
    uci = "e2e4 d7d5 e4e5 f7f5"
    epd = _epd_after(uci)
    # python-chess emits the ep square only when the capture is legal
    assert epd.endswith("f6"), f"Expected ep square f6, got: {epd!r}"
    assert epd in idx.name_by_epd


# ---------------------------------------------------------------------------
# Name detection
# ---------------------------------------------------------------------------


def test_identify_ruy_lopez(idx):
    """identify() returns the Ruy Lopez name for the standard move order."""
    result = openings.identify(STARTING_FEN, ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"])
    assert result is not None
    assert result["eco"] == "C60"
    assert "Ruy Lopez" in result["name"]


def test_identify_returns_deepest_match(idx):
    """identify() returns the deepest (most-specific) named opening on the line.

    After e4 e5 Nf3 Nc6 Bb5 the position is Ruy Lopez (C60, 5 plies),
    which should beat King's Pawn Game: Center Game (3 plies) and
    Alekhine's Defense (2 plies) etc.
    """
    result = openings.identify(STARTING_FEN, ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"])
    assert result is not None
    # C60 Ruy Lopez is the deepest match (5 plies) on this line
    assert result["eco"] == "C60"


def test_identify_returns_none_for_unknown(idx):
    """identify() returns None when no named opening matches the line."""
    result = openings.identify(STARTING_FEN, ["g1f3", "g8f6", "g2g3"])
    assert result is None


def test_identify_partial_match(idx):
    """identify() returns a shallower match when the line goes past known data."""
    # Play the Italian Game (C50, 5 plies) then an extra unknown move
    result = openings.identify(
        STARTING_FEN, ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "d7d6"]
    )
    # The line after 6 plies is not in the fixture, but C50 Italian matches at ply 5
    assert result is not None
    assert result["eco"] == "C50"


# ---------------------------------------------------------------------------
# Transposition test
# ---------------------------------------------------------------------------


def test_transposition_same_result(idx):
    """Two move orders reaching the same position must return the same {eco, name}.

    Standard order: 1.e4 e5 2.Nf3 Nc6 3.Bb5
    Transposition:  1.Nf3 Nc6 2.e4 e5 3.Bb5
    Both arrive at the Ruy Lopez EPD.
    """
    standard = openings.identify(
        STARTING_FEN, ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]
    )
    transposition = openings.identify(
        STARTING_FEN, ["g1f3", "b8c6", "e2e4", "e7e5", "f1b5"]
    )
    assert standard is not None
    assert transposition is not None
    assert standard == transposition, (
        f"Transposition mismatch: standard={standard}, transposition={transposition}"
    )


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def test_candidates_from_e4e5nf3nc6_prefix(idx):
    """candidates() lists openings reachable beyond the e4 e5 Nf3 Nc6 position.

    Both Ruy Lopez (C60) and Italian Game (C50) continue from that node.
    """
    fen_after_4_plies = chess.Board()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6"]:
        fen_after_4_plies.push_uci(m)
    fen = fen_after_4_plies.fen()

    result = openings.candidates(fen)
    assert isinstance(result, dict)
    assert "items" in result
    assert "truncated" in result

    names = [item["name"] for item in result["items"]]
    assert any("Ruy Lopez" in n for n in names), f"Ruy Lopez not in candidates: {names}"
    assert any("Italian" in n for n in names), f"Italian Game not in candidates: {names}"


def test_candidates_ascending_plies_order(idx):
    """candidates() items are sorted shortest-line-first, then name alphabetically."""
    # From the starting position many lines apply — check ordering invariant.
    result = openings.candidates(STARTING_FEN)
    items = result["items"]
    if len(items) < 2:
        pytest.skip("not enough candidates to test ordering")
    plies = [len(it["san"]) for it in items]
    for i in range(len(plies) - 1):
        if plies[i] > plies[i + 1]:
            pytest.fail(
                f"candidates not sorted by plies: item[{i}]={plies[i]} > item[{i+1}]={plies[i+1]}"
            )
        if plies[i] == plies[i + 1]:
            assert items[i]["name"] <= items[i + 1]["name"], (
                f"Same-ply items not sorted by name: {items[i]['name']!r} > {items[i+1]['name']!r}"
            )


def test_candidates_dedupe_by_name(idx):
    """candidates() must not return two items with the same name."""
    result = openings.candidates(STARTING_FEN)
    names = [it["name"] for it in result["items"]]
    assert len(names) == len(set(names)), f"Duplicate names in candidates: {names}"


def test_candidates_cap_and_truncated(idx):
    """candidates() with cap=1 returns at most 1 item and truncated=True if more exist."""
    # From starting position we have several lines in the fixture
    result_uncapped = openings.candidates(STARTING_FEN)
    if len(result_uncapped["items"]) < 2:
        pytest.skip("not enough candidates to test cap")

    result = openings.candidates(STARTING_FEN, cap=1)
    assert len(result["items"]) <= 1
    assert result["truncated"] is True


def test_candidates_q_filter(idx):
    """candidates() with q='Ruy' returns only lines whose name contains 'Ruy'."""
    result = openings.candidates(STARTING_FEN, q="Ruy")
    for item in result["items"]:
        assert "ruy" in item["name"].lower(), (
            f"q filter failed: {item['name']!r} does not contain 'ruy'"
        )


def test_candidates_q_filter_case_insensitive(idx):
    """candidates() q filter is case-insensitive."""
    result_lower = openings.candidates(STARTING_FEN, q="ruy")
    result_upper = openings.candidates(STARTING_FEN, q="RUY")
    assert result_lower["items"] == result_upper["items"]


def test_candidates_excludes_final_node(idx):
    """candidates() must not include lines where the current position is the final node.

    The Ruy Lopez's own final EPD should NOT appear as a candidate from that FEN.
    """
    ruy_fen = chess.Board()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]:
        ruy_fen.push_uci(m)
    fen = ruy_fen.fen()

    result = openings.candidates(fen)
    # C60 Ruy Lopez (5 plies) ends HERE — must not appear.
    # C65 Ruy Lopez: Berlin Defense (6 plies) extends beyond — may appear.
    eco_names = [(it["eco"], it["name"]) for it in result["items"]]
    assert ("C60", "Ruy Lopez") not in eco_names, (
        f"Final node should not be a candidate: {eco_names}"
    )


def test_candidates_empty_when_no_index():
    """candidates() returns empty dict when no data loaded."""
    # Reload with nonexistent path to get empty index
    empty_idx = openings.load("/nonexistent/path/for/test")
    # At module level _index is now empty — test via the module functions.
    result = openings.candidates(STARTING_FEN)
    assert result == {"items": [], "truncated": False}
    # Restore the real fixture index for subsequent tests
    openings.load(FIXTURE_DIR)


# ---------------------------------------------------------------------------
# Commentary lookup
# ---------------------------------------------------------------------------


def test_commentary_hit():
    """commentary_lookup() returns the entry when the EPD is in the commentary dict."""
    # Use the Ruy Lopez EPD as a key
    ruy_epd = _epd_after("e2e4 e7e5 g1f3 b8c6 f1b5")
    commentary = {
        ruy_epd: {
            "san": "Bb5",
            "text": "Pins the Nc6 and pressures e5; the defining move of the Ruy Lopez.",
        }
    }
    # FEN string that corresponds to the Ruy Lopez EPD
    board = chess.Board()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]:
        board.push_uci(m)
    fen = board.fen()

    result = openings.commentary_lookup(fen, commentary)
    assert result is not None
    assert result["san"] == "Bb5"
    assert "Ruy Lopez" in result["text"]


def test_commentary_miss():
    """commentary_lookup() returns None when the FEN's EPD is not in the dict."""
    board = chess.Board()
    board.push_uci("d2d4")
    fen = board.fen()
    result = openings.commentary_lookup(fen, {})
    assert result is None


def test_commentary_fen_to_epd_normalization():
    """commentary_lookup() converts FEN to EPD, so a FEN key won't match — only EPD keys do."""
    ruy_epd = _epd_after("e2e4 e7e5 g1f3 b8c6 f1b5")
    board = chess.Board()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]:
        board.push_uci(m)
    fen = board.fen()  # FEN has clock fields; EPD does not

    # Keyed by EPD — must hit
    result_epd = openings.commentary_lookup(fen, {ruy_epd: {"san": "Bb5", "text": "..."}})
    assert result_epd is not None

    # Keyed by FEN — must miss (wrong key format)
    result_fen = openings.commentary_lookup(fen, {fen: {"san": "Bb5", "text": "..."}})
    assert result_fen is None


# ---------------------------------------------------------------------------
# Missing-data degradation
# ---------------------------------------------------------------------------


def test_load_nonexistent_dir_returns_empty():
    """load() with a nonexistent directory returns empty structures without raising."""
    idx = openings.load("/nonexistent/path/for/openings/test")
    assert idx.name_by_epd == {}
    assert idx.lines == []
    assert idx.passes_through == {}


def test_identify_with_empty_index_returns_none():
    """identify() returns None when the index is empty (no data loaded)."""
    openings.load("/nonexistent/path/for/openings/test")
    result = openings.identify(STARTING_FEN, ["e2e4", "e7e5"])
    assert result is None
    # Restore
    openings.load(FIXTURE_DIR)


def test_candidates_with_empty_index_returns_empty():
    """candidates() returns empty dict when no data loaded."""
    openings.load("/nonexistent/path/for/openings/test")
    result = openings.candidates(STARTING_FEN)
    assert result == {"items": [], "truncated": False}
    # Restore
    openings.load(FIXTURE_DIR)


def test_load_empty_dir_returns_empty(tmp_path):
    """load() with an existing but empty directory returns empty structures."""
    idx = openings.load(str(tmp_path))
    assert idx.lines == []
    assert idx.name_by_epd == {}
    # Restore
    openings.load(FIXTURE_DIR)


def test_no_exception_on_bad_fen():
    """identify() and candidates() do not raise on an invalid FEN."""
    openings.load(FIXTURE_DIR)
    result = openings.identify("not a valid fen", ["e2e4"])
    assert result is None
    result2 = openings.candidates("not a valid fen")
    assert result2 == {"items": [], "truncated": False}
