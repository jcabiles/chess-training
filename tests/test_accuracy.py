"""Unit tests for app.accuracy — pure module, no Stockfish binary required.

All tests are derivable from the spec math in docs/ai-dlc/specs/game-accuracy-elo.md.
This file was written as an independent CHECKER: tests pin the spec behavior, not
whatever the implementation happens to do.

Formula reference (from spec):
    move_acc = clamp(103.1668 * exp(-0.04354 * max(0, win_pct_drop)) - 3.1669, 0, 100)
    elo      = clamp(round(45.0 * accuracy + (-2000.0)), 100, 2900)
"""

from __future__ import annotations

import pytest

from app.accuracy import (
    ELO_MAX,
    ELO_MIN,
    accuracy_to_elo,
    move_accuracy,
    summarize,
)

# ---------------------------------------------------------------------------
# Convenience FENs (differ only in active-color field)
# ---------------------------------------------------------------------------

FEN_WHITE = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN_BLACK = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"


# ---------------------------------------------------------------------------
# Helper: build a minimal ply row dict
# ---------------------------------------------------------------------------

def ply(eval_cp_white=None, mate_white=None, fen_before=FEN_WHITE):
    return {"eval_cp_white": eval_cp_white, "mate_white": mate_white, "fen_before": fen_before}


# ---------------------------------------------------------------------------
# 1. move_accuracy(0.0) ≈ 100.0
# ---------------------------------------------------------------------------

def test_move_accuracy_zero_drop_is_perfect():
    """No win% drop → formula yields 103.1668 - 3.1669 = 99.9999 ≈ 100."""
    acc = move_accuracy(0.0)
    assert acc == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# 2. SCALE-BUG REGRESSION — most important test
# ---------------------------------------------------------------------------

def test_move_accuracy_50pt_drop_is_low():
    """A 50-POINT win% drop must score LOW (≈8.53, not ~97.8%).

    If someone passes a 0–1 probability (0.5 instead of 50), exp(-0.04354*0.5)
    ≈ 0.978 → acc ≈ 97.8%, which is nearly perfect for a blunder.
    This test catches that scale bug: move_accuracy(50.0) must be < 20, not ~100.

    Expected by spec math:
        103.1668 * exp(-0.04354 * 50) - 3.1669
        = 103.1668 * exp(-2.177) - 3.1669
        ≈ 103.1668 * 0.1133 - 3.1669
        ≈ 11.69 - 3.17
        ≈ 8.52
    """
    acc_50 = move_accuracy(50.0)
    assert acc_50 == pytest.approx(8.53, abs=0.2), (
        f"move_accuracy(50.0) should be ≈8.53 (large drop → low score); got {acc_50:.4f}"
    )
    assert acc_50 < 20.0, (
        f"A 50-point win% drop must score < 20; got {acc_50:.4f}. "
        "Likely cause: input was treated as 0–1 probability (drop=0.5 → ~97.8%)."
    )

    # The scale-bug sentinel: passing 0.5 (wrong scale) must yield a VERY
    # different — and much higher — result than passing 50.0 (correct scale).
    acc_half = move_accuracy(0.5)
    assert acc_50 < acc_half - 70.0, (
        f"move_accuracy(50.0)={acc_50:.2f} and move_accuracy(0.5)={acc_half:.2f} "
        "are unexpectedly close; they should differ by at least 70 percentage points."
    )


# ---------------------------------------------------------------------------
# 3. move_accuracy monotonicity and clamping
# ---------------------------------------------------------------------------

def test_move_accuracy_is_monotone_non_increasing():
    """Larger win% drops must produce equal-or-lower accuracy."""
    samples = [0.0, 10.0, 25.0, 50.0]
    accs = [move_accuracy(d) for d in samples]
    for i in range(len(accs) - 1):
        assert accs[i] >= accs[i + 1], (
            f"Expected move_accuracy({samples[i]}) >= move_accuracy({samples[i+1]}), "
            f"got {accs[i]:.4f} vs {accs[i+1]:.4f}"
        )


def test_move_accuracy_clamps_negative_drop():
    """A negative drop (position improved) is clamped to 0 — same as drop=0."""
    assert move_accuracy(-5.0) == move_accuracy(0.0)


def test_move_accuracy_clamps_huge_drop_to_zero():
    """An extreme drop must never produce a negative accuracy."""
    assert move_accuracy(1e6) >= 0.0


# ---------------------------------------------------------------------------
# 4. accuracy_to_elo: anchors, floor, and cap
# ---------------------------------------------------------------------------

def test_accuracy_to_elo_100_pct_is_2500():
    """Spec anchor: 100% accuracy → elo = round(45*100 - 2000) = 2500."""
    assert accuracy_to_elo(100.0) == 2500


def test_accuracy_to_elo_80_pct_is_1600():
    """Spec anchor: 80% accuracy → elo = round(45*80 - 2000) = 1600."""
    assert accuracy_to_elo(80.0) == 1600


def test_accuracy_to_elo_zero_pct_is_floor():
    """0% accuracy: raw = round(45*0 - 2000) = -2000 → clamped to ELO_MIN = 100."""
    assert accuracy_to_elo(0.0) == ELO_MIN
    assert accuracy_to_elo(0.0) == 100


def test_accuracy_to_elo_pathological_high_is_capped():
    """300% (pathological input): raw is very large → clamped to ELO_MAX = 2900."""
    assert accuracy_to_elo(300.0) == ELO_MAX
    assert accuracy_to_elo(300.0) == 2900


# ---------------------------------------------------------------------------
# 5. summarize — perfect game (every move has ~0 win% drop)
# ---------------------------------------------------------------------------

def test_summarize_perfect_game_both_sides_near_100():
    """All moves at same eval → ~0 win% drop per move → ~100% accuracy / ~2500 Elo.

    4 plies (indices 0-3), alternating W/B to move. Final ply (index 3) is excluded.
    Scored moves: ply 0 (White), ply 1 (Black), ply 2 (White) → 2 White, 1 Black.
    """
    ev = 20  # constant White-POV eval across all plies
    plies = [
        ply(eval_cp_white=ev, fen_before=FEN_WHITE),  # ply 0 — White moves
        ply(eval_cp_white=ev, fen_before=FEN_BLACK),  # ply 1 — Black moves
        ply(eval_cp_white=ev, fen_before=FEN_WHITE),  # ply 2 — White moves
        ply(eval_cp_white=ev, fen_before=FEN_BLACK),  # ply 3 — final, excluded
    ]
    result = summarize(plies, "white")

    assert result["white_accuracy"] == pytest.approx(100.0, abs=0.1)
    assert result["black_accuracy"] == pytest.approx(100.0, abs=0.1)
    assert result["white_elo"] == 2500
    assert result["black_elo"] == 2500
    # Final ply excluded: 3 scored plies → 2 White moves (ply 0, 2), 1 Black (ply 1)
    assert result["white_moves"] == 2
    assert result["black_moves"] == 1


# ---------------------------------------------------------------------------
# 6. summarize — White blunder (end-to-end scale-bug guard)
# ---------------------------------------------------------------------------

def test_summarize_white_blunder_pulls_white_accuracy_down():
    """White blunders on move 1: eval swings from +300 to -300 (≈50-pt win% drop).

    White accuracy must be pulled well below 60%; Black (no swing) stays high.
    This is the end-to-end guard ensuring the ×100 scale factor flows through
    summarize correctly — omitting it would make every move score ~100%.

    Plies:
        ply 0 (W to move): eval=+300  →  White plays, big drop
        ply 1 (B to move): eval=-300  (successor of White's blunder)
        ply 2 (W to move): eval=-300  →  Black played cleanly (no swing)
        ply 3 (B to move): eval=-300  (final ply, excluded)
    Scored: White=ply0 (big drop), Black=ply1 (no drop).
    """
    plies = [
        ply(eval_cp_white=300,  fen_before=FEN_WHITE),  # ply 0 — White blunders
        ply(eval_cp_white=-300, fen_before=FEN_BLACK),  # ply 1 — Black plays cleanly
        ply(eval_cp_white=-300, fen_before=FEN_WHITE),  # ply 2 — White (final scored)
        ply(eval_cp_white=-300, fen_before=FEN_BLACK),  # ply 3 — final, excluded
    ]
    result = summarize(plies, "white")

    # White blundered on ply 0 → accuracy pulled low
    assert result["white_accuracy"] is not None
    assert result["white_accuracy"] < 60.0, (
        f"White blundered (+300→-300); expected white_accuracy < 60, "
        f"got {result['white_accuracy']}"
    )
    # Black played cleanly (no eval swing on ply 1→2)
    assert result["black_accuracy"] is not None
    assert result["black_accuracy"] >= 90.0, (
        f"Black had no swing; expected black_accuracy >= 90, "
        f"got {result['black_accuracy']}"
    )


# ---------------------------------------------------------------------------
# 7. summarize — 0-move side
# ---------------------------------------------------------------------------

def test_summarize_zero_move_black_side_is_none():
    """2-ply list: ply 0 scored (White), ply 1 is the final ply (excluded).

    Black has 0 scored moves → black_accuracy is None, black_elo is None, black_moves == 0.
    White has exactly 1 scored move.
    """
    plies = [
        ply(eval_cp_white=50, fen_before=FEN_WHITE),   # ply 0 — White's only scored move
        ply(eval_cp_white=50, fen_before=FEN_BLACK),   # ply 1 — final, excluded
    ]
    result = summarize(plies, "white")

    assert result["black_accuracy"] is None
    assert result["black_elo"] is None
    assert result["black_moves"] == 0
    assert result["white_moves"] == 1


# ---------------------------------------------------------------------------
# 8. summarize — len(plies) <= 1
# ---------------------------------------------------------------------------

def test_summarize_empty_list_returns_all_none():
    """Empty list → both sides None, 0 moves, my_color echoed."""
    result = summarize([], "black")
    assert result["white_accuracy"] is None
    assert result["black_accuracy"] is None
    assert result["white_elo"] is None
    assert result["black_elo"] is None
    assert result["white_moves"] == 0
    assert result["black_moves"] == 0
    assert result["my_color"] == "black"


def test_summarize_single_ply_returns_all_none():
    """1-element list → both sides None, 0 moves (no successor to compute drop)."""
    result = summarize([ply(eval_cp_white=0)], None)
    assert result["white_accuracy"] is None
    assert result["black_accuracy"] is None
    assert result["white_elo"] is None
    assert result["black_elo"] is None
    assert result["white_moves"] == 0
    assert result["black_moves"] == 0


# ---------------------------------------------------------------------------
# 9. summarize — fen_before=None in the middle is skipped (no exception)
# ---------------------------------------------------------------------------

def test_summarize_none_fen_in_middle_is_skipped():
    """A ply with fen_before=None is skipped; surrounding plies still counted.

    Plies:
        ply 0 (W): fen ok, eval=50
        ply 1 (W): fen=None  ← skipped (chess.Board(None) would raise)
        ply 2 (W): fen ok, eval=50
        ply 3 (B): final, excluded
    Ply 0 is scored (its successor is ply 1 which has an eval); ply 1 is skipped.
    """
    plies = [
        ply(eval_cp_white=50, fen_before=FEN_WHITE),   # ply 0 — scored
        {"eval_cp_white": 50, "mate_white": None, "fen_before": None},  # ply 1 — skipped
        ply(eval_cp_white=50, fen_before=FEN_WHITE),   # ply 2 — scored (if fen ok)
        ply(eval_cp_white=50, fen_before=FEN_BLACK),   # ply 3 — final, excluded
    ]
    # Must not raise even with a None fen in the middle
    result = summarize(plies, "white")
    assert isinstance(result, dict)
    # ply 0 scored (W→W, no drop), ply 1 skipped, ply 2 scored (W→B, no drop)
    assert result["white_moves"] >= 1  # at least ply 0 was counted


# ---------------------------------------------------------------------------
# 10. summarize — Black-to-move-first: move attributed to Black
# ---------------------------------------------------------------------------

def test_summarize_black_to_move_first_attributed_to_black():
    """When the first ply has a Black-to-move FEN, that move counts for Black.

    Plies:
        ply 0 (B to move): Black's move
        ply 1 (W to move): successor / White's next move
        ply 2 (B to move): final, excluded
    Black has 1 scored move; White has 1 scored move.
    """
    plies = [
        ply(eval_cp_white=0, fen_before=FEN_BLACK),    # ply 0 — Black moves
        ply(eval_cp_white=0, fen_before=FEN_WHITE),    # ply 1 — White moves
        ply(eval_cp_white=0, fen_before=FEN_BLACK),    # ply 2 — final, excluded
    ]
    result = summarize(plies, None)

    assert result["black_moves"] >= 1, (
        f"First ply is Black-to-move; black_moves should be ≥ 1, got {result['black_moves']}"
    )


# ---------------------------------------------------------------------------
# 11. summarize — my_color is echoed back unchanged
# ---------------------------------------------------------------------------

def test_summarize_echoes_my_color_unchanged():
    """my_color must be passed through as-is into the returned dict."""
    for color in ("white", "black", None, "some-other-value"):
        result = summarize([], color)
        assert result["my_color"] == color, (
            f"Expected my_color={color!r} to be echoed; got {result['my_color']!r}"
        )
