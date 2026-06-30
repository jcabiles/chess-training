"""Unit tests for app.analysis (pure logic, no Stockfish binary required).

PovScore objects are constructed directly from the pure-Python helpers
``chess.engine.PovScore``, ``chess.engine.Cp``, and ``chess.engine.Mate`` — no
engine process or network is involved.
"""

from __future__ import annotations

import chess
import chess.engine
import pytest

from app.analysis import (
    BEST_MAX,
    BLUNDER_WP_DROP,
    GOOD_MAX,
    INACCURACY_MAX,
    MATE_CP,
    MISTAKE_MAX,
    MISTAKE_WP_DROP,
    bucket,
    classify,
    game_phase,
    leak_severity,
    pov_score_to_white_cp,
    win_prob_from_cp,
    win_prob_white,
)

# --- pov_score_to_white_cp: centipawn scores ---------------------------------


def test_cp_score_from_white_pov_is_identity():
    score = chess.engine.PovScore(chess.engine.Cp(80), chess.WHITE)
    assert pov_score_to_white_cp(score) == 80


def test_cp_score_from_black_pov_is_flipped_to_white():
    # +120 for Black-to-move means -120 from White's POV.
    score = chess.engine.PovScore(chess.engine.Cp(120), chess.BLACK)
    assert pov_score_to_white_cp(score) == -120


# --- pov_score_to_white_cp: mate mapping -------------------------------------


def test_mate_mapping_m1_magnitude_greater_than_m8():
    m1 = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(1), chess.WHITE)
    )
    m8 = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(8), chess.WHITE)
    )
    assert m1 == MATE_CP - 1
    assert m8 == MATE_CP - 8
    assert abs(m1) > abs(m8)


def test_mate_mapping_white_delivers_is_positive():
    score = chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
    assert pov_score_to_white_cp(score) == MATE_CP - 3


def test_mate_mapping_black_delivers_is_negative():
    # Mate(-3) from White's POV: Black mates in 3 -> negative White-POV cp.
    score = chess.engine.PovScore(chess.engine.Mate(-3), chess.WHITE)
    assert pov_score_to_white_cp(score) == -(MATE_CP - 3)


def test_mate_mapping_respects_pov_flip():
    # Mate(2) for Black-to-move => from White POV this is Black mating in 2.
    score = chess.engine.PovScore(chess.engine.Mate(2), chess.BLACK)
    assert pov_score_to_white_cp(score) == -(MATE_CP - 2)


# --- classify: sign cases ----------------------------------------------------


def test_classify_white_move_sign():
    # White's eval drops 200cp (300 -> 100): White gave up 200 -> mistake.
    assert classify(300, 100, mover_is_white=True) == "mistake"


def test_classify_black_move_sign():
    # White-POV eval rises 200cp (-300 -> -100): Black gave up 200 -> mistake.
    assert classify(-300, -100, mover_is_white=False) == "mistake"


def test_classify_white_good_move_no_loss():
    # White improves its own eval -> negative cpLoss clamps to 0 -> best.
    assert classify(100, 300, mover_is_white=True) == "best"


# --- classify: negative-cpLoss clamp ----------------------------------------


def test_classify_negative_cploss_clamps_to_best():
    # Black "loss" of -150 (White eval fell, good for Black) clamps to 0.
    assert classify(0, -150, mover_is_white=False) == "best"


def test_classify_zero_cploss_is_best():
    assert classify(50, 50, mover_is_white=True) == "best"


# --- bucket boundaries -------------------------------------------------------


@pytest.mark.parametrize(
    "cp_loss, expected",
    [
        (0, "best"),
        (BEST_MAX, "best"),            # 10 -> best
        (BEST_MAX + 1, "good"),        # 11 -> good
        (GOOD_MAX, "good"),            # 50 -> good
        (GOOD_MAX + 1, "inaccuracy"),  # 51 -> inaccuracy
        (INACCURACY_MAX, "inaccuracy"),    # 100 -> inaccuracy
        (INACCURACY_MAX + 1, "mistake"),   # 101 -> mistake
        (MISTAKE_MAX, "mistake"),          # 250 -> mistake
        (MISTAKE_MAX + 1, "blunder"),      # 251 -> blunder
    ],
)
def test_bucket_boundaries(cp_loss, expected):
    assert bucket(cp_loss) == expected


@pytest.mark.parametrize(
    "before, after, mover_is_white, expected",
    [
        (10, 0, True, "best"),     # cpLoss 10 -> best
        (11, 0, True, "good"),     # cpLoss 11 -> good
        (50, 0, True, "good"),     # cpLoss 50 -> good
        (51, 0, True, "inaccuracy"),   # cpLoss 51 -> inaccuracy
        (100, 0, True, "inaccuracy"),  # cpLoss 100 -> inaccuracy
        (101, 0, True, "mistake"),     # cpLoss 101 -> mistake
        (250, 0, True, "mistake"),     # cpLoss 250 -> mistake
        (251, 0, True, "blunder"),     # cpLoss 251 -> blunder
    ],
)
def test_classify_bucket_boundaries_via_white_move(before, after, mover_is_white, expected):
    assert classify(before, after, mover_is_white) == expected


# --- classify: mate scenarios end-to-end ------------------------------------


def test_classify_threw_away_forced_mate_is_blunder():
    # White had M1 (MATE_CP - 1) and ended at a quiet +0 -> huge cpLoss.
    before = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(1), chess.WHITE)
    )
    assert classify(before, 0, mover_is_white=True) == "blunder"


def test_classify_walked_into_mate_is_blunder():
    # White was +0 and now Black has M2 against White -> huge cpLoss.
    after = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE)
    )
    assert classify(0, after, mover_is_white=True) == "blunder"


def test_classify_faster_mate_is_small_loss():
    # M5 -> M3: still winning, just slower-to-faster nuance. cpLoss small.
    before = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(5), chess.WHITE)
    )
    after = pov_score_to_white_cp(
        chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
    )
    # White goes from M5 to M3 (faster) -> White improved -> clamps to best.
    assert classify(before, after, mover_is_white=True) == "best"


# ---------------------------------------------------------------------------
# T3 additions: win_prob_from_cp
# ---------------------------------------------------------------------------


def test_win_prob_at_zero_is_half():
    assert abs(win_prob_from_cp(0) - 0.5) < 1e-6


def test_win_prob_symmetric_around_zero():
    """win_prob(cp) + win_prob(-cp) == 1.0 for all cp."""
    for cp in (1, 50, 100, 250, 500, 1000):
        total = win_prob_from_cp(cp) + win_prob_from_cp(-cp)
        assert abs(total - 1.0) < 1e-9, f"symmetry failed at cp={cp}: {total}"


def test_win_prob_monotonically_increasing():
    """Higher cp advantage → higher win probability."""
    cps = list(range(-500, 501, 50))
    probs = [win_prob_from_cp(cp) for cp in cps]
    for i in range(len(probs) - 1):
        assert probs[i] <= probs[i + 1], (
            f"Not monotonic: win_prob({cps[i]})={probs[i]} "
            f"> win_prob({cps[i + 1]})={probs[i + 1]}"
        )


def test_win_prob_clamped_to_unit_interval():
    """Result is always in [0, 1] even for extreme values."""
    assert 0.0 <= win_prob_from_cp(100_000) <= 1.0
    assert 0.0 <= win_prob_from_cp(-100_000) <= 1.0


def test_win_prob_large_advantage_approaches_one():
    assert win_prob_from_cp(2000) > 0.99


def test_win_prob_large_disadvantage_approaches_zero():
    assert win_prob_from_cp(-2000) < 0.01


# ---------------------------------------------------------------------------
# T3 additions: win_prob_white
# ---------------------------------------------------------------------------


def test_win_prob_white_mate_for_white():
    assert win_prob_white(None, 3) == 1.0


def test_win_prob_white_mate_for_black():
    assert win_prob_white(None, -3) == 0.0


def test_win_prob_white_uses_cp_when_no_mate():
    prob = win_prob_white(100, None)
    assert abs(prob - win_prob_from_cp(100)) < 1e-9


def test_win_prob_white_unknown_eval_is_half():
    assert win_prob_white(None, None) == 0.5


# ---------------------------------------------------------------------------
# T3 additions: game_phase
# ---------------------------------------------------------------------------


def test_game_phase_start_position_is_opening():
    assert game_phase(chess.Board()) == "opening"


def test_game_phase_king_vs_king_is_endgame():
    board = chess.Board("8/8/8/8/4K3/8/8/4k3 w - - 0 1")
    assert game_phase(board) == "endgame"


def test_game_phase_rook_endgame_is_endgame():
    # Two kings and two rooks — low material total.
    board = chess.Board("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    assert game_phase(board) == "endgame"


def test_game_phase_full_pieces_is_opening():
    # Full opening material: 2N+2B+2R+Q per side → total = 2*3+2*3+2*5+9 = 39 per side = 78.
    assert game_phase(chess.Board()) == "opening"


@pytest.mark.parametrize(
    "fen, expected",
    [
        # K+Q+R+R vs k — 9+5+5 = 19 for White only = 19 total → endgame (<24).
        ("4k3/8/8/8/8/8/8/1KQ1RR2 w - - 0 1", "endgame"),
        # K+Q+R+R vs k+q+r+r — 19*2=38 → middlegame (24..55).
        ("1kq1rr2/8/8/8/8/8/8/1KQ1RR2 w - - 0 1", "middlegame"),
    ],
)
def test_game_phase_boundaries(fen, expected):
    assert game_phase(chess.Board(fen)) == expected


# ---------------------------------------------------------------------------
# T3 additions: leak_severity
# ---------------------------------------------------------------------------


def test_leak_severity_below_mistake_is_none():
    assert leak_severity(0.0) == "none"
    assert leak_severity(MISTAKE_WP_DROP - 0.001) == "none"


def test_leak_severity_at_mistake_threshold():
    assert leak_severity(MISTAKE_WP_DROP) == "mistake"


def test_leak_severity_between_thresholds_is_mistake():
    mid = (MISTAKE_WP_DROP + BLUNDER_WP_DROP) / 2
    assert leak_severity(mid) == "mistake"


def test_leak_severity_at_blunder_threshold():
    assert leak_severity(BLUNDER_WP_DROP) == "blunder"


def test_leak_severity_above_blunder_is_blunder():
    assert leak_severity(BLUNDER_WP_DROP + 0.1) == "blunder"
    assert leak_severity(1.0) == "blunder"


def test_leak_severity_constants_are_sane():
    """Sanity-check the level-tuned thresholds are reasonable."""
    assert 0 < MISTAKE_WP_DROP < BLUNDER_WP_DROP < 1.0
