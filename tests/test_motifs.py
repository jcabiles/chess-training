"""Unit tests for app.motifs — pure tactical detection, no Stockfish required.

All positions are crafted FENs chosen so that the expected motif is unambiguous.
Each "positive" test verifies the detector fires; each "near-miss" test verifies
no false positive on a similar but tactically different position.
"""

from __future__ import annotations

import chess

from app.motifs import (
    attackers_count,
    defenders_count,
    detect_motifs,
    hanging_pieces,
    see,
)

# ---------------------------------------------------------------------------
# SEE — Static Exchange Evaluation
# ---------------------------------------------------------------------------


class TestSee:
    """Crafted FENs with known SEE results."""

    def test_capture_undefended_pawn_is_winning(self):
        # White rook on d1 captures undefended Black pawn on d6.
        board = chess.Board("8/8/3p4/8/8/8/8/3R4 w - - 0 1")
        move = chess.Move.from_uci("d1d6")
        assert see(board, move) == 100

    def test_capture_undefended_queen_is_winning(self):
        # White rook on d1 captures undefended Black queen on d5.
        board = chess.Board("8/8/8/3q4/8/8/8/3R4 w - - 0 1")
        move = chess.Move(chess.D1, chess.D5)
        assert see(board, move) == 900

    def test_rook_takes_pawn_defended_by_rook_is_losing(self):
        # White Rd1 captures pawn on d6, but Black Rd8 defends it.
        # Net: +100 (pawn) then -500 (rook) = -400 for White.
        board = chess.Board("3r4/8/3p4/8/8/8/8/3R4 w - - 0 1")
        move = chess.Move.from_uci("d1d6")
        assert see(board, move) == -400

    def test_rook_takes_pawn_defended_by_pawn_is_losing(self):
        # White Re5 captures pawn on d6, defended by Black pawn on c7.
        board = chess.Board("8/2p5/3p4/4R3/8/8/8/8 w - - 0 1")
        move = chess.Move.from_uci("e5d6")
        assert see(board, move) == -400

    def test_equal_rook_trade_returns_zero(self):
        # White Rd1 captures Black Rd8 defended by another Black rook Rb8.
        # Net: 500 - 500 = 0.
        board = chess.Board("1r1r4/8/8/8/8/8/8/3R4 w - - 0 1")
        move = chess.Move.from_uci("d1d8")
        assert see(board, move) == 0

    def test_pawn_takes_pawn_defended_by_pawn_is_equal(self):
        # White pawn on e5 takes Black pawn on d6 defended by Black pawn on c7.
        board = chess.Board("8/2p5/3p4/4P3/8/8/8/8 w - - 0 1")
        move = chess.Move.from_uci("e5d6")
        assert see(board, move) == 0

    def test_knight_takes_rook_defended_by_king(self):
        # White Ne3 takes Black Rd5 defended by Black Ke5.
        # Net: 500 - 320 = 180 (take rook, king recaptures knight).
        board = chess.Board("8/8/8/3rk3/8/4N3/8/8 w - - 0 1")
        move = chess.Move.from_uci("e3d5")
        assert see(board, move) == 180

    def test_non_capture_returns_zero(self):
        # Quiet move: no capture.
        board = chess.Board("8/8/8/8/8/8/8/3R4 w - - 0 1")
        move = chess.Move.from_uci("d1d5")
        assert see(board, move) == 0

    def test_equal_queen_trade_defended_by_king(self):
        # White Qd1 takes Black Qd8 defended by Black Ke8.
        # Net: 900 - 900 = 0.
        board = chess.Board("3qk3/8/8/8/8/8/8/3Q4 w - - 0 1")
        move = chess.Move.from_uci("d1d8")
        assert see(board, move) == 0

    def test_see_positive_means_winning_capture(self):
        # Any SEE > 0 indicates the initial capturer comes out ahead.
        board = chess.Board("8/8/3p4/8/8/8/8/3R4 w - - 0 1")
        assert see(board, chess.Move.from_uci("d1d6")) > 0

    def test_see_negative_means_losing_capture(self):
        board = chess.Board("3r4/8/3p4/8/8/8/8/3R4 w - - 0 1")
        assert see(board, chess.Move.from_uci("d1d6")) < 0


# ---------------------------------------------------------------------------
# Attacker / defender counts
# ---------------------------------------------------------------------------


class TestAttackerDefenderCounts:
    def test_undefended_piece_has_zero_defenders(self):
        board = chess.Board("8/8/3p4/8/8/8/8/3R4 w - - 0 1")
        # Black pawn on d6 has no Black defenders.
        assert defenders_count(board, chess.BLACK, chess.D6) == 0

    def test_defended_piece_has_one_defender(self):
        board = chess.Board("8/2p5/3p4/8/8/8/8/8 b - - 0 1")
        # Black pawn on d6 defended by pawn on c7.
        assert defenders_count(board, chess.BLACK, chess.D6) == 1

    def test_attacker_count_one(self):
        board = chess.Board("8/8/3p4/8/8/8/8/3R4 w - - 0 1")
        assert attackers_count(board, chess.WHITE, chess.D6) == 1

    def test_attacker_count_zero_for_unattacked(self):
        board = chess.Board("8/8/8/8/8/8/8/3R4 w - - 0 1")
        # e7 is NOT on the d-file or rank 1, so the White rook cannot attack it.
        assert attackers_count(board, chess.WHITE, chess.E7) == 0


# ---------------------------------------------------------------------------
# Hanging pieces
# ---------------------------------------------------------------------------


class TestHangingPieces:
    def test_undefended_attacked_piece_is_hanging(self):
        # White rook attacks Black queen on d5 (undefended).
        board = chess.Board("8/8/8/3q4/8/8/8/3R4 w - - 0 1")
        hung = hanging_pieces(board, chess.BLACK)
        squares = [h["square"] for h in hung]
        assert chess.D5 in squares

    def test_defended_piece_is_not_hanging(self):
        # Black rook on d8 defended by another Black rook on d7 — equal exchange (SEE = 0).
        # White rook on d1 can take d8 but only breaks even → d8 is NOT hanging.
        board = chess.Board("3r4/3r4/8/8/8/8/8/3R4 w - - 0 1")
        hung = hanging_pieces(board, chess.BLACK)
        squares = [h["square"] for h in hung]
        assert chess.D8 not in squares

    def test_hanging_piece_see_is_negative(self):
        board = chess.Board("8/8/8/3q4/8/8/8/3R4 w - - 0 1")
        hung = hanging_pieces(board, chess.BLACK)
        assert all(h["see"] < 0 for h in hung)

    def test_hanging_pieces_is_json_serializable(self):
        import json

        board = chess.Board("8/8/8/3q4/8/8/8/3R4 w - - 0 1")
        hung = hanging_pieces(board, chess.BLACK)
        # Should not raise.
        json.dumps(hung)


# ---------------------------------------------------------------------------
# detect_motifs — hanging
# ---------------------------------------------------------------------------


class TestHangingMotif:
    def test_hanging_detects_undefended_piece(self):
        # Knight on e5 forks Kc6 and Rd7; Rd7 is hanging (undefended, SEE > 0).
        board = chess.Board("8/3r4/2k5/4N3/8/8/8/1K6 w - - 0 1")
        motifs = detect_motifs(board)
        types = [m["type"] for m in motifs]
        assert "hanging" in types

    def test_hanging_near_miss_defended_piece(self):
        # Black rook on d8 defended by Black Rd7 → SEE = 0 (equal trade) → NOT hanging.
        board = chess.Board("3r4/3r4/8/8/8/8/8/3R4 w - - 0 1")
        motifs = detect_motifs(board)
        hanging = [m for m in motifs if m["type"] == "hanging"]
        assert not any("d8" in m["targets"] for m in hanging)


# ---------------------------------------------------------------------------
# detect_motifs — fork / knight_fork
# ---------------------------------------------------------------------------


class TestForkMotif:
    def _make_knight_fork_board(self) -> chess.Board:
        # White knight on e5 attacks Kc6 and Rd7 simultaneously.
        return chess.Board("8/3r4/2k5/4N3/8/8/8/1K6 w - - 0 1")

    def test_knight_fork_detected(self):
        board = self._make_knight_fork_board()
        motifs = detect_motifs(board)
        assert any(m["type"] == "knight_fork" for m in motifs)

    def test_knight_fork_has_two_targets(self):
        board = self._make_knight_fork_board()
        forks = [m for m in detect_motifs(board) if m["type"] == "knight_fork"]
        assert forks
        assert len(forks[0]["targets"]) >= 2

    def test_knight_fork_identifies_forking_square(self):
        board = self._make_knight_fork_board()
        forks = [m for m in detect_motifs(board) if m["type"] == "knight_fork"]
        assert forks[0]["by"] == "e5"

    def test_fork_near_miss_knight_only_attacks_one_valuable_piece(self):
        # White knight on e5, only Black king on c6 nearby (no second target).
        board = chess.Board("8/8/2k5/4N3/8/8/8/1K6 w - - 0 1")
        motifs = detect_motifs(board)
        fork_motifs = [m for m in motifs if m["type"] in ("fork", "knight_fork")]
        assert not fork_motifs

    def test_motifs_are_json_serializable(self):
        import json

        board = self._make_knight_fork_board()
        json.dumps(detect_motifs(board))  # must not raise


# ---------------------------------------------------------------------------
# detect_motifs — pin
# ---------------------------------------------------------------------------


class TestPinMotif:
    def _make_pin_board(self) -> chess.Board:
        # White bishop on b2 pins Black knight on c3 against Black king on d4.
        # White to move: detect opponent (Black) pieces that are pinned.
        return chess.Board("8/8/8/8/3k4/2n5/1B6/1K6 w - - 0 1")

    def test_pin_detected(self):
        board = self._make_pin_board()
        motifs = detect_motifs(board)
        assert any(m["type"] == "pin" for m in motifs)

    def test_pin_target_is_knight(self):
        board = self._make_pin_board()
        pins = [m for m in detect_motifs(board) if m["type"] == "pin"]
        assert pins
        assert "c3" in pins[0]["targets"]

    def test_pin_pinner_identified(self):
        board = self._make_pin_board()
        pins = [m for m in detect_motifs(board) if m["type"] == "pin"]
        assert pins[0]["by"] == "b2"

    def test_pin_near_miss_bishop_not_on_diagonal(self):
        # Bishop on b1 — NOT on the b2-c3-d4 diagonal, so no pin.
        board = chess.Board("8/8/8/8/3k4/2n5/8/1B4K1 w - - 0 1")
        motifs = detect_motifs(board)
        pins = [m for m in motifs if m["type"] == "pin"]
        # c3 should NOT be pinned.
        assert not any("c3" in m["targets"] for m in pins)


# ---------------------------------------------------------------------------
# detect_motifs — skewer
# ---------------------------------------------------------------------------


class TestSkewerMotif:
    def _make_skewer_board(self) -> chess.Board:
        # White Rh1 skewers Black king on h5; Black rook on h8 is behind the king.
        return chess.Board("7r/8/8/7k/8/8/8/7R w - - 0 1")

    def test_skewer_detected(self):
        board = self._make_skewer_board()
        motifs = detect_motifs(board)
        assert any(m["type"] == "skewer" for m in motifs)

    def test_skewer_targets_king_and_rook(self):
        board = self._make_skewer_board()
        skewers = [m for m in detect_motifs(board) if m["type"] == "skewer"]
        assert skewers
        targets = set(skewers[0]["targets"])
        assert "h5" in targets  # king
        assert "h8" in targets  # rook behind

    def test_skewer_near_miss_rook_on_different_file(self):
        # White rook on e1 — not on h-file, cannot skewer Kh5→Rh8.
        board = chess.Board("7r/8/8/7k/8/8/8/4R2K w - - 0 1")
        motifs = detect_motifs(board)
        skewers = [m for m in motifs if m["type"] == "skewer"]
        # Should not detect a skewer from e1 through h5 to h8.
        assert not any(
            "h5" in m["targets"] and "h8" in m["targets"] for m in skewers
        )


# ---------------------------------------------------------------------------
# detect_motifs — discovered
# ---------------------------------------------------------------------------


class TestDiscoveredMotif:
    def _make_discovered_board(self) -> chess.Board:
        # White rook on a1 blocked by White knight on a4; Black queen on a7.
        # Moving the knight reveals Ra1 attack on Qa7.
        return chess.Board("8/q7/8/8/N7/8/8/R3K3 w Q - 0 1")

    def test_discovered_attack_detected(self):
        board = self._make_discovered_board()
        motifs = detect_motifs(board)
        assert any(m["type"] == "discovered" for m in motifs)

    def test_discovered_target_is_queen(self):
        board = self._make_discovered_board()
        discovered = [m for m in detect_motifs(board) if m["type"] == "discovered"]
        assert discovered
        assert "a7" in discovered[0]["targets"]

    def test_discovered_near_miss_no_blocker(self):
        # White rook on a1, Black queen on a7, NO own piece blocking the file.
        board = chess.Board("8/q7/8/8/8/8/8/R3K3 w Q - 0 1")
        motifs = detect_motifs(board)
        discovered = [m for m in motifs if m["type"] == "discovered"]
        # No blocking piece → no discovered attack motif.
        assert not discovered


# ---------------------------------------------------------------------------
# detect_motifs — back_rank
# ---------------------------------------------------------------------------


class TestBackRankMotif:
    def _make_back_rank_board(self) -> chess.Board:
        # Black king on g8, pawns on f7/g7/h7 (no forward escape), White Rd1.
        # White can play Rd8+ and the king is trapped.
        return chess.Board("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1")

    def test_back_rank_threat_detected(self):
        board = self._make_back_rank_board()
        motifs = detect_motifs(board)
        assert any(m["type"] == "back_rank" for m in motifs)

    def test_back_rank_target_is_king(self):
        board = self._make_back_rank_board()
        back = [m for m in detect_motifs(board) if m["type"] == "back_rank"]
        assert back
        assert "g8" in back[0]["targets"]

    def test_back_rank_near_miss_king_has_flight_square(self):
        # King on f8 — f7/g7/h7 still occupied but f8 itself can go to e8 or e7
        # (different forward squares unblocked → no back-rank threat).
        board = chess.Board("5k2/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1")
        motifs = detect_motifs(board)
        back = [m for m in motifs if m["type"] == "back_rank"]
        assert not back

    def test_back_rank_near_miss_no_attacker_on_back_rank(self):
        # King trapped by pawns but White only has a knight — no back-rank threat.
        board = chess.Board("6k1/5ppp/8/8/8/4N3/5PPP/6K1 w - - 0 1")
        motifs = detect_motifs(board)
        back = [m for m in motifs if m["type"] == "back_rank"]
        assert not back


# ---------------------------------------------------------------------------
# JSON-serializability of all motif types
# ---------------------------------------------------------------------------


def test_detect_motifs_output_always_json_serializable():
    """Every motif dict returned by detect_motifs must be JSON-serializable."""
    import json

    boards = [
        chess.Board(),
        chess.Board("8/3r4/2k5/4N3/8/8/8/1K6 w - - 0 1"),
        chess.Board("8/8/8/8/3k4/2n5/1B6/1K6 w - - 0 1"),
        chess.Board("7r/8/8/7k/8/8/8/7R w - - 0 1"),
        chess.Board("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"),
        chess.Board("8/q7/8/8/N7/8/8/R3K3 w Q - 0 1"),
    ]
    for board in boards:
        motifs = detect_motifs(board)
        json.dumps(motifs)  # must not raise


# ---------------------------------------------------------------------------
# Import-safety: motifs.py must not need a Stockfish binary
# ---------------------------------------------------------------------------


def test_motifs_importable_without_stockfish():
    """Importing app.motifs must never trigger engine startup."""
    import importlib

    importlib.import_module("app.motifs")  # no EngineUnavailable raised
